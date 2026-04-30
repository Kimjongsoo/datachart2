# DataChart v1 — Windows 셋업 가이드

## 1. Python 설치 확인

PowerShell 열어서:

```powershell
python --version
```

`Python 3.10` 이상이면 OK. 없거나 낮으면 https://www.python.org/downloads/ 에서 **Python 3.12** 다운로드.

> ⚠️ 설치 첫 화면에서 **"Add python.exe to PATH"** 체크박스 꼭 체크하세요.

## 2. 프로젝트 폴더 만들기

영문 경로 추천 (한글 경로는 가끔 문제):

```powershell
mkdir C:\Projects\datachart
cd C:\Projects\datachart
```

이 폴더에 다음 4개 파일을 복사해 넣어주세요:

- `datachart.py`
- `requirements.txt`
- `setup_guide.md`
- `.gitignore`

## 3. 가상환경 생성 + 활성화

```powershell
python -m venv .venv
.venv\Scripts\activate
```

활성화되면 프롬프트 앞에 `(.venv)` 가 붙습니다.

> 매번 새 PowerShell 열 때마다 `.venv\Scripts\activate` 한 번 실행해야 가상환경이 활성화됩니다.

## 4. 패키지 설치

```powershell
pip install --upgrade pip
pip install -r requirements.txt
```

5분 정도 걸립니다. PySide6가 좀 무거워요(약 100MB).

## 5. 실행

```powershell
python datachart.py
```

창이 뜨고 **한화비전(172900)** 1년치 종합 데이터(차트/수급/펀더멘털)가 보이면 성공!

- **차트** 탭: 캔들 + MA20/60 + 거래량
- **수급** 탭: 외국인/기관/개인 누적 순매수(억원)
- **펀더멘털** 탭: PER/PBR/EPS/BPS/시가총액/외국인 지분율 + 추이 차트
- 다른 종목 보고 싶으면 상단 입력란에 6자리 코드 입력 후 [불러오기] 또는 Enter
- 마우스 휠로 확대·축소, 드래그로 이동, 우클릭 메뉴

추천 테스트 종목:

| 코드 | 종목 |
|---|---|
| 489790 | 한화비전 |
| 012450 | 한화에어로스페이스 |
| 005930 | 삼성전자 |
| 000660 | SK하이닉스 |
| 005380 | 현대차 |
| 035420 | NAVER |
| 035720 | 카카오 |
| 207940 | 삼성바이오로직스 |
| 373220 | LG에너지솔루션 |

## 6. 두 번째 실행부터는 빠름

같은 폴더에 `datachart.duckdb` 파일이 자동 생성됩니다 — 로컬 캐시예요. 같은 종목/기간을 다시 조회하면 네트워크 없이 즉시 표시됩니다.

용량은 종목당 약 50KB. 종목 1000개 1년치를 다 받아도 50MB 수준이라 1TB 디스크면 한참 남습니다.

---

## 트러블슈팅

**`python을 찾을 수 없습니다`**
- 설치 시 PATH 체크 안 한 경우. 재설치하면서 첫 화면 PATH 체크박스 활성화.

**`pip install`이 매우 느리거나 멈춤**
- 회사 방화벽 가능성 → 집에서 시도
- 또는 국내 미러 사용: `pip install -r requirements.txt -i https://pypi.org/simple`

**`finplot` 창이 검게 뜨거나 차트가 안 보임**
- 그래픽 드라이버 문제 가능 → `pip install pyqtgraph --upgrade`
- 또는 통합 그래픽이 너무 옛날일 때

**`종목 데이터 없음` 오류**
- 종목코드 잘못 입력 (6자리 숫자, 예: 005930) 또는 KRX 사이트 일시 장애
- 코스닥 종목도 6자리(예: 091990 셀트리온헬스케어) 입력하면 됨

**`pykrx`에서 timeout 발생**
- KRX 서버 일시 부하. 잠시 후 재시도. 한 번 받으면 캐시되니까 두 번째부터는 영향 없음.

---

## 다음 단계 미리보기

이게 잘 도는 걸 확인하면 다음 순서로 확장합니다:

1. **종목 검색 기능** — 코드 대신 종목명 일부로 찾기
2. **여러 종목 비교** — 한 화면에 2~4개 종목 동시 표시
3. **기술적 지표** — RSI, MACD, 볼린저밴드 등
4. **MCP 서버 래퍼** — Claude가 이 데이터를 호출해서 분석할 수 있도록
5. **키움 REST 연동** — 실시간 시세 WebSocket
