"""코인 언급/관심 트렌드 스크리너.

4시간마다 실행되어 여러 소스의 랭킹을 합산, 상위 20개를 텔레그램으로 보낸다.
소스별 수집 로직은 sources.py 참고.

환경변수:
    TELEGRAM_BOT_TOKEN   (필수) - macro-brief와 동일 봇 재사용
    TELEGRAM_CHAT_ID     (필수) - 개인 DM용 chat id
    REDDIT_CLIENT_ID     (선택) - 없으면 Reddit 소스 스킵
    REDDIT_CLIENT_SECRET (선택)
    COINGECKO_API_KEY    (선택) - demo 키. 없어도 동작
    DRY_RUN=1            (선택) - 텔레그램 발송 없이 출력만
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import sources as S

# Windows 콘솔(cp949)에서 이모지 출력 시 UnicodeEncodeError로 죽는 것 방지
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

TOP_N = 20
KST = timezone(timedelta(hours=9))
TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"

# 소셜 실언급이 시장지표보다 무겁다 — 원래 알고 싶은 게 "어디서 회자되는가"라서.
SOURCE_WEIGHTS = {
    "X트렌딩": 1.5,
    "Reddit": 1.3,
    "/biz/": 1.0,
    "검색트렌드": 1.0,
    "거래량": 0.7,
}


STATE_PATH = Path(__file__).with_name("state") / "latest.json"
RANK_JUMP = 3       # 이만큼 이상 움직여야 화살표를 붙인다(자잘한 진동은 무시)


def load_previous() -> dict:
    """직전 실행의 {symbol: 순위}. 첫 실행이거나 파일이 깨졌으면 빈 dict."""
    try:
        with STATE_PATH.open(encoding="utf-8") as f:
            return json.load(f).get("ranking", {})
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as e:
        print(f"[state] 이전 상태 읽기 실패({e}) — 변화 표시 없이 진행", file=sys.stderr)
        return {}


def save_current(top: list) -> None:
    ranking = {sym: i for i, (sym, _score) in enumerate(top, 1)}
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"updated_at": datetime.now(KST).isoformat(), "ranking": ranking}
    with STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def change_marker(sym: str, now_rank: int, previous: dict) -> str:
    """직전 대비 변화 표시. 이전 상태가 없으면(첫 실행) 아무것도 안 붙인다."""
    if not previous:
        return ""
    if sym not in previous:
        return "🆕 "
    delta = previous[sym] - now_rank      # 양수 = 순위 상승
    if delta >= RANK_JUMP:
        return f"⬆️{previous[sym]}→{now_rank} "
    if -delta >= RANK_JUMP:
        return f"⬇️{previous[sym]}→{now_rank} "
    return ""


def borda_merge(sources: list[tuple[str, list[str]]]) -> tuple[list, dict]:
    """소스별 순위를 0~1로 정규화해 가중 합산. 리스트 길이 차이를 상쇄한다."""
    scores: defaultdict = defaultdict(float)
    tags: defaultdict = defaultdict(list)
    for label, ranked in sources:
        n = len(ranked)
        w = SOURCE_WEIGHTS.get(label, 1.0)
        for i, sym in enumerate(ranked):
            scores[sym] += w * (n - i) / n
            tags[sym].append(label)
    ordered = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    return ordered, tags


def build_message(top, universe, tags, turnovers, x_labels, source_labels,
                  previous) -> str:
    now = datetime.now(KST).strftime("%m-%d %H:%M")
    social = {"X트렌딩", "Reddit", "/biz/"}
    lines = [f"🔥 코인 관심도 트렌드 TOP {len(top)}  ({now} KST)"]

    newcomers = [s for s, _ in top if previous and s not in previous]
    if newcomers:
        lines.append(f"🆕 신규 진입 {len(newcomers)}종: "
                     + ", ".join(universe.get(s, {}).get("name", s) for s in newcomers[:5]))
    lines.append("")

    for i, (sym, _score) in enumerate(top, 1):
        info = universe.get(sym, {})
        name = info.get("name", sym)
        chg = info.get("change_24h")
        chg_txt = f"  {chg:+.1f}%" if isinstance(chg, (int, float)) else ""
        rank = info.get("rank")
        rank_txt = f" #{rank}" if rank else ""
        # 소셜에서 실제로 회자된 종목은 눈에 띄게
        mark = "🗣 " if social & set(tags.get(sym, [])) else ""
        move = change_marker(sym, i, previous)
        lines.append(f"{i}. {move}{mark}{name} ({sym}){rank_txt}{chg_txt}")

        why = []
        for t in tags.get(sym, []):
            if t == "거래량" and sym in turnovers:
                why.append(f"거래량 {turnovers[sym]:.2f}x")
            elif t == "X트렌딩" and sym in x_labels:
                why.append(f"X트렌딩 “{x_labels[sym]}”")
            else:
                why.append(t)
        lines.append(f"    └ {' · '.join(why)}")
    legend = "🗣 = SNS 실언급 포착"
    if previous:
        legend += f" · 🆕 신규 · ⬆️⬇️ = {RANK_JUMP}계단 이상 이동"
    lines += ["", f"소스: {' + '.join(source_labels)}", legend]
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
    universe = S.fetch_universe()
    if not universe:
        print("시세 데이터를 못 받아옴 — 발송 스킵", file=sys.stderr)
        return 1

    # 이 둘은 universe에 새 코인을 채워넣으므로 matchers보다 먼저 돌려야 한다.
    # (X트렌딩이 캐시태그로 해소한 소형 코인도 Reddit·/biz/ 본문에서 잡히도록)
    cg_trending = S.fetch_cg_trending(universe)
    x_ranked, x_labels = S.fetch_x_trending(universe)

    matchers = S.build_matchers(universe)
    reddit_ranked = S.fetch_reddit(universe, matchers)
    biz_ranked = S.fetch_biz(universe, matchers)
    turnover_ranked, turnovers = S.rank_by_turnover(universe)

    sources = [(label, lst) for label, lst in [
        ("X트렌딩", x_ranked),
        ("Reddit", reddit_ranked),
        ("/biz/", biz_ranked),
        ("검색트렌드", cg_trending),
        ("거래량", turnover_ranked),
    ] if lst]

    if not sources:
        print("모든 소스 실패 — 발송 스킵", file=sys.stderr)
        return 1

    ordered, tags = borda_merge(sources)
    top = ordered[:TOP_N]
    previous = load_previous()
    msg = build_message(top, universe, tags, turnovers, x_labels,
                        [s[0] for s in sources], previous)
    print(msg)

    if os.environ.get("DRY_RUN") == "1":
        print("\n[DRY_RUN] 텔레그램 발송 생략", file=sys.stderr)
        return 0

    send_telegram(msg)
    print("sent.", file=sys.stderr)
    # 발송에 성공한 뒤에만 갱신한다. 실패했는데 저장하면 다음 회차가
    # 사용자가 본 적 없는 순위를 기준으로 변화를 계산하게 된다.
    save_current(top)
    return 0


if __name__ == "__main__":
    sys.exit(main())
