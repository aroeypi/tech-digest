#!/usr/bin/env python3
"""Daily Tech / AI / Security digest -> Slack.

표준 라이브러리만 사용합니다 (pip install 불필요).

환경 변수
  SLACK_WEBHOOK_URLS   Slack Incoming Webhook URL. 여러 개는 줄바꿈 또는 쉼표로 구분.
                       (구독 채널/워크스페이스 하나당 URL 하나)
  ANTHROPIC_API_KEY    (선택) 있으면 각 기사에 한국어 한 줄 요약을 붙입니다.
  ANTHROPIC_MODEL      (선택) 요약 모델. 기본값 claude-haiku-4-5

사용법
  python digest.py              # 수집 -> Slack 발송 -> state/seen.json 갱신
  python digest.py --dry-run    # 발송하지 않고 Slack 메시지 JSON만 출력
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import email.utils
import hashlib
import html
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "feeds.json"
STATE_PATH = ROOT / "state" / "seen.json"
USER_AGENT = "Mozilla/5.0 (compatible; tech-digest/1.0; +https://github.com/)"
KST = timezone(timedelta(hours=9))
SEEN_RETENTION_DAYS = 14


@dataclass
class Item:
    title: str
    link: str
    source: str
    published: datetime | None
    summary: str = ""

    @property
    def key(self) -> str:
        return hashlib.sha1(normalize_url(self.link).encode()).hexdigest()[:16]


# ---------------------------------------------------------------- fetching

def fetch(url: str, timeout: int = 20) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def normalize_url(url: str) -> str:
    p = urllib.parse.urlsplit(url.strip())
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query) if not k.startswith("utm_")]
    return urllib.parse.urlunsplit(
        (p.scheme.lower(), p.netloc.lower(), p.path.rstrip("/"), urllib.parse.urlencode(q), "")
    )


def parse_date(text: str | None) -> datetime | None:
    if not text:
        return None
    text = text.strip()
    try:  # RFC 822 (RSS)
        dt = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        try:  # ISO 8601 (Atom)
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child(el: ET.Element, name: str) -> ET.Element | None:
    for c in el:
        if _local(c.tag) == name:
            return c
    return None


def _text(el: ET.Element, *names: str) -> str:
    for n in names:
        c = _child(el, n)
        if c is not None and (c.text or "").strip():
            return c.text.strip()
    return ""


def clean_text(s: str, limit: int = 300) -> str:
    s = re.sub(r"<[^>]+>", " ", html.unescape(s or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s[: limit - 1] + "…" if len(s) > limit else s


def parse_feed(data: bytes) -> tuple[str, list[Item]]:
    root = ET.fromstring(data)
    items: list[Item] = []
    if _local(root.tag) == "feed":  # Atom
        source = _text(root, "title")
        for e in root:
            if _local(e.tag) != "entry":
                continue
            link = ""
            for l in e:
                if _local(l.tag) == "link" and l.get("rel", "alternate") == "alternate":
                    link = l.get("href", "")
                    break
            items.append(Item(
                title=clean_text(_text(e, "title"), 200),
                link=link,
                source=source,
                published=parse_date(_text(e, "published", "updated")),
                summary=clean_text(_text(e, "summary", "content")),
            ))
    else:  # RSS 2.0 / RDF
        channel = _child(root, "channel")
        source = _text(channel, "title") if channel is not None else ""
        parent = channel if channel is not None and _child(channel, "item") is not None else root
        for e in parent:
            if _local(e.tag) != "item":
                continue
            items.append(Item(
                title=clean_text(_text(e, "title"), 200),
                link=_text(e, "link", "guid"),
                source=source,
                published=parse_date(_text(e, "pubDate", "date", "published", "updated")),
                summary=clean_text(_text(e, "description", "encoded")),
            ))
    source = clean_text(source, 60)
    for it in items:
        it.source = source
    return source, [i for i in items if i.title and i.link.startswith("http")]


def collect(urls: list[str]) -> list[Item]:
    out: list[Item] = []

    def one(u: str) -> list[Item]:
        try:
            return parse_feed(fetch(u))[1]
        except Exception as exc:  # 피드 하나가 실패해도 전체는 계속 진행
            print(f"[warn] {u}: {exc}", file=sys.stderr)
            return []

    with cf.ThreadPoolExecutor(max_workers=8) as pool:
        for items in pool.map(one, urls):
            out.extend(items)
    return out


# ---------------------------------------------------------------- selection

def _title_key(t: str) -> str:
    return re.sub(r"[^a-z0-9가-힣]", "", t.lower())[:60]


def select(items: list[Item], seen: dict, since: datetime, limit: int) -> list[Item]:
    picked: list[Item] = []
    titles: set[str] = set()
    items = sorted(items, key=lambda i: i.published or since, reverse=True)
    for it in items:
        if it.published and it.published < since:
            continue
        tk = _title_key(it.title)
        if it.key in seen or tk in titles:
            continue
        picked.append(it)
        titles.add(tk)
        if len(picked) >= limit:
            break
    return picked


# ---------------------------------------------------------------- optional AI summary

def summarize_ko(sections: dict[str, list[Item]]) -> None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        return
    flat = [it for items in sections.values() for it in items]
    if not flat:
        return
    listing = "\n".join(
        f"{n}. {it.title}\n   {it.summary[:250]}" for n, it in enumerate(flat, 1)
    )
    prompt = (
        "다음은 오늘의 tech/AI/security 뉴스 목록입니다. 각 항목을 한국어로 "
        "핵심만 담아 한 문장(60자 이내)으로 요약하세요. 과장 없이 사실만 쓰세요.\n"
        'JSON 배열로만 답하세요. 형식: [{"n": 1, "ko": "..."}, ...]\n\n' + listing
    )
    body = json.dumps({
        "model": os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5"),
        "max_tokens": 2000,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=body,
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            text = json.load(resp)["content"][0]["text"]
        rows = json.loads(text[text.index("["): text.rindex("]") + 1])
        for r in rows:
            n = int(r["n"])
            if 1 <= n <= len(flat):
                flat[n - 1].summary = "🇰🇷 " + clean_text(str(r["ko"]), 140)
    except Exception as exc:  # 요약 실패 시 원문 요약으로 발송
        print(f"[warn] summary failed: {exc}", file=sys.stderr)


# ---------------------------------------------------------------- slack

def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_message(sections: list[tuple[str, list[Item]]], today: datetime) -> dict:
    total = sum(len(v) for _, v in sections)
    date_str = today.strftime("%Y-%m-%d (%a)")
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": f"📰 Daily Tech Digest · {date_str}"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": f"오늘의 Tech · AI · Security 소식 {total}건"}]},
    ]
    for title, items in sections:
        blocks.append({"type": "divider"})
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*"}})
        if not items:
            blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "_새 소식 없음_"}]})
            continue
        lines = []
        for it in items:
            t = esc(it.title).replace("|", "¦")
            line = f"• <{it.link}|{t}>  _{esc(it.source)}_"
            if it.summary:
                line += f"\n    {esc(clean_text(it.summary, 160))}"
            lines.append(line)
        # section 텍스트 3000자 제한 대응
        chunk = ""
        for line in lines:
            if len(chunk) + len(line) + 1 > 2900:
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": chunk}})
                chunk = ""
            chunk += line + "\n"
        if chunk:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": chunk}})
    return {"text": f"Daily Tech Digest {date_str} ({total}건)", "blocks": blocks[:50], "unfurl_links": False}


def webhook_urls() -> list[str]:
    raw = os.environ.get("SLACK_WEBHOOK_URLS", "")
    return [u.strip() for u in re.split(r"[\s,]+", raw) if u.strip()]


def post(url: str, payload: dict) -> None:
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        resp.read()


# ---------------------------------------------------------------- main

def load_seen() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_seen(seen: dict, now: datetime) -> None:
    cutoff = (now - timedelta(days=SEEN_RETENTION_DAYS)).isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    STATE_PATH.parent.mkdir(exist_ok=True)
    STATE_PATH.write_text(json.dumps(seen, indent=0, sort_keys=True) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="발송하지 않고 메시지 JSON 출력")
    args = ap.parse_args()

    cfg = json.loads(CONFIG_PATH.read_text())
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=cfg.get("window_hours", 30))
    limit = cfg.get("max_items_per_category", 7)
    seen = load_seen()

    sections: dict[str, list[Item]] = {}
    used: set[str] = set()
    for cat in cfg["categories"]:
        picked = select(collect(cat["feeds"]), {**seen, **{k: "" for k in used}}, since, limit)
        used.update(i.key for i in picked)  # 카테고리 간 중복 제거
        sections[cat["title"]] = picked

    summarize_ko(sections)
    payload = build_message(list(sections.items()), now.astimezone(KST))

    if args.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    urls = webhook_urls()
    if not urls:
        print("SLACK_WEBHOOK_URLS 가 비어 있습니다.", file=sys.stderr)
        return 1
    if not any(sections.values()):
        print("새 소식이 없어 발송하지 않습니다.")
        return 0

    failed = 0
    for u in urls:
        try:
            post(u, payload)
        except urllib.error.URLError as exc:
            failed += 1
            print(f"[error] webhook #{urls.index(u) + 1} 발송 실패: {exc}", file=sys.stderr)
    print(f"발송 완료: {len(urls) - failed}/{len(urls)}개 채널")

    if failed < len(urls):
        stamp = now.isoformat()
        for items in sections.values():
            for it in items:
                seen[it.key] = stamp
        save_seen(seen, now)
    return 1 if failed == len(urls) else 0


if __name__ == "__main__":
    sys.exit(main())
