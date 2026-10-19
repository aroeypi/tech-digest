# 📰 Daily Tech Digest → Slack

매일 아침 Tech · AI · Security 소식을 RSS로 모아 여러 Slack 채널에 동시에 보내주는 서비스예요.
GitHub Actions에서 돌아가서 **서버도, 비용도 필요 없어요**. Python 표준 라이브러리만 써요.

```
RSS 피드 13개 ──▶ digest.py ──▶ 최근 30시간 + 중복 제거 ──▶ (선택) Claude 한국어 요약 ──▶ Slack 채널 N개
                    ▲                                                                    │
                    └──────────── state/seen.json (보낸 기사 기록, 14일 보관) ◀──────────────┘
```

## 설정 (10분)

### 1. 레포 만들기
이 폴더를 새 GitHub 레포로 올려요 (public이면 Actions 무료 무제한).

### 2. Slack Webhook 만들기
1. https://api.slack.com/apps → **Create New App** → *From scratch* → 이름 `Tech Digest`, 워크스페이스 선택
2. 왼쪽 **Incoming Webhooks** → On
3. **Add New Webhook to Workspace** → 보낼 채널 선택 (예: `#tech-digest`) → URL 복사
   (`https://hooks.slack.com/services/...`)

### 3. GitHub Secret 등록
레포 **Settings → Secrets and variables → Actions → New repository secret**

| 이름 | 값 |
|---|---|
| `SLACK_WEBHOOK_URLS` | 위에서 복사한 URL. **여러 채널이면 한 줄에 하나씩** |
| `ANTHROPIC_API_KEY` | (선택) 넣으면 기사마다 한국어 한 줄 요약이 붙어요 |

### 4. 테스트
**Actions → Daily Tech Digest → Run workflow**
- `dry_run` 체크 → 로그에서 메시지 미리보기만
- 체크 해제 → 실제 발송

이후 매일 **07:50 KST**쯤 자동 발송돼요 (GitHub 스케줄은 조금 늦어질 수 있어요).

## 구독자 늘리기

| 방식 | 방법 |
|---|---|
| **같은 워크스페이스** | 공개 채널 `#tech-digest` 하나에 보내고, 구독자는 채널에 참여만 하면 끝 (가장 간단) |
| **다른 채널/워크스페이스** | 상대가 자기 워크스페이스에 Webhook을 만들어 URL을 주면 `SLACK_WEBHOOK_URLS`에 한 줄 추가 |
| **셀프 구독 (다음 단계)** | Slack 앱을 *Public Distribution*으로 켜고 "Add to Slack" OAuth 페이지를 만들면, 구독자가 버튼 한 번으로 등록 → webhook URL을 DB에 저장 |

> Webhook URL은 비밀번호처럼 다뤄 주세요. 코드나 이슈에 붙이지 말고 Secret에만 넣어요.

## 커스터마이즈 — `feeds.json`

```jsonc
{
  "window_hours": 30,            // 이 시간 안에 올라온 글만
  "max_items_per_category": 7,   // 카테고리당 최대 기사 수
  "categories": [
    { "key": "ai", "title": "🤖 AI", "feeds": ["https://...rss"] }
  ]
}
```
카테고리 추가, 피드 교체, 이모지 변경 모두 이 파일만 고치면 돼요. 발송 시각은 `.github/workflows/daily-digest.yml`의 `cron`(UTC 기준)에서 바꿔요.

## 로컬 실행
```bash
python digest.py --dry-run                                  # 미리보기
SLACK_WEBHOOK_URLS="https://hooks.slack.com/..." python digest.py   # 실제 발송
```

## 동작 메모
- 피드 하나가 죽어도 경고만 남기고 나머지는 계속 발송해요.
- 같은 기사가 여러 피드/카테고리에 나와도 한 번만 보내요 (URL + 제목 기준).
- 새 소식이 하나도 없으면 발송하지 않아요.
- 발송이 모두 실패하면 기록을 남기지 않아서 다음 실행 때 다시 보내요.
