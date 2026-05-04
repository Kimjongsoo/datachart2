# DataChart

한국 주식 종합 차트·분석 데스크톱 앱.
**KRX OpenAPI · 한국투자증권(KIS) REST/WebSocket · Yahoo Finance · Naver Finance · pykrx** 를 무료 출처로 통합해 일/주/5분봉, 외국인·기관 수급, 펀더멘털, 실시간 틱 라이브 봉까지 한 화면에 표시합니다.

> **모의/실전 자동 분기, 멀티 출처 자동 폴백, KIS WebSocket 틱 단위 실시간, 한국 시장 관례 색상 (상승=빨강, 하락=파랑)**

---

## 주요 기능

### 📈 차트 탭
- 캔들 + **MA 4종**(5/20/112/224, 색상별 구분)
- **거래량** (만주 단위 자동 변환)
- **일목균형표** 토글: 선행스팬1·2 점선 표시 (HTS 표준 9/26/52/26 파라미터)
- **지수 비교 오버레이**: 코스피·코스피 200·KRX 300·KRX 100·코스닥·코스닥 150 정규화 라인
- **현재가 박스**: 빨강(상승)/파랑(하락) 가로 점선 + 우측 라벨

### 📊 주기 선택
| 주기 | 출처 | 비고 |
|---|---|---|
| 일봉 | KRX OpenAPI / pykrx | 1년치 |
| 주봉 | 일봉 → W-FRI 리샘플 | |
| 5분봉 | Yahoo Finance + KIS 갭보강 | 60일치, 진짜 OHLC |

### ⚡ 실시간 LIVE (KIS Developer 키 등록 시)
- **WebSocket H0STCNT0** 체결가 푸시 — 정규장 09:00~15:30 KST에 자동 연결
- 가격 라벨이 **체결 발생 즉시** 깜빡이며 갱신 (REST 폴링 아님)
- 5분봉 모드: 진행 중인 봉이 **틱 단위로 꿈틀거림**
- 일/주봉 모드: 오늘 봉을 KIS 일봉 OHL로 합성

### 💰 수급 탭
- 외국인·기관·개인 **누적 순매수** (60일치, Naver frgn 스크래핑)

### 📑 펀더멘털 탭
- PER · PBR · EPS · BPS · 시가총액 · 외국인 지분율 (요약)
- PER/PBR 추이, 외국인 지분율 추이, 분기 매출/영업이익 (yfinance 재무제표)

### 📕 스텔스 모드 (회사용 🤫)
- 토글 한 번에 차트·탭·색상 모두 숨김
- **종목코드 + 종목명 + 가격 + 등락률만** 작은 흑색 글씨로 표시
- 창 크기 360×56로 축소, 윈도우 타이틀 "Memo"
- 다시 누르면 원래대로 복원

---

## 데이터 출처 우선순위 (자동 폴백)

| 데이터 종류 | 1순위 | 2순위 | 3순위 |
|---|---|---|---|
| **OHLCV 일봉** | KRX OpenAPI (`sto/stk_bydd_trd`) | pykrx 스크래핑 | — |
| **5분봉 history** | Yahoo Finance (`yfinance`) | Naver siseJson 1분 → 5분 합성 | — |
| **5분봉 오늘 갭** | KIS REST (`inquire-time-itemchartprice`) | (yfinance 그대로) | — |
| **실시간 체결가** | **KIS WebSocket (`H0STCNT0`)** | KIS REST `inquire-price` (2초 폴링) | OHLCV 마지막 종가 (정적) |
| **지수 (KOSPI 시리즈)** | KRX OpenAPI (`idx/krx_dd_trd`, `idx/kospi_dd_trd`, `idx/kosdaq_dd_trd`) | — | — |
| **외국인·기관 일별** | (KRX 투자자별 거래실적 신청 시) | Naver frgn 스크래핑 | — |
| **펀더멘털 (PER/PBR/시총)** | (KRX 종목정보 신청 시) | Naver Finance 메인 스크래핑 | — |
| **분기 매출/영업이익** | yfinance `quarterly_financials` | — | — |
| **종목명** | KIS API → Naver → pykrx | | |

API 키 없어도 무료 폴백으로 모든 핵심 기능이 작동합니다 (단 실시간 틱은 비활성).

---

## 설치

```bash
git clone https://github.com/Kimjongsoo/datachart2.git
cd datachart2
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS/Linux
pip install -r requirements.txt
```

자세한 Windows 셋업 가이드는 [`setup_guide.md`](setup_guide.md) 참고.

### 의존성

```text
pyside6>=6.6      # GUI (Qt6)
finplot>=1.9      # 캔들 차트 (pyqtgraph 기반)
duckdb>=1.0       # 로컬 캐시
pykrx>=1.0.45     # KRX 스크래핑 (폴백용)
pandas>=2.0
yfinance>=0.2     # 5분봉/분기 재무
```

---

## 환경변수

`.env.example` 참고. **시스템 환경변수**(Windows GUI: 시스템 → 환경 변수 편집)에 등록하세요. `.env` 파일은 git ignore 처리되어 있지만 **절대 커밋 금지**.

| 환경변수 | 발급처 | 용도 |
|---|---|---|
| `KRX_API_KEY` | https://openapi.krx.co.kr | KOSPI/KOSDAQ/KRX 시리즈 지수 정식 데이터 |
| `KIS_APP_KEY` / `KIS_APP_SECRET` | https://apiportal.koreainvestment.com | 실시간 시세, 5분봉 갭보강, 라이브 봉 |
| `KIS_ENV` | — | `real`(기본, 실전) 또는 `mock`(모의투자) |

### 모의·실전 동시 운용 (선택)
```bash
KIS_REAL_APP_KEY=...    KIS_REAL_APP_SECRET=...
KIS_MOCK_APP_KEY=...    KIS_MOCK_APP_SECRET=...
KIS_ENV=real            # 'real' 또는 'mock'으로 토글
```

미등록 시: 무료 폴백 출처로 차트 정상 작동, 실시간 LIVE만 비활성화.

---

## 실행

```bash
python datachart.py
```

기본 종목 한화비전(489790). 상단 입력란에 6자리 종목코드 입력 후 Enter 또는 [불러오기].

### 단축 (선택)
바탕화면에 `DataChart.bat` 파일 만들고:
```bat
@echo off
cd /d C:\Users\user\Desktop\dev\chart
call .venv\Scripts\activate
python datachart.py
```
더블클릭 한 번에 실행.

---

## 아키텍처

```
┌─────────────────────────────────────────────────────────┐
│  DataChartWindow (PySide6 QMainWindow)                  │
│                                                         │
│  ┌────────────────────────────────────────────────────┐ │
│  │ 컨트롤 바: [코드][종목명][주기][일목][비교][LIVE]   │ │
│  └────────────────────────────────────────────────────┘ │
│                                                         │
│  ┌─[ 차트 ]─────────────────────────────────────────┐  │
│  │  ┌──────────────────────────────────┐           │  │
│  │  │ 가격 (캔들 + MA + 일목 + 비교)    │           │  │
│  │  └──────────────────────────────────┘           │  │
│  │  ┌──────────────────────────────────┐           │  │
│  │  │ 거래량 (만주)                     │           │  │
│  │  └──────────────────────────────────┘           │  │
│  └───────────────────────────────────────────────────┘  │
│                                                         │
│  ┌─[ 수급 ]──────────────────────────────────────────┐  │
│  │ 외국인 / 기관 / 개인 누적 순매수                   │  │
│  └────────────────────────────────────────────────────┘ │
│                                                         │
│  ┌─[ 펀더멘털 ]──────────────────────────────────────┐  │
│  │ 요약 라벨 + PER/PBR + 외국인 지분율 + 매출/이익    │  │
│  └────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────┘

KisRealtimeWorker (별도 thread) ──[Signal]─→ DataChartWindow
  │
  └─ ws://ops.koreainvestment.com:21000  (KIS WebSocket H0STCNT0)
```

- **GUI**: PySide6 + finplot (pyqtgraph 기반) + QSplitter 분할 레이아웃
- **캐시**: DuckDB 로컬 (`datachart.duckdb`, 자동 생성, git ignore)
- **실시간**: `websockets.sync.client` + `threading.Thread` + Qt `Signal`
- **HTTP**: stdlib `urllib` + `yfinance`
- **콘솔 노이즈 차단**: `_PykrxNoiseFilter`로 stdout/stderr 필터링 (pykrx, Qt DPI 등)

---

## 폴더 구조

```
datachart2/
├── datachart.py          메인 애플리케이션 (~2,500 줄)
├── requirements.txt      의존성
├── setup_guide.md        Windows 셋업 상세 가이드
├── .env.example          환경변수 템플릿
├── .gitignore            venv·duckdb·log·env 차단
└── README.md
```

---

## 한계

- **모바일 HTS와의 미세 지연**: KIS Developers는 자사 HTS와 달리 OpenAPI 게이트웨이를 한 단계 거쳐서 50~300ms 추가 지연. 자사 직결 시세는 일반 OpenAPI로 받을 수 없음.
- **yfinance 분봉 ~15분 지연**: KIS 분봉으로 갭 보강하지만 KIS 키 없으면 갭 발생.
- **5분봉 60일 한도**: yfinance interval='5m'은 max 60일. 더 긴 history는 KIS daily-itemchartprice 추가 필요.
- **호가창 미지원**: 현재는 체결가(`H0STCNT0`)만. 호가(`H0STASP0`) 추가 가능.
- **KRX 일부 endpoint 인증 필요**: pykrx의 투자자/펀더멘털 endpoint는 KRX 비공개 API라 빈 결과. → Naver/yfinance 자동 폴백.

---

## 보안

- 모든 API 키는 **환경변수로만** 관리
- 코드에 하드코딩 금지, `.env` 파일 git 커밋 금지 (`.gitignore`로 차단)
- KIS WebSocket 토큰(approval_key)은 메모리 캐시 (23h)
- KRX 401 응답 시 그 서비스만 차단 (다른 서비스는 정상 호출)

---

## 라이선스

MIT (또는 본인이 원하는 라이선스로 교체)

## 면책

본 도구는 정보 조회·분석 용도이며, 투자 자문/권유가 아닙니다. 매매 의사결정 및 결과는 사용자 본인의 책임입니다.
