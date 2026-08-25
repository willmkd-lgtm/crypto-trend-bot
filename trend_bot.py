"""코인 SNS/커뮤니티 언급량 트렌드 스크리너.

1시간마다 실행되어 아래 소스를 합산 랭킹으로 집계, 상위 20개를 텔레그램으로 발송한다.

소스 (Twitter/IG/FB API는 유료·접근불가라 제외 — 대신 이미 소셜/검색 신호를
내포한 트렌딩 소스 + Reddit 직접 집계로 대체):
    - CoinGecko  /search/trending   (검색 급상승, 무료·키 불필요)
    - CoinMarketCap /trending/latest (커뮤니티 트렌딩, 무료 키 필요)
    - Reddit     최근 1시간 내 신규 글의 티커 언급량 (무료 앱 등록 필요)

각 소스에서 상위 항목에 순위 기반 점수(Borda count)를 매기고 합산한 뒤
상위 20개를 출력한다. 소스 하나가 실패해도(키 미설정 등) 나머지로 계속 집계한다.

환경변수:
    TELEGRAM_BOT_TOKEN   (필수) - macro-brief와 동일 봇 재사용 가능
    TELEGRAM_CHAT_ID     (필수) - 개인 DM용 chat id
    CMC_API_KEY          (선택) - 없으면 CoinMarketCap 소스 스킵
    REDDIT_CLIENT_ID     (선택) - 없으면 Reddit 소스 스킵
    REDDIT_CLIENT_SECRET (선택)
    COINGECKO_API_KEY    (선택) - demo 키. 없어도 동작하나 레이트리밋에 더 취약
"""
from __future__ import annotations

import os
import re
import sys
import time
from collections import Counter, defaultdict
from typing import Optional

import requests

TOP_N = 20
CG_BASE = "https://api.coingecko.com/api/v3"
CMC_BASE = "https://pro-api.coinmarketcap.com/v1"
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

REDDIT_SUBS = [
    "CryptoCurrency", "CryptoMoonShots", "Bitcoin", "ethereum",
    "altcoin", "SatoshiStreetBets", "binance",
]
REDDIT_WINDOW_SEC = 70 * 60  # 실행 주기(1h)보다 살짝 여유

# 상위시총 코인과 겹치는 흔한 영단어(오탐 방지용 제외 목록)
REDDIT_TICKER_STOPWORDS = {
    "ONE", "ALL", "FOR", "SAFE", "MOVE", "TOP", "REAL", "FAIR",
    "HOT", "NEW", "LIVE", "CORE", "EDGE", "MASK", "MATH", "BASE",
}


def _cg_headers() -> dict:
    key = os.environ.get("COINGECKO_API_KEY")
    return {"x-cg-demo-api-key": key} if key else {}


def fetch_coin_universe(top_n: int = 250) -> dict:
    """시총 상위 코인의 symbol(대문자) -> {id, name, symbol, change_24h} 매핑."""
    try:
        r = requests.get(
            f"{CG_BASE}/coins/markets",
            params={
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": top_n,
                "page": 1,
                "price_change_percentage": "24h",
            },
            headers=_cg_headers(),
            timeout=20,
        )
        r.raise_for_status()
        universe = {}
        for c in r.json():
            sym = c["symbol"].upper()
            # 같은 심볼로 여러 코인이 있으면 시총 1위(먼저 나온 것)만 채택
            if sym not in universe:
                universe[sym] = {
                    "id": c["id"],
                    "name": c["name"],
                    "symbol": sym,
                    "change_24h": c.get("price_change_percentage_24h"),
                }
        return universe
    except Exception as e:
        print(f"[coin_universe] 실패: {e}", file=sys.stderr)
        return {}


def fetch_coingecko_trending() -> list[str]:
    """반환: symbol(대문자) 리스트, 순위순."""
    try:
        r = requests.get(f"{CG_BASE}/search/trending", headers=_cg_headers(), timeout=20)
        r.raise_for_status()
        coins = r.json().get("coins", [])
        return [c["item"]["symbol"].upper() for c in coins]
    except Exception as e:
        print(f"[coingecko_trending] 실패: {e}", file=sys.stderr)
        return []


def fetch_cmc_trending(limit: int = 20) -> list[str]:
    api_key = os.environ.get("CMC_API_KEY")
    if not api_key:
        print("[cmc_trending] CMC_API_KEY 없음 — 스킵", file=sys.stderr)
        return []
    try:
        r = requests.get(
            f"{CMC_BASE}/cryptocurrency/trending/latest",
            headers={"X-CMC_PRO_API_KEY": api_key},
            params={"limit": limit},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json().get("data", [])
        return [d["symbol"].upper() for d in data]
    except Exception as e:
        print(f"[cmc_trending] 실패: {e}", file=sys.stderr)
        return []


def fetch_reddit_mentions(universe: dict) -> list[str]:
    """반환: 언급량 많은 순 symbol 리스트."""
    client_id = os.environ.get("REDDIT_CLIENT_ID")
    client_secret = os.environ.get("REDDIT_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("[reddit_mentions] REDDIT_CLIENT_ID/SECRET 없음 — 스킵", file=sys.stderr)
        return []
    try:
        import praw
    except ImportError:
        print("[reddit_mentions] praw 미설치 — 스킵", file=sys.stderr)
        return []

    try:
        reddit = praw.Reddit(
            client_id=client_id,
            client_secret=client_secret,
            user_agent="crypto-trend-bot/1.0 (hourly mention scanner)",
        )
        reddit.read_only = True

        symbols = sorted(universe.keys(), key=len, reverse=True)  # 긴 심볼 먼저 매칭
        dollar_pat = re.compile(r"\$([A-Za-z]{2,10})\b")
        name_pats = {
            sym: re.compile(r"\b" + re.escape(info["name"]) + r"\b", re.IGNORECASE)
            for sym, info in universe.items()
            if len(info["name"]) >= 4
        }
        bare_pats = {
            sym: re.compile(r"\b" + re.escape(sym) + r"\b")
            for sym in symbols
            if sym not in REDDIT_TICKER_STOPWORDS
        }

        cutoff = time.time() - REDDIT_WINDOW_SEC
        counts: Counter = Counter()

        for sub_name in REDDIT_SUBS:
            try:
                sub = reddit.subreddit(sub_name)
                for post in sub.new(limit=100):
                    if post.created_utc < cutoff:
                        break
                    text = f"{post.title} {post.selftext or ''}"
                    hit_syms = set()
                    for m in dollar_pat.finditer(text):
                        sym = m.group(1).upper()
                        if sym in universe:
                            hit_syms.add(sym)
                    for sym, pat in bare_pats.items():
                        if pat.search(text):
                            hit_syms.add(sym)
                    for sym, pat in name_pats.items():
                        if pat.search(text):
                            hit_syms.add(sym)
                    counts.update(hit_syms)
            except Exception as e:
                print(f"[reddit_mentions] r/{sub_name} 실패: {e}", file=sys.stderr)
                continue

        return [sym for sym, _ in counts.most_common(TOP_N)]
    except Exception as e:
        print(f"[reddit_mentions] 실패: {e}", file=sys.stderr)
        return []


def borda_scores(ranked_lists: list[list[str]]) -> dict:
    scores: defaultdict = defaultdict(float)
    for ranked in ranked_lists:
        n = len(ranked)
        for i, sym in enumerate(ranked):
            scores[sym] += (n - i)
    return scores


def build_message(top: list[tuple], sources_by_sym: dict) -> str:
    lines = ["🔥 코인 SNS/커뮤니티 언급 트렌드 TOP {}".format(len(top)), ""]
    for i, (sym, score, info) in enumerate(top, 1):
        name = info["name"] if info else sym
        chg = info.get("change_24h") if info else None
        chg_txt = f" ({chg:+.1f}%)" if isinstance(chg, (int, float)) else ""
        tags = "/".join(sources_by_sym.get(sym, []))
        lines.append(f"{i}. {name}({sym}){chg_txt} — {tags}")
    lines.append("")
    lines.append("소스: CoinGecko 트렌딩 · CoinMarketCap 트렌딩 · Reddit(최근 1h)")
    return "\n".join(lines)


def send_telegram(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 환경변수가 필요합니다.")
    url = TELEGRAM_API.format(token=token)
    r = requests.post(url, json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True}, timeout=(10, 30))
    r.raise_for_status()


def main() -> int:
    universe = fetch_coin_universe()

    cg = fetch_coingecko_trending()
    cmc = fetch_cmc_trending()
    reddit = fetch_reddit_mentions(universe) if universe else []

    active_sources = [(name, lst) for name, lst in
                       [("CG", cg), ("CMC", cmc), ("Reddit", reddit)] if lst]
    if not active_sources:
        print("모든 소스 실패 — 발송 스킵", file=sys.stderr)
        return 1

    scores = borda_scores([lst for _, lst in active_sources])
    sources_by_sym: defaultdict = defaultdict(list)
    for name, lst in active_sources:
        for sym in lst:
            sources_by_sym[sym].append(name)

    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:TOP_N]
    top = [(sym, score, universe.get(sym)) for sym, score in ranked]

    msg = build_message(top, sources_by_sym)
    print(msg)
    send_telegram(msg)
    print("sent.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
