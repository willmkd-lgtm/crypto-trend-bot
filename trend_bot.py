"""코인 언급/관심 트렌드 스크리너.

1시간마다 실행되어 아래 소스를 합산 랭킹으로 집계, 상위 20개를 텔레그램으로 발송한다.

소스 (Twitter/IG/FB API는 유료·접근불가라 제외. Reddit 공개 JSON도 2026년 기준
로그인 리다이렉트로 막혀서 OAuth 자격증명이 있을 때만 사용):
    - CoinGecko /search/trending  검색 급상승 = 리테일 관심 쏠림 (무료·키 불필요)
    - 거래량 회전율 (24h 거래량 / 시총)  = 평소 대비 이상 활동 (무료·키 불필요)
    - Reddit    최근 1시간 신규 글의 티커 언급량 (REDDIT_CLIENT_ID/SECRET 있을 때만)

각 소스의 순위에 Borda count를 매기고 합산한 뒤 상위 20개를 출력한다.
소스 하나가 실패해도(키 미설정 등) 나머지로 계속 집계한다.

환경변수:
    TELEGRAM_BOT_TOKEN   (필수) - macro-brief와 동일 봇 재사용
    TELEGRAM_CHAT_ID     (필수) - 개인 DM용 chat id
    REDDIT_CLIENT_ID     (선택) - 없으면 Reddit 소스 스킵
    REDDIT_CLIENT_SECRET (선택)
    COINGECKO_API_KEY    (선택) - demo 키. 없어도 동작하나 레이트리밋에 더 취약
    DRY_RUN              (선택) - "1"이면 텔레그램 발송 없이 출력만
"""
from __future__ import annotations

import os
import re
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

# Windows 콘솔(cp949)에서 이모지 출력 시 UnicodeEncodeError로 죽는 것 방지
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

TOP_N = 20
UNIVERSE_SIZE = 250
TURNOVER_TOP = 20
CG_BASE = "https://api.coingecko.com/api/v3"
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
KST = timezone(timedelta(hours=9))

REDDIT_SUBS = [
    "CryptoCurrency", "CryptoMoonShots", "Bitcoin", "ethereum",
    "altcoin", "SatoshiStreetBets", "binance",
]
REDDIT_WINDOW_SEC = 70 * 60  # 실행 주기(1h)보다 살짝 여유

# 상위시총 코인과 겹치는 흔한 영단어(Reddit 오탐 방지)
REDDIT_TICKER_STOPWORDS = {
    "ONE", "ALL", "FOR", "SAFE", "MOVE", "TOP", "REAL", "FAIR",
    "HOT", "NEW", "LIVE", "CORE", "EDGE", "MASK", "MATH", "BASE",
}

# 회전율 랭킹에서 뺄 것들: 스테이블·랩드·스테이킹 파생은 구조적으로 회전율이 높아
# "화제성"과 무관하게 상위를 점거한다.
DERIVATIVE_NAME_PAT = re.compile(
    r"\b(wrapped|staked|liquid staked|bridged|restaked|tokenized)\b", re.IGNORECASE
)
KNOWN_NON_TREND_SYMBOLS = {
    "USDT", "USDC", "DAI", "FDUSD", "USD1", "TUSD", "BUSD", "USDE", "PYUSD",
    "USDS", "USDD", "FRAX", "LUSD", "GUSD", "USDF", "USDG", "RLUSD", "BSC-USD",
    "WBTC", "WETH", "STETH", "WSTETH", "WEETH", "WBETH", "RETH", "CBBTC",
    "SOLVBTC", "LBTC", "JITOSOL", "MSOL", "BNSOL", "RSETH", "EZETH",
    "SUSDE", "SUSDS", "BUIDL", "WBNB", "WHYPE", "WSOL",
}


def _cg_headers() -> dict:
    key = os.environ.get("COINGECKO_API_KEY")
    return {"x-cg-demo-api-key": key} if key else {}


def _is_derivative_or_stable(info: dict) -> bool:
    """스테이블코인/랩드토큰 판정. 회전율 랭킹에서만 제외한다."""
    if info["symbol"] in KNOWN_NON_TREND_SYMBOLS:
        return True
    if DERIVATIVE_NAME_PAT.search(info["name"]):
        return True
    # 가격이 $1 근처이고 24h 변동이 거의 없으면 스테이블코인으로 간주
    price, chg = info.get("price"), info.get("change_24h")
    if price is not None and 0.98 <= price <= 1.02:
        if chg is None or abs(chg) < 0.5:
            return True
    return False


def fetch_coin_universe(top_n: int = UNIVERSE_SIZE) -> dict:
    """시총 상위 코인의 symbol(대문자) -> info 매핑."""
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
            timeout=30,
        )
        r.raise_for_status()
        universe: dict = {}
        for c in r.json():
            sym = c["symbol"].upper()
            # 같은 심볼로 여러 코인이 있으면 시총 1위(먼저 나온 것)만 채택
            if sym in universe:
                continue
            universe[sym] = {
                "id": c["id"],
                "name": c["name"],
                "symbol": sym,
                "price": c.get("current_price"),
                "market_cap": c.get("market_cap"),
                "volume_24h": c.get("total_volume"),
                "rank": c.get("market_cap_rank"),
                "change_24h": c.get("price_change_percentage_24h"),
            }
        return universe
    except Exception as e:
        print(f"[universe] 실패: {e}", file=sys.stderr)
        return {}


def fetch_coingecko_trending(universe: dict) -> list[str]:
    """검색 급상승 코인. universe에 없는 코인은 여기서 info를 채워 넣는다."""
    try:
        r = requests.get(f"{CG_BASE}/search/trending", headers=_cg_headers(), timeout=20)
        r.raise_for_status()
        ranked: list[str] = []
        for c in r.json().get("coins", []):
            item = c["item"]
            sym = item["symbol"].upper()
            if sym not in universe:
                data = item.get("data") or {}
                chg = data.get("price_change_percentage_24h")
                if isinstance(chg, dict):
                    chg = chg.get("usd")
                universe[sym] = {
                    "id": item["id"],
                    "name": item["name"],
                    "symbol": sym,
                    "price": None,
                    "market_cap": None,
                    "volume_24h": None,
                    "rank": item.get("market_cap_rank"),
                    "change_24h": chg,
                }
            ranked.append(sym)
        return ranked
    except Exception as e:
        print(f"[trending] 실패: {e}", file=sys.stderr)
        return []


def rank_by_turnover(universe: dict, top: int = TURNOVER_TOP) -> tuple[list[str], dict]:
    """24h 거래량/시총 회전율 랭킹. 반환: (ranked_symbols, {symbol: turnover})."""
    rows = []
    for sym, info in universe.items():
        mc, vol = info.get("market_cap"), info.get("volume_24h")
        if not mc or not vol:
            continue
        if _is_derivative_or_stable(info):
            continue
        rows.append((vol / mc, sym))
    rows.sort(reverse=True)
    ranked = [sym for _, sym in rows[:top]]
    turnovers = {sym: t for t, sym in rows[:top]}
    return ranked, turnovers


def fetch_reddit_mentions(universe: dict) -> list[str]:
    """최근 1시간 신규 글의 코인 언급량 순 symbol 리스트.

    Reddit 공개 JSON 엔드포인트는 로그인 리다이렉트로 막혀 있어 OAuth 필수.
    자격증명이 없으면 조용히 스킵한다.
    """
    client_id = os.environ.get("REDDIT_CLIENT_ID")
    client_secret = os.environ.get("REDDIT_CLIENT_SECRET")
    if not client_id or not client_secret:
        print("[reddit] REDDIT_CLIENT_ID/SECRET 없음 — 스킵", file=sys.stderr)
        return []
    try:
        import praw
    except ImportError:
        print("[reddit] praw 미설치 — 스킵", file=sys.stderr)
        return []

    try:
        reddit = praw.Reddit(
            client_id=client_id,
            client_secret=client_secret,
            user_agent="crypto-trend-bot/1.0 (hourly mention scanner)",
        )
        reddit.read_only = True

        dollar_pat = re.compile(r"\$([A-Za-z]{2,10})\b")
        name_pats = {
            sym: re.compile(r"\b" + re.escape(info["name"]) + r"\b", re.IGNORECASE)
            for sym, info in universe.items()
            if len(info["name"]) >= 4
        }
        bare_pats = {
            sym: re.compile(r"\b" + re.escape(sym) + r"\b")
            for sym in universe
            if sym not in REDDIT_TICKER_STOPWORDS
        }

        cutoff = time.time() - REDDIT_WINDOW_SEC
        counts: Counter = Counter()
        scanned = 0

        for sub_name in REDDIT_SUBS:
            try:
                for post in reddit.subreddit(sub_name).new(limit=100):
                    if post.created_utc < cutoff:
                        break
                    scanned += 1
                    text = f"{post.title} {post.selftext or ''}"
                    hits = set()
                    for m in dollar_pat.finditer(text):
                        sym = m.group(1).upper()
                        if sym in universe:
                            hits.add(sym)
                    for sym, pat in bare_pats.items():
                        if pat.search(text):
                            hits.add(sym)
                    for sym, pat in name_pats.items():
                        if pat.search(text):
                            hits.add(sym)
                    counts.update(hits)
            except Exception as e:
                print(f"[reddit] r/{sub_name} 실패: {e}", file=sys.stderr)
                continue

        print(f"[reddit] 최근 {REDDIT_WINDOW_SEC // 60}분 글 {scanned}건 스캔", file=sys.stderr)
        return [sym for sym, _ in counts.most_common(TOP_N)]
    except Exception as e:
        print(f"[reddit] 실패: {e}", file=sys.stderr)
        return []


def borda_merge(sources: list[tuple[str, list[str]]]) -> tuple[list[tuple[str, float]], dict]:
    """순위 기반 점수를 합산. 반환: ([(symbol, score)], {symbol: [source_labels]})."""
    scores: defaultdict = defaultdict(float)
    tags: defaultdict = defaultdict(list)
    for label, ranked in sources:
        n = len(ranked)
        for i, sym in enumerate(ranked):
            scores[sym] += (n - i) / n  # 소스별 리스트 길이 차이를 정규화
            tags[sym].append(label)
    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return ordered, tags


def build_message(top: list, universe: dict, tags: dict, turnovers: dict,
                  source_labels: list[str]) -> str:
    now = datetime.now(KST).strftime("%m-%d %H:%M")
    lines = [f"🔥 코인 관심도 트렌드 TOP {len(top)}  ({now} KST)", ""]
    for i, (sym, _score) in enumerate(top, 1):
        info = universe.get(sym, {})
        name = info.get("name", sym)
        chg = info.get("change_24h")
        chg_txt = f"  {chg:+.1f}%" if isinstance(chg, (int, float)) else ""
        rank = info.get("rank")
        rank_txt = f" #{rank}" if rank else ""
        lines.append(f"{i}. {name} ({sym}){rank_txt}{chg_txt}")

        why = list(tags.get(sym, []))
        if sym in turnovers:
            why = [w if w != "거래량" else f"거래량 {turnovers[sym]:.2f}x" for w in why]
        lines.append(f"    └ {' · '.join(why)}")
    lines += ["", f"소스: {' + '.join(source_labels)}"]
    return "\n".join(lines)


def send_telegram(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise RuntimeError("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 환경변수가 필요합니다.")
    r = requests.post(
        TELEGRAM_API.format(token=token),
        json={"chat_id": chat_id, "text": text, "disable_web_page_preview": True},
        timeout=(10, 30),
    )
    r.raise_for_status()


def main() -> int:
    universe = fetch_coin_universe()
    if not universe:
        print("시세 데이터를 못 받아옴 — 발송 스킵", file=sys.stderr)
        return 1

    trending = fetch_coingecko_trending(universe)
    turnover_ranked, turnovers = rank_by_turnover(universe)
    reddit_ranked = fetch_reddit_mentions(universe)

    sources = [(label, lst) for label, lst in [
        ("검색트렌드", trending),
        ("거래량", turnover_ranked),
        ("Reddit", reddit_ranked),
    ] if lst]

    if not sources:
        print("모든 소스 실패 — 발송 스킵", file=sys.stderr)
        return 1

    ordered, tags = borda_merge(sources)
    top = ordered[:TOP_N]

    msg = build_message(top, universe, tags, turnovers, [s[0] for s in sources])
    print(msg)

    if os.environ.get("DRY_RUN") == "1":
        print("\n[DRY_RUN] 텔레그램 발송 생략", file=sys.stderr)
        return 0

    send_telegram(msg)
    print("sent.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
