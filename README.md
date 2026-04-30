# DataChart

한국 주식 종합 차트·분석 데스크톱 앱. KRX OpenAPI · 한국투자증권(KIS) · Yahoo Finance · Naver Finance · pykrx를 무료 데이터 출처로 통합해 일봉/주봉/5분봉, 외국인·기관 수급, 펀더멘털, 실시간 라이브 봉까지 한 화면에 표시합니다.

## 주요 기능

- **차트**: 캔들 + 이동평균(MA) + 거래량
- **주기**: 일봉 / 주봉 / 5분봉(60일치 진짜 OHLC)
- **지수 비교**: 코스피 / 코스피 200 / KRX 300 / KRX 100 / 코스닥 / 코스닥 150 정규화 오버레이
- **수급**: 외국인·기관·개인 누적 순매수 (60일치)
- **펀더멘털**: PER / PBR / EPS / BPS / 시가총액 / 외국인 지분율 + PER/PBR 추이
- **실시간 LIVE**: KIS API 키 등록 시 정규장 시간(09:00~15:30 KST)에 5분봉 1초 폴링 → 진행 중인 봉이 꿈틀거리며 자라는 모습

## 데이터 출처 우선순위 (자동 폴백)

| 데이터 | 1순위 | 폴백 |
|---|---|---|
| 일봉 OHLCV | KRX OpenAPI (`sto/stk_bydd_trd`) | pykrx 스크래핑 |
| 5분봉 OHLC (60일) | Yahoo Finance | Naver siseJson 1분 → 합성 |
| 5분봉 라이브 (1초) | KIS REST `inquire-price` | (없음) |
| 외국인·기관 일별 | (KRX 투자자별 거래실적 신청 시 자동) | Naver frgn 스크래핑 |
| 펀더멘털 | (KRX 종목정보 신청 시 자동) | Naver Finance 메인 |
| 지수 (KOSPI/KRX) | KRX OpenAPI (`idx/*_dd_trd`) | (없음) |

API 키가 없으면 무료 폴백으로 자동 전환됩니다.

## 설치

```bash
git clone <repo-url>
cd datachart
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS/Linux
pip install -r requirements.txt
```

## 환경변수 (선택, 등록 시 더 풍부한 데이터)

`.env.example` 파일을 참고해 Windows 시스템 환경변수에 등록하세요. **`.env` 파일을 직접 만들어 git 커밋하면 키가 노출되니 절대 금지** — 무조건 OS 환경변수로만 관리합니다.

| 환경변수 | 발급처 | 용도 |
|---|---|---|
| `KRX_API_KEY` | https://openapi.krx.co.kr | 정식 KRX 지수/종목 데이터 |
| `KIS_APP_KEY` / `KIS_APP_SECRET` | https://apiportal.koreainvestment.com | 5분봉 실시간 LIVE |
| `KIS_ENV` | (없음) | `real`(기본) 또는 `mock` |

미등록 시 차트는 무료 폴백으로 정상 작동하며, 5분봉 LIVE만 비활성화됩니다.

## 실행

```bash
python datachart.py
```

기본 종목은 한화비전(489790). 상단 입력란에 6자리 종목코드 입력 후 Enter 또는 [불러오기].

## 아키텍처

- **GUI**: PySide6 + finplot (pyqtgraph 기반)
- **캐시**: DuckDB 로컬 파일 (`datachart.duckdb`, 자동 생성, git ignore)
- **HTTP**: stdlib `urllib` + `yfinance`
- **차트 데이터**: `pykrx`, KRX OpenAPI, Yahoo Finance, Naver scraping

## 폴더 구조

```
datachart/
├── datachart.py          메인 애플리케이션
├── requirements.txt      의존성
├── setup_guide.md        Windows 셋업 상세 가이드
├── .env.example          환경변수 템플릿
├── .gitignore
└── README.md
```

## 한계

- 5분봉 라이브는 REST 1초 폴링 (WebSocket 아님). 진짜 틱 단위가 필요하면 KIS WebSocket(`H0STCNT0`) 업그레이드 필요.
- pykrx의 일부 엔드포인트는 KRX 정책상 인증 없이 빈 결과를 반환할 수 있음 → Naver/yfinance 자동 폴백
- yfinance 분봉은 yahoo 측 rate limit이 있어 짧은 시간에 다회 호출 시 일시 차단 가능

## 라이선스

MIT (또는 본인이 원하는 라이선스로 교체)

## 면책

본 도구는 정보 조회·분석 용도이며, 투자 자문/권유가 아닙니다. 매매 의사결정은 사용자 본인의 책임입니다.
