#!/usr/bin/env python3
"""
AI / 보안 뉴스 브리핑 수집기
─────────────────────────────
PC가 꺼져 있어도 GitHub Actions에서 매일 자동 실행됩니다.

동작:
  1) RSS 피드 수집 (무료, 무제한)
  2) 이미 처리한 기사 제외 (seen.json)
  3) 본문 추출
  4) map  : 기사별 개별 요약 (무료 LLM, 컨텍스트 8K 대응)
  5) reduce: 중요도 상위 항목만 골라 브리핑 생성
  6) docs/data/<날짜>.json + latest.json 저장
  7) Discord 웹훅 알림 (선택)

사용:
  python collect.py              # 정상 실행
  python collect.py --dry-run    # LLM 호출 없이 수집만 (무료 테스트)
  python collect.py --no-notify  # Discord 알림 생략
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import feedparser
import requests
import yaml

ROOT = Path(__file__).parent
DATA_DIR = ROOT / "docs" / "data"
SEEN_PATH = ROOT / "seen.json"
KST = timezone(timedelta(hours=9))

UA = "Mozilla/5.0 (compatible; personal-news-bot/1.0)"

# ──────────────────────────────────────────────────────────────
# LLM 프로바이더 폴백 체인
#   위에서부터 시도하고 실패하면 다음으로 넘어갑니다.
#   전부 무료 티어이며 API 키가 없는 항목은 자동으로 건너뜁니다.
# ──────────────────────────────────────────────────────────────
PROVIDERS = [
    {
        "name": "cerebras",
        "base_url": "https://api.cerebras.ai/v1",
        "key_env": "CEREBRAS_API_KEY",
        "model": "gpt-oss-120b",
        "ctx": 8000,          # 무료 티어 컨텍스트 제한
        "rpm": 30,            # 분당 요청 제한
    },
    {
        "name": "groq",
        "base_url": "https://api.groq.com/openai/v1",
        "key_env": "GROQ_API_KEY",
        "model": "llama-3.3-70b-versatile",
        "ctx": 32000,
        "rpm": 30,
    },
    {
        "name": "ollama",   # 집 PC가 켜져 있을 때만 (선택)
        "base_url": os.getenv("OLLAMA_URL", "http://localhost:11434/v1"),
        "key_env": None,
        "model": "qwen3.5:9b",
        "ctx": 16000,
        "rpm": 1000,
    },
]


# ══════════════════════════════════════════════════════════════
# 유틸
# ══════════════════════════════════════════════════════════════
def log(msg: str) -> None:
    print(f"[{datetime.now(KST):%H:%M:%S}] {msg}", flush=True)


def uid(url: str) -> str:
    return hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]


def clean_text(html: str) -> str:
    """태그 제거 + 공백 정리 (본문 추출 실패 시 폴백용)"""
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;?", " ", text)
    text = re.sub(r"&amp;?", "&", text)
    text = re.sub(r"&#\d+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


_KW_CACHE: dict[str, re.Pattern] = {}


def kw_match(keyword: str, haystack: str) -> bool:
    """단어 경계 기준 매칭.

    단순 부분 문자열 매칭을 쓰면 'source'가 'RCE'에, 'management'가 'agent'에
    걸립니다. 앞뒤가 단어 문자가 아닐 때만 매칭시킵니다.
    """
    pat = _KW_CACHE.get(keyword)
    if pat is None:
        pat = re.compile(rf"(?<!\w){re.escape(keyword)}(?!\w)", re.IGNORECASE)
        _KW_CACHE[keyword] = pat
    return bool(pat.search(haystack))


def load_seen() -> dict[str, str]:
    if SEEN_PATH.exists():
        try:
            return json.loads(SEEN_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_seen(seen: dict[str, str]) -> None:
    """오래된 기록은 정리 (최근 2000건 유지)"""
    if len(seen) > 2000:
        items = sorted(seen.items(), key=lambda kv: kv[1], reverse=True)[:2000]
        seen = dict(items)
    SEEN_PATH.write_text(json.dumps(seen, ensure_ascii=False, indent=0), encoding="utf-8")


# ══════════════════════════════════════════════════════════════
# 1) 수집
# ══════════════════════════════════════════════════════════════
def fetch_feed(feed: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        parsed = feedparser.parse(feed["url"], agent=UA)
    except Exception as e:
        log(f"  ✗ {feed['name']}: {e}")
        return []

    if getattr(parsed, "bozo", False) and not parsed.entries:
        log(f"  ✗ {feed['name']}: 파싱 실패")
        return []

    out = []
    for entry in parsed.entries[: feed.get("max", 6)]:
        link = entry.get("link") or ""
        if not link:
            continue
        raw_summary = entry.get("summary") or entry.get("description") or ""
        published = (
            entry.get("published")
            or entry.get("updated")
            or datetime.now(timezone.utc).isoformat()
        )
        out.append(
            {
                "id": uid(link),
                "title": clean_text(entry.get("title") or "(제목 없음)"),
                "url": link,
                "source": feed["name"],
                "category": feed["category"],
                "weight": float(feed.get("weight", 1.0)),
                "published": str(published),
                "rss_summary": clean_text(raw_summary)[:1500],
            }
        )
    log(f"  ✓ {feed['name']}: {len(out)}건")
    return out


def collect_all(feeds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    log("RSS 수집 시작")
    articles: list[dict[str, Any]] = []
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        for result in ex.map(fetch_feed, feeds):
            articles.extend(result)
    # URL 기준 중복 제거
    dedup = {a["id"]: a for a in articles}
    log(f"수집 완료: {len(dedup)}건 (중복 제거 후)")
    return list(dedup.values())


# ══════════════════════════════════════════════════════════════
# 2) 본문 추출
# ══════════════════════════════════════════════════════════════
def extract_body(article: dict[str, Any], char_limit: int) -> str:
    """trafilatura가 있으면 사용, 없거나 실패하면 RSS 요약으로 폴백"""
    try:
        import trafilatura  # 선택 의존성

        resp = requests.get(article["url"], timeout=15, headers={"User-Agent": UA})
        resp.raise_for_status()
        body = trafilatura.extract(
            resp.text, include_comments=False, include_tables=False
        )
        if body and len(body) > 200:
            return body[:char_limit]
    except Exception:
        pass
    return article["rss_summary"][:char_limit]


# ══════════════════════════════════════════════════════════════
# 3) LLM 호출 (OpenAI 호환 + 폴백 체인)
# ══════════════════════════════════════════════════════════════
class LLM:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run
        self.active: dict[str, Any] | None = None
        self.clients: list[tuple[dict, Any]] = []
        self._last_call = 0.0

        if dry_run:
            log("DRY-RUN 모드: LLM 호출을 건너뜁니다")
            return

        try:
            import openai
        except ImportError:
            log("openai 패키지 없음 → dry-run으로 전환")
            self.dry_run = True
            return

        for p in PROVIDERS:
            key = os.getenv(p["key_env"], "") if p["key_env"] else "local"
            if not key:
                continue
            self.clients.append(
                (p, openai.OpenAI(api_key=key, base_url=p["base_url"], timeout=90))
            )
        if not self.clients:
            log("사용 가능한 API 키가 없습니다 → dry-run으로 전환")
            self.dry_run = True
        else:
            names = ", ".join(p["name"] for p, _ in self.clients)
            log(f"LLM 폴백 체인: {names}")

    def _throttle(self, rpm: int) -> None:
        gap = 60.0 / max(rpm, 1)
        wait = gap - (time.time() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._last_call = time.time()

    def chat(self, system: str, user: str, max_tokens: int = 800) -> str | None:
        if self.dry_run:
            return None
        for provider, client in self.clients:
            try:
                self._throttle(provider["rpm"])
                resp = client.chat.completions.create(
                    model=provider["model"],
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    temperature=0.3,
                    max_tokens=max_tokens,
                )
                return resp.choices[0].message.content
            except Exception as e:
                log(f"  ! {provider['name']} 실패 ({type(e).__name__}) → 폴백")
                continue
        return None


def parse_json_loose(text: str) -> dict | None:
    """LLM이 코드펜스나 잡담을 섞어도 JSON을 건져냅니다"""
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        return None
    try:
        return json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None


# ══════════════════════════════════════════════════════════════
# 4) map — 기사별 요약
# ══════════════════════════════════════════════════════════════
MAP_SYSTEM = """너는 AI/보안 뉴스 분석가다. 반드시 아래 규칙을 지켜라.

규칙:
1. 제공된 본문에 명시된 내용만 사용한다. 본문에 없는 사실, 수치, 인용은 절대 만들어내지 않는다.
2. 요약은 한국어로 쓴다. 고유명사·제품명·CVE 번호는 원문 그대로 둔다.
3. 정보가 부족하면 importance를 1로 준다.
4. JSON 객체 하나만 출력한다. 설명이나 코드펜스를 붙이지 않는다.

출력 형식:
{"summary": "2문장 이내 한국어 요약", "importance": 1-5, "why": "왜 중요한지 한 문장"}

importance 기준:
5 = 실제 악용 중인 취약점, 대규모 침해, 판도를 바꾸는 모델/제품 출시
4 = 주요 벤더의 중대 패치, 중요한 신모델·연구 공개
3 = 일반적으로 알아둘 만한 업계 소식
2 = 부수적 소식
1 = 홍보성이거나 내용 부족"""


def summarize_one(llm: LLM, article: dict, char_limit: int, watchlist: list[str]) -> dict:
    body = extract_body(article, char_limit)
    article["body_chars"] = len(body)

    user = (
        f"제목: {article['title']}\n"
        f"출처: {article['source']}\n"
        f"본문:\n{body}"
    )
    raw = llm.chat(MAP_SYSTEM, user, max_tokens=400)
    parsed = parse_json_loose(raw) if raw else None

    if parsed:
        article["summary"] = str(parsed.get("summary", ""))[:400]
        article["why"] = str(parsed.get("why", ""))[:200]
        try:
            base = float(parsed.get("importance", 2))
        except (TypeError, ValueError):
            base = 2.0
    else:
        # 폴백: 추출 요약 (LLM 없이도 대시보드가 비지 않도록)
        article["summary"] = (body or article["rss_summary"])[:300]
        article["why"] = ""
        base = 2.0

    # 워치리스트 키워드 보너스 (단어 경계 기준)
    haystack = f"{article['title']} {article['summary']}"
    hits = [k for k in watchlist if kw_match(k, haystack)]
    article["matched"] = hits
    score = base * article["weight"] + (1.0 if hits else 0.0)
    article["importance"] = round(min(score, 6.0), 2)
    return article


# ══════════════════════════════════════════════════════════════
# 5) reduce — 최종 브리핑
# ══════════════════════════════════════════════════════════════
REDUCE_SYSTEM = """너는 개인 비서다. 아래 기사 요약들을 바탕으로 오늘의 브리핑 한 단락을 쓴다.

규칙:
1. 제공된 요약에 없는 내용은 절대 추가하지 않는다.
2. 한국어로 3~4문장. 오늘 흐름에서 가장 중요한 것이 무엇이고 왜 그런지 짚어준다.
3. 특정 기사를 언급할 때는 제목을 그대로 쓴다.
4. 인사말이나 머리말 없이 본문만 출력한다."""


def build_briefing(llm: LLM, picked: list[dict]) -> str:
    if not picked:
        return "오늘은 새로 수집된 기사가 없습니다."
    lines = [
        f"- [{a['category'].upper()}] {a['title']} — {a['summary']}"
        for a in picked
    ]
    raw = llm.chat(REDUCE_SYSTEM, "\n".join(lines), max_tokens=600)
    if raw:
        return raw.strip()
    top = picked[0]
    return f"오늘 {len(picked)}건이 수집되었습니다. 가장 주목할 항목은 「{top['title']}」입니다."


# ══════════════════════════════════════════════════════════════
# 6) Discord 알림
# ══════════════════════════════════════════════════════════════
def notify_discord(payload: dict) -> None:
    url = os.getenv("DISCORD_WEBHOOK_URL", "")
    if not url:
        log("DISCORD_WEBHOOK_URL 미설정 → 알림 생략")
        return

    site = os.getenv("DASHBOARD_URL", "")
    embeds = []
    for cat, emoji, label in (("security", "🔐", "Security"), ("ai", "🤖", "AI")):
        items = [a for a in payload["items"] if a["category"] == cat]
        if not items:
            continue
        desc = "\n".join(
            f"**{i+1}. [{a['title'][:90]}]({a['url']})**\n{a['summary'][:160]}"
            for i, a in enumerate(items)
        )
        embeds.append(
            {
                "title": f"{emoji} {label}",
                "description": desc[:4000],
                "color": 0xE74C3C if cat == "security" else 0x3498DB,
            }
        )

    body = {
        "content": (
            f"## 📰 {payload['date']} 브리핑\n{payload['briefing'][:1200]}"
            + (f"\n\n🔗 <{site}>" if site else "")
        )[:2000],
        "embeds": embeds[:10],
    }
    try:
        r = requests.post(url, json=body, timeout=20)
        r.raise_for_status()
        log("Discord 알림 전송 완료")
    except Exception as e:
        log(f"Discord 알림 실패: {e}")


# ══════════════════════════════════════════════════════════════
# main
# ══════════════════════════════════════════════════════════════
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="LLM 호출 없이 수집만")
    ap.add_argument("--no-notify", action="store_true", help="Discord 알림 생략")
    ap.add_argument("--ignore-seen", action="store_true", help="중복 필터 무시")
    args = ap.parse_args()

    cfg = yaml.safe_load((ROOT / "feeds.yaml").read_text(encoding="utf-8"))
    limits = cfg.get("limits", {})
    watchlist = cfg.get("watchlist", [])
    char_limit = int(limits.get("article_char_limit", 5000))

    # 1) 수집
    articles = collect_all(cfg["feeds"])

    # 2) 중복 제거
    seen = load_seen()
    if not args.ignore_seen:
        before = len(articles)
        articles = [a for a in articles if a["id"] not in seen]
        log(f"신규 기사: {len(articles)}건 (기처리 {before - len(articles)}건 제외)")

    if not articles:
        log("새 기사가 없습니다. 종료합니다.")
        return 0

    # 3) 요약 대상 제한
    cap = int(limits.get("max_articles_to_summarize", 40))
    articles.sort(key=lambda a: a["weight"], reverse=True)
    articles = articles[:cap]

    # 4) map
    llm = LLM(dry_run=args.dry_run)
    log(f"요약 시작: {len(articles)}건")
    summarized = []
    for i, art in enumerate(articles, 1):
        summarized.append(summarize_one(llm, art, char_limit, watchlist))
        if i % 10 == 0:
            log(f"  진행 {i}/{len(articles)}")
    log("요약 완료")

    # 5) 중요도 상위 선별
    picked: list[dict] = []
    for cat in ("security", "ai"):
        pool = sorted(
            (a for a in summarized if a["category"] == cat),
            key=lambda a: a["importance"],
            reverse=True,
        )
        picked.extend(pool[: int(limits.get(cat, 5))])

    # 6) reduce
    briefing = build_briefing(llm, picked)

    # 7) 저장
    now = datetime.now(KST)
    payload = {
        "date": now.strftime("%Y-%m-%d"),
        "generated_at": now.isoformat(),
        "briefing": briefing,
        "stats": {
            "collected": len(articles),
            "summarized": len(summarized),
            "picked": len(picked),
            "llm": "dry-run" if llm.dry_run else "live",
        },
        "items": [
            {
                k: a[k]
                for k in (
                    "id", "title", "url", "source", "category",
                    "summary", "why", "importance", "matched", "published",
                )
            }
            for a in picked
        ],
        "all_items": [
            {
                k: a[k]
                for k in ("id", "title", "url", "source", "category",
                          "summary", "importance", "matched", "published")
            }
            for a in sorted(summarized, key=lambda x: x["importance"], reverse=True)
        ],
    }

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / f"{payload['date']}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (DATA_DIR / "latest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 날짜 인덱스 갱신 (대시보드 날짜 선택용)
    dates = sorted(
        (p.stem for p in DATA_DIR.glob("*.json") if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p.stem)),
        reverse=True,
    )
    (DATA_DIR / "index.json").write_text(
        json.dumps({"dates": dates[:180]}, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # 8) seen 갱신
    stamp = now.isoformat()
    for a in summarized:
        seen[a["id"]] = stamp
    save_seen(seen)

    # 9) 알림
    if not args.no_notify:
        notify_discord(payload)

    log(f"완료 — 선별 {len(picked)}건 / 전체 {len(summarized)}건")
    return 0


if __name__ == "__main__":
    sys.exit(main())
