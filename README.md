# 📰 My Briefing — AI & 보안 뉴스 개인 비서

**내 PC가 꺼져 있어도 매일 아침 자동으로 도는** AI·보안 뉴스 브리핑.
서버 없음. 비용 $0. 카드 등록 없음.

```
GitHub Actions (매일 07:00 KST, 상시 무료)
   │
   ├─ RSS 12개 수집 ────────────── 무료·무제한
   ├─ 기사별 요약 (map) ────────── Cerebras 무료 gpt-oss-120b
   ├─ 브리핑 생성 (reduce) ─────── 실패 시 Groq → 로컬 Ollama 폴백
   ├─ docs/data/*.json 커밋
   └─ Discord 웹훅 알림
              │
              ▼
   GitHub Pages 대시보드 (정적, 상시 무료)
```

---

## 왜 이 구조인가

PC를 상시 켜둘 수 없다는 조건이 설계를 결정했습니다.

| 후보 | 판정 |
|---|---|
| 집 PC + cron | ❌ PC가 꺼지면 멈춤 |
| Oracle Cloud Always Free | ⚠️ 2026년 6월 4 OCPU/24GB → **2 OCPU/12GB로 반토막**, 유휴 시 회수 정책 있음 |
| Cloudflare Workers | ⚠️ 무료 플랜 **CPU 10ms 제한** — RSS 12개 파싱엔 빠듯 |
| **GitHub Actions** | ✅ **공개 저장소는 실행 시간 무제한**, CPU 제한 없음, 상시 가동 |

> ⚠️ **공개(public) 저장소로 만드세요.** 비공개 저장소는 무료 2,000분/월 제한이 있고, 무료 플랜에서 **스케줄 워크플로가 조용히 실행되지 않는 알려진 문제**가 있습니다. API 키는 코드가 아니라 GitHub Secrets에 들어가므로 공개해도 안전합니다.

---

## 설치 (약 20분, 전부 무료)

### 1. 저장소 만들기

```bash
git init && git add . && git commit -m "init"
gh repo create my-briefing --public --source=. --push
# 또는 GitHub 웹에서 public 저장소 생성 후 push
```

### 2. API 키 발급 (카드 불필요)

| 서비스 | 발급처 | 무료 한도 |
|---|---|---|
| **Cerebras** (주력) | [cloud.cerebras.ai](https://cloud.cerebras.ai) | 100만 토큰/일, 30 RPM |
| **Groq** (폴백) | [console.groq.com](https://console.groq.com) | llama-3.3-70b 1,000회/일 |

하루 사용량은 약 5만 토큰 — **Cerebras 무료 한도의 5%**입니다. 여유가 큽니다.

### 3. Discord 웹훅 (선택)

Discord 서버 → 채널 설정 → 연동 → 웹훅 → **웹훅 URL 복사**
봇을 만들 필요 없이 URL 하나면 됩니다.

### 4. GitHub Secrets 등록

저장소 → Settings → Secrets and variables → Actions → **New repository secret**

| 이름 | 값 |
|---|---|
| `CEREBRAS_API_KEY` | Cerebras 키 |
| `GROQ_API_KEY` | Groq 키 |
| `DISCORD_WEBHOOK_URL` | 웹훅 URL (선택) |

그리고 같은 화면의 **Variables** 탭에서:

| 이름 | 값 |
|---|---|
| `DASHBOARD_URL` | `https://<아이디>.github.io/my-briefing/` |

### 5. GitHub Pages 켜기

Settings → Pages → Source: **Deploy from a branch** → Branch: `main` / 폴더: **`/docs`** → Save

### 6. 첫 실행

Actions 탭 → **Daily News Briefing** → **Run workflow** 버튼

> 💡 새 저장소는 첫 수동 실행을 해줘야 이후 스케줄이 활성화되는 경우가 있습니다. 반드시 한 번 눌러주세요.

몇 분 뒤 `https://<아이디>.github.io/my-briefing/` 에서 확인할 수 있습니다.

---

## 로컬에서 미리 보기

```bash
pip install -r requirements.txt

# LLM 없이 수집만 (API 키 불필요, 완전 무료)
python collect.py --dry-run --no-notify

# 대시보드 확인
python -m http.server 8000 --directory docs
# → http://localhost:8000
```

`--dry-run`은 LLM을 호출하지 않고 RSS 요약을 그대로 씁니다. 피드가 살아있는지, 대시보드가 잘 나오는지 확인할 때 쓰세요. 대시보드 상단에 `⚠ DRY-RUN` 표시가 뜹니다.

### 실행 옵션

| 옵션 | 설명 |
|---|---|
| `--dry-run` | LLM 호출 없이 수집만 |
| `--no-notify` | Discord 알림 생략 |
| `--ignore-seen` | 중복 필터 무시 (재처리) |

---

## 커스터마이징

### 뉴스 소스 — `feeds.yaml`

```yaml
- name: 내가 보는 블로그
  url: https://example.com/feed.xml
  category: ai        # ai | security
  weight: 1.3         # 중요도 가중치
  max: 5              # 이 피드에서 가져올 최대 개수
```

### 관심 키워드 — `feeds.yaml`의 `watchlist`

**여기가 이 봇의 핵심 가치입니다.** 본인이 실제로 쓰는 스택 이름을 넣어두면, 그 제품에 취약점이 터졌을 때 놓치지 않습니다.

```yaml
watchlist:
  - Nginx
  - PostgreSQL
  - Cloudflare
  - Next.js
  - zero-day
  - actively exploited
```

매칭되면 중요도 +1 되고 대시보드에 노란 배지가 붙습니다.
매칭은 **단어 경계 기준**이라 `source`가 `RCE`에 걸리는 오탐이 없습니다.

### 실행 시각 — `.github/workflows/daily.yml`

```yaml
- cron: "0 22 * * *"   # UTC 기준. 22:00 UTC = 07:00 KST
```

> GitHub cron은 부하에 따라 **5~20분 지연**될 수 있습니다. 정상 동작입니다.

### 브리핑 항목 수 — `feeds.yaml`의 `limits`

```yaml
limits:
  ai: 5          # AI 카테고리에서 뽑을 개수
  security: 5
  max_articles_to_summarize: 40   # LLM에 보낼 최대 기사 수
  article_char_limit: 5000        # 본문 자르기 (무료 티어 8K 컨텍스트 대응)
```

---

## 설계 노트

### 왜 map-reduce인가

Cerebras 무료 티어는 컨텍스트가 **8K**입니다. 기사 40개를 한 번에 넣을 수 없습니다.
그래서 **① 기사 1개씩 개별 요약 → ② 요약본만 모아 브리핑 생성** 2단계로 나눴습니다.
덕분에 무료 티어로도 돌아가고, 로컬 8GB GPU로 갈아타도 그대로 작동합니다.

### 환각 대응

무료 소형 모델은 환각이 잦습니다. 세 겹으로 막았습니다.

1. 기사 본문을 반드시 컨텍스트에 넣고 **"본문에 없는 내용 금지"**를 시스템 프롬프트에 명시
2. 모든 항목에 **원문 링크 필수** — 의심되면 1초 만에 확인 가능
3. LLM이 JSON을 안 뱉거나 전부 실패해도 **추출 요약으로 폴백** → 대시보드가 비지 않음

### 폴백 체인

`Cerebras → Groq → 로컬 Ollama` 순으로 시도합니다.
한도 소진이나 장애가 나도 브리핑이 멈추지 않습니다.
집 PC가 켜져 있고 Ollama가 돌고 있으면 마지막 폴백으로 자동 사용됩니다.

```bash
# 로컬 폴백을 쓰려면 (선택, RTX 3070 8GB에 적합)
ollama pull qwen3.5:9b
```

---

## 다음에 붙일 것들

- [ ] **챗 기능** — Cloudflare Worker로 Groq 프록시 (무료 10만 요청/일, API 키 은닉). I/O 프록시라 CPU 10ms 제한에 안 걸립니다
- [ ] **CISA KEV 즉시 알림** — 일일 브리핑과 별개로, 신규 등재 시 바로 Discord 알림
- [ ] **주간 다이제스트** — 일요일에 한 주 요약
- [ ] **PWA 전환** — 폰 홈 화면에 추가, 오프라인 캐시
- [ ] **읽음 표시 / 북마크** — localStorage로 충분

---

## 파일 구조

```
.
├── collect.py              # 수집·요약 코어
├── feeds.yaml              # 뉴스 소스 + 관심 키워드
├── requirements.txt
├── seen.json               # 중복 방지 (자동 생성/커밋)
├── .github/workflows/
│   └── daily.yml           # 매일 07:00 KST 자동 실행
└── docs/                   # GitHub Pages 루트
    ├── index.html          # 대시보드 (의존성 0, 빌드 불필요)
    └── data/
        ├── latest.json     # 최신 브리핑
        ├── index.json      # 날짜 목록
        └── YYYY-MM-DD.json # 일자별 아카이브
```

---

## 문제 해결

| 증상 | 원인 / 해결 |
|---|---|
| 스케줄이 안 돎 | 저장소가 **public**인지 확인. 첫 수동 실행(Run workflow)을 한 번 해줘야 활성화됨 |
| 60일 후 멈춤 | 커밋이 없으면 GitHub이 스케줄 워크플로를 자동 비활성화. 이 봇은 매일 커밋하므로 해당 없음 |
| 대시보드가 비어 있음 | Actions를 아직 안 돌렸거나, Pages 폴더가 `/docs`가 아님 |
| 요약이 원문 그대로임 | API 키 미설정 → dry-run 폴백 상태. 대시보드 상단 `⚠ DRY-RUN` 확인 |
| 특정 피드 0건 | 해당 사이트가 RSS를 바꿨거나 봇을 차단. 로그의 `✗` 표시 확인 후 `feeds.yaml`에서 교체 |
| Actions 실행 시간 초과 | `max_articles_to_summarize`를 20으로 낮추기 |

---

## 비용

**$0.** 전부 무료 티어 안에서 돌아갑니다.

| 항목 | 사용량 | 무료 한도 |
|---|---|---|
| GitHub Actions | 약 5분/일 | 공개 저장소 무제한 |
| GitHub Pages | 정적 파일 | 무료 |
| Cerebras | 약 5만 토큰/일 | 100만 토큰/일 |
| RSS | 무제한 | 한도 없음 |
| Discord 웹훅 | 1회/일 | 무료 |
