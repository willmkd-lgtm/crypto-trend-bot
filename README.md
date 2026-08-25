# crypto-trend-bot

1시간마다 코인 SNS/커뮤니티 언급 트렌드 상위 20개를 집계해 텔레그램(개인 DM)으로 보낸다.

## 왜 Twitter/IG/FB가 아니라 이 소스들인가

- Twitter/X 검색 API는 유료(Basic $200/월~)라 실시간 언급량 스캔에 못 씀
- Instagram/Facebook은 해시태그 트렌드를 뽑을 공식 API 자체가 없고, 스크래핑은 ToS 위반·로그인벽으로 불안정
- 대신 **이미 소셜/검색 신호를 내포한 트렌딩 소스**(CoinGecko, CoinMarketCap) + **직접 집계 가능한 Reddit**을 합산해서
  "SNS·커뮤니티에서 뜨는 코인" 스크리너를 대체 구현

## 소스 & 집계 방식

| 소스 | 내용 | 비용 |
|---|---|---|
| CoinGecko `/search/trending` | 검색 급상승 코인 | 무료, 키 불필요 |
| CoinMarketCap `/trending/latest` | 커뮤니티 트렌딩 | 무료 키(Basic 플랜) |
| Reddit | 최근 1시간 신규 글의 티커/코인명 언급량 (7개 서브레딧) | 무료 앱 등록 |

각 소스의 순위에 Borda count(1위=N점 ... 꼴찌=1점)를 매기고 합산 → 상위 20개.
소스 하나가 키 미설정 등으로 실패해도 나머지 소스로 계속 집계한다(전체 실패시에만 발송 스킵).

## 필요한 GitHub Secrets

리포 Settings → Secrets and variables → Actions 에서 등록:

| Secret | 필수 | 설명 |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | **macro-brief 리포에 이미 등록된 값과 동일하게** 복사해서 넣으면 됨 (같은 봇 재사용) |
| `TELEGRAM_CHAT_ID` | ✅ | 마찬가지로 macro-brief의 개인 DM용 `TELEGRAM_CHAT_ID` 값을 그대로 복사 |
| `CMC_API_KEY` | 선택 | 없으면 CoinMarketCap 소스만 스킵하고 나머지로 동작 |
| `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET` | 선택 | 없으면 Reddit 소스만 스킵 |
| `COINGECKO_API_KEY` | 선택 | 없어도 동작하나 무료 레이트리밋에 더 취약 |

### CoinMarketCap 키 발급
1. https://pro.coinmarketcap.com/signup 무료 가입
2. 대시보드에서 API 키 복사 → `CMC_API_KEY`로 등록
3. (참고) Basic 무료 플랜에 trending 엔드포인트가 빠져 있으면 해당 소스는 자동 스킵되고 CoinGecko+Reddit만으로 동작함

### Reddit 앱 등록 (client_id/secret만 있으면 됨, 로그인 불필요 — read-only)
1. https://www.reddit.com/prefs/apps → "create app" → 타입 **script** 선택
2. 생성 후 앱 이름 아래 나오는 문자열이 `client_id`, "secret"이 `client_secret`
3. 각각 `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`로 등록

## 로컬 테스트

```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
export CMC_API_KEY=...            # 선택
export REDDIT_CLIENT_ID=...       # 선택
export REDDIT_CLIENT_SECRET=...   # 선택
python trend_bot.py
```

## 알려진 한계

- Reddit 티커 매칭은 정규식 기반이라 완벽하지 않음. `$TICKER` 표기와 전체 코인명은 신뢰도가 높지만,
  대문자 단독 티커(`ONE`, `FOR` 등 영단어와 겹치는 것)는 오탐 방지를 위해 소수 스톱워드로만 제외해뒀음 —
  완전하지 않으니 결과에 이상하게 튀는 종목이 있으면 원문 언급을 직접 확인할 것
- CoinGecko 무료 엔드포인트는 레이트리밋이 있어 드물게 실패할 수 있음(스킵 처리됨)
- 저장소는 **public**으로 운영(코드 공개, 키는 Secrets라 비공개) — GitHub Actions 무료 시간 무제한 확보 목적
