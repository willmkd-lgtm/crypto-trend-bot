"""트렌드 소스별 수집기.

각 수집기는 (ranked_symbols, extra) 를 돌려준다.
ranked_symbols 는 관심도 높은 순 심볼 리스트, extra 는 표시용 부가정보.
실패하면 예외를 던지지 않고 빈 리스트를 돌려준다 — 한 소스가 죽어도 나머지로 집계한다.
"""
from __future__ import annotations

import html
import os
import re
import sys
import time
from collections import Counter
from typing import Optional

import requests

CG_BASE = "https://api.coingecko.com/api/v3"
TOP_N = 20

# 이 봇은 개인용 집계 도구다. 정직하게 신원을 밝히고 4시간에 1회만 요청한다.
HTTP_UA = "crypto-trend-bot/1.0 (personal crypto trend aggregator; low-volume)"

# ── 오탐 방지 ────────────────────────────────────────────────────────────
# 심볼/코인명이 흔한 영단어와 겹치면 일반 문장에서 무차별로 걸린다.
# 예: JUST(JST), CASH(CASH), REAL(REAL), MOVE(MOVE) → 실제 언급이 아닌데 잡힘.
COMMON_WORDS = {
    "A", "ABOUT", "ALL", "AM", "AN", "AND", "ANY", "ARE", "AS", "AT", "BACK",
    "BAD", "BE", "BEST", "BIG", "BOTH", "BUY", "BY", "CALL", "CAN", "CASH",
    "COIN", "COME", "CORE", "DAY", "DID", "DO", "DOES", "DOWN", "EACH", "EDGE",
    "END", "EVEN", "FAIR", "FAR", "FEW", "FIRE", "FIRST", "FOR", "FREE", "FROM",
    "FUN", "GAS", "GET", "GIVE", "GO", "GOOD", "GOT", "HAS", "HAVE", "HE",
    "HERE", "HIGH", "HIS", "HIT", "HOLD", "HOME", "HOPE", "HOT", "HOW", "I",
    "IF", "IN", "INTO", "IS", "IT", "ITS", "JUST", "KEEP", "KEY", "KIND",
    "LAND", "LAST", "LEFT", "LESS", "LIFE", "LIKE", "LINK", "LIVE", "LONG",
    "LOOK", "LOSS", "LOT", "LOVE", "LOW", "MAKE", "MAN", "MANY", "MASK",
    "MATH", "MAY", "ME", "MEME", "MIND", "MINE", "MOON", "MORE", "MOST",
    "MOVE", "MUCH", "MUST", "MY", "NAME", "NEAR", "NEED", "NEW", "NEXT",
    "NFT", "NO", "NOT", "NOW", "OF", "OFF", "OK", "OLD", "ON", "ONE", "ONLY",
    "OP", "OR", "OTHER", "OUR", "OUT", "OVER", "OWN", "PART", "PAY", "PEOPLE",
    "PLAY", "POST", "PUMP", "PUT", "REAL", "RIGHT", "RISE", "RUN", "SAFE",
    "SAID", "SAME", "SEE", "SELL", "SEND", "SHE", "SHOW", "SIDE", "SO", "SOME",
    "SOON", "STAY", "STILL", "STOP", "SUCH", "SURE", "TAKE", "TALK", "TEAM",
    "TELL", "THAN", "THAT", "THE", "THEIR", "THEM", "THEN", "THERE", "THESE",
    "THEY", "THIS", "TIME", "TO", "TOO", "TOP", "TRUE", "TRUST", "TRY", "TWO",
    "UP", "US", "USE", "VERY", "WANT", "WAS", "WAY", "WE", "WELL", "WERE",
    "WHAT", "WHEN", "WHERE", "WHICH", "WHO", "WHY", "WILL", "WIN", "WITH",
    "WORK", "WORLD", "WOULD", "YOU", "YOUR",
}


def _cg_headers() -> dict:
    key = os.environ.get("COINGECKO_API_KEY")
    return {"x-cg-demo-api-key": key} if key else {}


def build_matchers(universe: dict) -> tuple[re.Pattern, dict, dict]:
    """언급 탐지용 정규식 3종.

    - cashtag: $BTC 표기 (가장 신뢰도 높음)
    - bare:    맨 티커. 흔한 영단어는 제외
    - name:    코인 전체 이름. 흔한 영단어이거나 너무 짧으면 제외
    """
    cashtag = re.compile(r"\$([A-Za-z][A-Za-z0-9]{1,9})\b")
    bare = {
        sym: re.compile(r"\b" + re.escape(sym) + r"\b")
        for sym in universe
        if sym not in COMMON_WORDS and len(sym) >= 3
    }
    name = {}
    for sym, info in universe.items():
        n = info["name"]
        if len(n) < 4 or n.upper() in COMMON_WORDS:
            continue
        name[sym] = re.compile(r"\b" + re.escape(n) + r"\b", re.IGNORECASE)
    return cashtag, bare, name


def find_mentions(text: str, universe: dict, matchers: tuple) -> set:
    """한 덩어리 텍스트에서 언급된 심볼 집합."""
    cashtag, bare, name = matchers
    hits = set()
    for m in cashtag.finditer(text):
        sym = m.group(1).upper()
        if sym in universe:
            hits.add(sym)
    for sym, pat in bare.items():
        if pat.search(text):
            hits.add(sym)
    for sym, pat in name.items():
        if pat.search(text):
            hits.add(sym)
    return hits


# ── 1. CoinGecko 시세 유니버스 ───────────────────────────────────────────

def fetch_universe(top_n: int = 250) -> dict:
    try:
        r = requests.get(
            f"{CG_BASE}/coins/markets",
            params={"vs_currency": "usd", "order": "market_cap_desc",
                    "per_page": top_n, "page": 1,
                    "price_change_percentage": "24h"},
            headers=_cg_headers(), timeout=30,
        )
        r.raise_for_status()
        uni: dict = {}
        for c in r.json():
            sym = c["symbol"].upper()
            if sym in uni:          # 같은 심볼이면 시총 상위만
                continue
            uni[sym] = {
                "id": c["id"], "name": c["name"], "symbol": sym,
                "price": c.get("current_price"),
                "market_cap": c.get("market_cap"),
                "volume_24h": c.get("total_volume"),
                "rank": c.get("market_cap_rank"),
                "change_24h": c.get("price_change_percentage_24h"),
            }
        return uni
    except Exception as e:
        print(f"[universe] 실패: {e}", file=sys.stderr)
        return {}


# ── 2. CoinGecko 검색 트렌딩 ─────────────────────────────────────────────

def fetch_cg_trending(universe: dict) -> list[str]:
    try:
        r = requests.get(f"{CG_BASE}/search/trending",
                         headers=_cg_headers(), timeout=20)
        r.raise_for_status()
        ranked = []
        for c in r.json().get("coins", []):
            item = c["item"]
            sym = item["symbol"].upper()
            if sym not in universe:      # 250위 밖 코인도 표시할 수 있게 채워둠
                data = item.get("data") or {}
                chg = data.get("price_change_percentage_24h")
                if isinstance(chg, dict):
                    chg = chg.get("usd")
                universe[sym] = {
                    "id": item["id"], "name": item["name"], "symbol": sym,
                    "price": None, "market_cap": None, "volume_24h": None,
                    "rank": item.get("market_cap_rank"), "change_24h": chg,
                }
            ranked.append(sym)
        return ranked
    except Exception as e:
        print(f"[cg_trending] 실패: {e}", file=sys.stderr)
        return []


# ── 3. 거래량 회전율 ─────────────────────────────────────────────────────

DERIVATIVE_NAME_PAT = re.compile(
    r"\b(wrapped|staked|liquid staked|bridged|restaked|tokenized)\b", re.IGNORECASE)
NON_TREND_SYMBOLS = {
    "USDT", "USDC", "DAI", "FDUSD", "USD1", "TUSD", "BUSD", "USDE", "PYUSD",
    "USDS", "USDD", "FRAX", "LUSD", "GUSD", "USDF", "USDG", "RLUSD", "BSC-USD",
    "WBTC", "WETH", "STETH", "WSTETH", "WEETH", "WBETH", "RETH", "CBBTC",
    "SOLVBTC", "LBTC", "JITOSOL", "MSOL", "BNSOL", "RSETH", "EZETH",
    "SUSDE", "SUSDS", "BUIDL", "WBNB", "WHYPE", "WSOL",
}


def _is_stable_or_derivative(info: dict) -> bool:
    if info["symbol"] in NON_TREND_SYMBOLS:
        return True
    if DERIVATIVE_NAME_PAT.search(info["name"]):
        return True
    price, chg = info.get("price"), info.get("change_24h")
    if price is not None and 0.98 <= price <= 1.02:
        if chg is None or abs(chg) < 0.5:
            return True
    return False


def rank_by_turnover(universe: dict, top: int = TOP_N) -> tuple[list[str], dict]:
    """24h 거래량/시총. 스테이블·랩드는 구조적으로 높아서 제외한다."""
    rows = []
    for sym, info in universe.items():
        mc, vol = info.get("market_cap"), info.get("volume_24h")
        if mc and vol and not _is_stable_or_derivative(info):
            rows.append((vol / mc, sym))
    rows.sort(reverse=True)
    return [s for _, s in rows[:top]], {s: t for t, s in rows[:top]}


# ── 4. X(트위터) 트렌딩 ──────────────────────────────────────────────────
# X 검색 API는 유료라 못 쓴다. 대신 X 트렌딩을 공개 게시하는 집계 사이트를 읽는다.
# trends24.in robots.txt: `User-agent: *` 에 Allow: / 와 Content-Signal use=reference.
# 학습에 쓰지 않고 참조만 하며, 4시간에 1회로 요청을 최소화한다.

# 빈 문자열 = 전세계. 나머지는 크립토 리테일이 활발한 시장 위주로 골랐다.
TRENDS24_REGIONS = ["", "united-states", "united-kingdom", "turkey",
                    "nigeria", "india", "brazil", "indonesia"]


def _parse_trends24(page_html: str) -> list[tuple[str, Optional[str]]]:
    """최신 스냅샷 한 개의 (트렌드명, 트윗수) 목록."""
    body = re.sub(r"<style.*?</style>", "", page_html, flags=re.S)
    # 타임라인의 첫 카드가 가장 최근 스냅샷
    start = body.find("trend-card__list")
    if start < 0:
        return []
    end = body.find("</ol>", start)
    block = body[start:end if end > 0 else start + 20000]
    out = []
    for li in re.findall(r"<li>.*?</li>", block, re.S):
        a = re.search(r'class=trend-link[^>]*>([^<]+)</a>', li)
        if not a:
            a = re.search(r"<a[^>]*>([^<]+)</a>", li)
        if not a:
            continue
        cnt = re.search(r'data-count="([^"]*)"', li)
        out.append((html.unescape(a.group(1)).strip(),
                    cnt.group(1) if cnt and cnt.group(1) else None))
    return out


CASHTAG_LOOKUP_BUDGET = 6      # 회당 CoinGecko 검색 호출 상한(레이트리밋 보호)
_cashtag_cache: dict = {}


def resolve_cashtag(tok: str, universe: dict, budget: list) -> Optional[str]:
    """유니버스에 없는 캐시태그를 CoinGecko 검색으로 해소.

    X 트렌딩에 $BNBCAT 같은 소형 코인이 뜨는 게 오히려 조기 신호인데,
    시총 250위 유니버스만 보면 통째로 놓친다. 다만 호출 예산은 제한한다.
    """
    if tok in _cashtag_cache:
        return _cashtag_cache[tok]
    if budget[0] <= 0:
        return None
    budget[0] -= 1
    try:
        r = requests.get(f"{CG_BASE}/search", params={"query": tok},
                         headers=_cg_headers(), timeout=15)
        r.raise_for_status()
        for c in r.json().get("coins", [])[:5]:
            if c.get("symbol", "").upper() != tok:
                continue
            universe.setdefault(tok, {
                "id": c.get("id"), "name": c.get("name", tok), "symbol": tok,
                "price": None, "market_cap": None, "volume_24h": None,
                "rank": c.get("market_cap_rank"), "change_24h": None,
            })
            _cashtag_cache[tok] = tok
            return tok
    except Exception as e:
        print(f"[cashtag] {tok} 조회 실패: {e}", file=sys.stderr)
    _cashtag_cache[tok] = None
    return None


def match_trend_term(term: str, universe: dict, budget: Optional[list] = None) -> set:
    """X 트렌드 문구 하나에서 코인 식별. 일반 본문보다 훨씬 엄격하게 본다.

    트렌드는 "SKY PAYDAY WITH BEAUTILAB" 같은 짧은 광고 문구가 많아서,
    맨 티커를 부분매칭하면 화장품 행사가 코인 SKY로 둔갑한다.
    그래서 캐시태그이거나, 문구 전체가 코인명/심볼과 일치하거나,
    이름이 충분히 고유할(5자 이상) 때만 인정한다.
    """
    hits = set()
    # $BTC 또는 #Bitcoin 같은 태그 표기
    for m in re.finditer(r"[$#]([A-Za-z][A-Za-z0-9]{1,9})\b", term):
        tok = m.group(1).upper()
        if tok in universe:
            hits.add(tok)
            continue
        matched_name = next(
            (sym for sym, info in universe.items() if info["name"].upper() == tok), None)
        if matched_name:
            hits.add(matched_name)
        elif budget is not None and m.group(0).startswith("$"):
            # 캐시태그는 크립토일 가능성이 높으니 유니버스 밖이면 조회해본다
            resolved = resolve_cashtag(tok, universe, budget)
            if resolved:
                hits.add(resolved)
    # 문구 전체가 심볼/코인명과 정확히 일치
    plain = term.strip().lstrip("#$").strip().upper()
    if plain in universe and plain not in COMMON_WORDS:
        hits.add(plain)
    for sym, info in universe.items():
        nm = info["name"].upper()
        if nm in COMMON_WORDS or len(nm) < 4:
            continue
        if plain == nm:
            hits.add(sym)
        elif len(nm) >= 5 and re.search(r"\b" + re.escape(nm) + r"\b", term, re.I):
            hits.add(sym)      # "Bitcoin ETF" 처럼 고유한 이름이 포함된 경우
    return hits


def fetch_x_trending(universe: dict, matchers: tuple) -> tuple[list[str], dict]:
    """X 트렌딩에 등장한 코인. 반환: (ranked, {sym: 트렌드표기})."""
    seen: Counter = Counter()
    label: dict = {}
    ok_regions = 0
    budget = [CASHTAG_LOOKUP_BUDGET]
    for region in TRENDS24_REGIONS:
        url = f"https://trends24.in/{region}/" if region else "https://trends24.in/"
        name = region or "worldwide"
        try:
            r = requests.get(url, headers={"User-Agent": HTTP_UA}, timeout=20)
            if not r.ok:
                print(f"[x_trending] {name} HTTP {r.status_code}", file=sys.stderr)
                continue
            trends = _parse_trends24(r.text)
            if not trends:
                print(f"[x_trending] {name} 파싱 결과 없음(구조 변경 의심)", file=sys.stderr)
                continue
            ok_regions += 1
            n = len(trends)
            for i, (term, _count) in enumerate(trends):
                for sym in match_trend_term(term, universe, budget):
                    seen[sym] += (n - i) / n     # 트렌드 상위일수록 가중
                    label.setdefault(sym, term)
        except Exception as e:
            print(f"[x_trending] {name} 실패: {e}", file=sys.stderr)
        time.sleep(1)                            # 예의상 간격
    if not ok_regions:
        return [], {}
    print(f"[x_trending] {ok_regions}개 지역에서 코인 {len(seen)}종 발견", file=sys.stderr)
    return [s for s, _ in seen.most_common(TOP_N)], label


# ── 5. Reddit (OAuth 필요) ───────────────────────────────────────────────
# 공개 JSON(/r/*/new.json)은 2026년 기준 로그인으로 302 리다이렉트된다. 실측 확인.

REDDIT_SUBS = ["CryptoCurrency", "CryptoMoonShots", "Bitcoin", "ethereum",
               "altcoin", "SatoshiStreetBets", "binance"]
REDDIT_WINDOW_SEC = 4 * 3600 + 600      # 실행 주기(4h)보다 살짝 여유


def fetch_reddit(universe: dict, matchers: tuple) -> list[str]:
    cid = os.environ.get("REDDIT_CLIENT_ID")
    sec = os.environ.get("REDDIT_CLIENT_SECRET")
    if not cid or not sec:
        print("[reddit] 자격증명 없음 — 스킵", file=sys.stderr)
        return []
    try:
        import praw
    except ImportError:
        print("[reddit] praw 미설치 — 스킵", file=sys.stderr)
        return []
    try:
        reddit = praw.Reddit(client_id=cid, client_secret=sec, user_agent=HTTP_UA)
        reddit.read_only = True
        cutoff = time.time() - REDDIT_WINDOW_SEC
        counts: Counter = Counter()
        scanned = 0
        for sub in REDDIT_SUBS:
            try:
                for post in reddit.subreddit(sub).new(limit=100):
                    if post.created_utc < cutoff:
                        break
                    scanned += 1
                    text = f"{post.title} {post.selftext or ''}"
                    w = 1 + (post.num_comments or 0) / 50
                    for sym in find_mentions(text, universe, matchers):
                        counts[sym] += w
            except Exception as e:
                print(f"[reddit] r/{sub} 실패: {e}", file=sys.stderr)
        print(f"[reddit] 글 {scanned}건 스캔", file=sys.stderr)
        return [s for s, _ in counts.most_common(TOP_N)]
    except Exception as e:
        print(f"[reddit] 실패: {e}", file=sys.stderr)
        return []


# ── 6. 4chan /biz/ (무인증) ──────────────────────────────────────────────

BIZ_WINDOW_SEC = 4 * 3600


def fetch_biz(universe: dict, matchers: tuple) -> list[str]:
    """4chan /biz/ 카탈로그의 최근 스레드에서 코인 언급 집계."""
    try:
        r = requests.get("https://a.4cdn.org/biz/catalog.json",
                         headers={"User-Agent": HTTP_UA}, timeout=20)
        r.raise_for_status()
        threads = [t for page in r.json() for t in page.get("threads", [])]
        now = time.time()
        counts: Counter = Counter()
        recent = 0
        for t in threads:
            if now - (t.get("last_modified") or 0) > BIZ_WINDOW_SEC:
                continue
            recent += 1
            raw = f"{t.get('sub', '')} {t.get('com', '')}"
            text = html.unescape(re.sub(r"<[^>]+>", " ", raw))
            w = 1 + (t.get("replies") or 0) / 50     # 댓글 많은 스레드에 가중
            for sym in find_mentions(text, universe, matchers):
                counts[sym] += w
        print(f"[biz] 최근 스레드 {recent}건 스캔", file=sys.stderr)
        return [s for s, _ in counts.most_common(TOP_N)]
    except Exception as e:
        print(f"[biz] 실패: {e}", file=sys.stderr)
        return []
