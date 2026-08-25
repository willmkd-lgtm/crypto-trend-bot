# crypto-trend-bot

4시간마다 코인 관심도 트렌드 상위 20개를 집계해 텔레그램(개인 DM)으로 보낸다.

## 왜 Twitter/IG/FB가 아닌가

- **Twitter/X**: 검색 API가 유료(Basic $200/월~)라 실시간 언급량 스캔 불가
- **Instagram/Facebook**: 해시태그 트렌드를 뽑을 공식 API 자체가 없고, 스크래핑은 ToS 위반·로그인벽으로 불안정
- **Reddit 공개 JSON**: 2026년 기준 `/r/*/new.json`이 로그인 페이지로 302 리다이렉트됨 (실측 확인).
  OAuth 자격증명이 있을 때만 사용 가능

그래서 **자격증명 없이도 동작하는 관심도 프록시 2종**을 기본으로 삼고,
Reddit은 자격증명이 있으면 자동으로 추가되는 구조로 만들었다.

## 소스 & 집계 방식

| 소스 | 내용 | 자격증명 |
|---|---|---|
| 검색트렌드 | CoinGecko `/search/trending` — 검색 급상승 = 리테일 관심 쏠림 | 불필요 |
| 거래량 | 24h 거래량 / 시총 회전율 — 규모 대비 이상 활동 | 불필요 |
| Reddit | 최근 1시간 신규 글의 티커/코인명 언급량 (7개 서브레딧) | **필요** |

각 소스의 순위를 Borda count로 점수화(리스트 길이 차이는 정규화)해 합산 → 상위 20개.
소스 하나가 실패하거나 자격증명이 없어도 나머지로 계속 집계한다(전체 실패시에만 발송 스킵).

**거래량 회전율에서 제외되는 것**: 스테이블코인·랩드/스테이킹 파생 토큰.
구조적으로 회전율이 높아 화제성과 무관하게 상위를 점거하기 때문
(심볼 목록 + 이름 패턴 + `$1 근처 & 변동 거의 없음` 휴리스틱 3중 필터).

## 출력 예시

```
🔥 코인 관심도 트렌드 TOP 20  (08-25 22:08 KST)

1. Cash Cat (CASHCAT) #180  +12.6%
    └ 검색트렌드 · 거래량 0.41x
2. Ethena (ENA) #58  -8.4%
    └ 검색트렌드 · 거래량 0.44x
...
```

`#180`은 시총 순위, `0.41x`는 24h 거래량이 시총의 41%라는 뜻.

## GitHub Secrets

리포 Settings → Secrets and variables → Actions:

| Secret | 필수 | 설명 |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | macro-brief와 동일 봇 재사용 |
| `TELEGRAM_CHAT_ID` | ✅ | 개인 DM용 chat id |
| `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` | 선택 | 없으면 Reddit 소스만 스킵 |
| `COINGECKO_API_KEY` | 선택 | 없어도 동작하나 무료 레이트리밋에 더 취약 |

### Reddit 자격증명 (선택)

1. https://www.reddit.com/prefs/apps → "create app" → 타입 **script**
2. redirect uri는 `http://localhost:8080` (script 타입도 필수 입력이라 형식만 채우는 것)
3. 생성 후 앱 이름 아래 문자열이 `client_id`, "secret" 옆이 `client_secret`

> 버튼을 눌러도 반응이 없으면 대개 **계정 이메일 미인증**이 원인.
> Reddit은 이메일 인증 없이는 앱 생성을 막는다.

## 로컬 테스트

```bash
pip install -r requirements.txt
DRY_RUN=1 python trend_bot.py          # 발송 없이 결과만 출력
```

`DRY_RUN=1`이면 텔레그램 발송을 생략한다. 실제 발송은 위 환경변수 2개가 필요.

## 알려진 한계

- 이 봇이 재는 것은 **직접적인 SNS 언급량이 아니라 관심도 프록시**다.
  검색 급상승과 거래량 회전율은 화제성과 상관이 높지만 동일하진 않다
  (Reddit 자격증명을 넣으면 실제 언급량이 한 축으로 추가된다)
- Reddit 티커 매칭은 정규식 기반. `$TICKER` 표기와 전체 코인명은 신뢰도가 높지만,
  영단어와 겹치는 대문자 단독 티커(`ONE`, `FOR` 등)는 소수 스톱워드로만 막아둠 —
  결과에 튀는 종목이 있으면 원문을 직접 확인할 것
- CoinGecko 무료 엔드포인트는 레이트리밋이 있어 드물게 실패할 수 있음(해당 소스만 스킵됨)
- 저장소는 **public** — 코드는 공개, 키는 Secrets라 비공개.
  GitHub Actions 무료 시간 무제한을 확보하려는 목적
