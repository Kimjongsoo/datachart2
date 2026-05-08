"""
DataChart v2 - 한국 주식 종합 데이터 차트
==========================================
스택:
  - pykrx        : KRX 공개 데이터 (OHLCV / 투자자 수급 / 펀더멘털 / 시총 / 외국인보유율)
  - DuckDB       : 로컬 캐시 (테이블별 저장)
  - PySide6      : Qt6 데스크톱 GUI (탭 인터페이스)
  - finplot      : 캔들·라인 차트 (pyqtgraph 기반)

기능:
  - [차트] 캔들 + MA20/60 + 거래량
  - [수급] 외국인 / 기관 / 개인 일별 순매수대금 (누적 라인)
  - [펀더멘털] PER / PBR / EPS / BPS / DIV + 시가총액 + 외국인 보유율 (최신값 + 추이)

캐시 테이블:
  ohlcv_daily            : 일봉 OHLCV
  investor_value_daily   : 일별 투자자별 순매수 거래대금
  fundamental_daily      : PER/PBR/EPS/BPS/DIV/DPS
  marketcap_daily        : 시가총액/거래량/거래대금/상장주식수
  foreign_rate_daily     : 외국인 보유 수량/지분율/한도소진율
"""
from __future__ import annotations

import os
# pyqtgraph가 PyQt5/6 대신 PySide6 바인딩을 쓰도록 강제 (finplot import 전에 설정해야 함)
os.environ.setdefault("PYQTGRAPH_QT_LIB", "PySide6")
os.environ.setdefault("QT_API", "pyside6")
# Windows DPI 경고 억제 (Qt가 이미 잡힌 DPI 컨텍스트를 덮어쓰지 않도록)
os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "0")
os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "0")
# Qt qpa.window 카테고리 로그 비활성화 (DPI awareness 경고 차단)
os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.window=false;qt.qpa.windows=false")

import sys
import contextlib as _contextlib
import io as _io
from datetime import datetime, timedelta
from pathlib import Path


# --- pykrx 노이즈 필터: 특정 문구만 차단, 나머지 stderr는 정상 통과 ---
class _PykrxNoiseFilter:
    """pykrx가 stderr/__stderr__로 print하는 'KRX 로그인 실패...' 등
    특정 노이즈 문구만 걸러내고 나머지(예: 진짜 traceback)는 정상 통과시킴."""

    _NOISE_PHRASES = (
        # pykrx 노이즈
        "KRX 로그인 실패",
        "KRX_ID 또는 KRX_PW",
        "Error occurred in get_market_",
        "Error occurred in get_stock_",
        "Expecting value: line 1 column 1",
        # Qt DPI awareness 경고 (Windows에서 이미 DPI 컨텍스트 잡혀있을 때)
        "qt.qpa.window:",
        "SetProcessDpiAwarenessContext()",
        "DPI_AWARENESS_CONTEXT_PER_MONITOR",
        "Qt's default DPI awareness",
    )

    def __init__(self, original):
        self._orig = original

    def write(self, s):
        if any(p in s for p in self._NOISE_PHRASES):
            return  # 노이즈는 폐기
        return self._orig.write(s)

    def flush(self):
        return self._orig.flush()

    def __getattr__(self, name):
        return getattr(self._orig, name)


# pykrx는 print() 함수 사용 → 기본적으로 stdout으로 나감.
# stdout, stderr 둘 다 필터로 감싸 (그리고 backup reference도)
_orig_stdout = sys.stdout
_orig_stderr = sys.stderr
sys.stdout = _PykrxNoiseFilter(_orig_stdout)
sys.stderr = _PykrxNoiseFilter(_orig_stderr)
try:
    sys.__stdout__ = sys.stdout
    sys.__stderr__ = sys.stderr
except (AttributeError, TypeError):
    pass


# --- 일시 stderr 차단 컨텍스트 (네트워크 호출 시 잠깐 끄고 싶을 때) ---
@_contextlib.contextmanager
def _silence_stderr():
    """fd 2까지 일시 차단 (필터로 안 잡히는 OS-level write 대비)."""
    import os as _os
    import sys as _sys
    old_stderr = _sys.stderr
    old_under = _sys.__stderr__
    sink = _io.StringIO()
    _sys.stderr = sink
    _sys.__stderr__ = sink
    old_fd = _os.dup(2)
    devnull_fd = _os.open(_os.devnull, _os.O_WRONLY)
    _os.dup2(devnull_fd, 2)
    try:
        yield
    finally:
        _sys.stderr = old_stderr
        _sys.__stderr__ = old_under
        _os.dup2(old_fd, 2)
        _os.close(old_fd)
        _os.close(devnull_fd)


import duckdb
import pandas as pd
from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
import threading as _threading
import time as _time
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFormLayout, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMainWindow, QPushButton, QSplitter,
    QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)
import finplot as fplt
# pykrx는 import 시점에도 KRX_ID/PW 미설정 메시지를 print하므로 silence 안에서 import
with _silence_stderr():
    from pykrx import stock

# --- finplot 표시 timezone 고정 -------------------------------------------
# 우리 데이터(yfinance, KIS, KRX OpenAPI)는 모두 KST naive로 저장됨.
# pandas는 naive datetime을 ns로 변환할 때 UTC로 가정 → ns값이 'UTC 11:45'로 저장됨.
# finplot 기본 display_timezone=tzlocal()=KST면 fromtimestamp가 +9 더해 20:45로 표시.
# UTC로 고정하면 표시값과 데이터값이 같은 숫자로 보임 (사용자 KST 시각과 일치).
import datetime as _dt_mod
fplt.display_timezone = _dt_mod.timezone.utc

# --- 한국 시장 관례: 상승=빨강, 하락=파랑 (캔들·거래량 색상) -------------
fplt.candle_bull_color = "#dd2200"        # 상승봉 외곽선
fplt.candle_bull_body_color = "#dd2200"   # 상승봉 채움
fplt.candle_bear_color = "#0066dd"        # 하락봉 외곽선
fplt.candle_bear_body_color = "#0066dd"   # 하락봉 채움
fplt.volume_bull_color = "#ff9988"
fplt.volume_bull_body_color = "#ff9988"
fplt.volume_bear_color = "#88aaff"
fplt.volume_bear_body_color = "#88aaff"

# --- X축 시간 ticks: 5분봉에서도 줌 레벨에 따라 2h/3h/6h 단위 표시 ----------
# 기본 finplot은 3일 이상 보이면 곧장 일자(D) ticks로 전환 → 분봉에서 시간 정보 사라짐.
# 'days' threshold를 7일로 올리고, 그 사이를 6h/3h/2h ticks로 채움.
fplt.time_splits = [
    ('years',    63072000, 'YS',    4),
    ('months',    7776000, 'MS',   10),
    ('weeks',     1814400, 'W-MON',10),
    ('days',       604800, 'D',    10),   # > 7일: 날짜만
    ('hours',      259200, '6h',   16),   # 3~7일: 6시간
    ('hours',       86400, '3h',   16),   # 1~3일: 3시간
    ('hours',       32400, '2h',   16),   # 9시간~1일: 2시간
    ('hours',       10800, 'h',    16),   # 3~9시간: 1시간
    ('minutes',      2700, '15min',16),
    ('minutes',       900, '5min', 16),
    ('minutes',       180, 'min',  16),
    ('seconds',        45, '15s',  19),
    ('seconds',        15, '5s',   19),
    ('seconds',         3, 's',    19),
    ('milliseconds',    0, 'ms',   23),
]

# --- 설정 -----------------------------------------------------------------
DB_PATH = Path(__file__).parent / "datachart.duckdb"
DEFAULT_CODE = "489790"   # 한화비전
DEFAULT_DAYS = 365        # 1년치
CACHE_FRESH_DAYS = 5      # 최근 N일 이내 캐시면 재사용


# --- DB 스키마 -------------------------------------------------------------
SCHEMA_SQL = [
    """
    CREATE TABLE IF NOT EXISTS ohlcv_daily (
        code   VARCHAR,
        date   DATE,
        open   DOUBLE,
        high   DOUBLE,
        low    DOUBLE,
        close  DOUBLE,
        volume BIGINT,
        PRIMARY KEY (code, date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS investor_value_daily (
        code        VARCHAR,
        date        DATE,
        individual  BIGINT,   -- 개인 순매수 거래대금
        foreign_all BIGINT,   -- 외국인합계
        institution BIGINT,   -- 기관합계
        financial   BIGINT,   -- 금융투자
        pension     BIGINT,   -- 연기금 등
        other_corp  BIGINT,   -- 기타법인
        PRIMARY KEY (code, date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fundamental_daily (
        code  VARCHAR,
        date  DATE,
        bps   DOUBLE,
        per   DOUBLE,
        pbr   DOUBLE,
        eps   DOUBLE,
        div_y DOUBLE,   -- DIV (배당수익률 %)
        dps   DOUBLE,
        PRIMARY KEY (code, date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS marketcap_daily (
        code        VARCHAR,
        date        DATE,
        market_cap  BIGINT,
        volume      BIGINT,
        value       BIGINT,
        listed_shr  BIGINT,
        PRIMARY KEY (code, date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS foreign_rate_daily (
        code         VARCHAR,
        date         DATE,
        listed_shr   BIGINT,
        limit_shr    BIGINT,
        foreign_shr  BIGINT,
        limit_rate   DOUBLE,
        foreign_rate DOUBLE,
        exhaust_rate DOUBLE,
        PRIMARY KEY (code, date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS krx_index_daily (
        date       DATE,
        idx_class  VARCHAR,   -- 계열구분 (예: KOSPI 시리즈)
        idx_name   VARCHAR,   -- 지수명 (예: 코스피, 코스피 200)
        open       DOUBLE,
        high       DOUBLE,
        low        DOUBLE,
        close      DOUBLE,
        cmp_prev   DOUBLE,    -- 전일대비
        fluc_rate  DOUBLE,    -- 등락률
        volume     BIGINT,
        value      BIGINT,    -- 거래대금
        market_cap BIGINT,
        PRIMARY KEY (date, idx_name)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ohlcv_minute (
        code     VARCHAR,
        datetime TIMESTAMP,
        open     DOUBLE,
        high     DOUBLE,
        low      DOUBLE,
        close    DOUBLE,
        volume   BIGINT,    -- 분 누적 거래량
        PRIMARY KEY (code, datetime)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS naver_investor_flow (
        code            VARCHAR,
        date            DATE,
        close           DOUBLE,
        volume          BIGINT,
        foreign_net     BIGINT,   -- 외국인 순매수(주)
        institution_net BIGINT,   -- 기관 순매수(주)
        foreign_qty     BIGINT,   -- 외국인 보유주식수
        foreign_rate    DOUBLE,   -- 외국인 지분율(%)
        PRIMARY KEY (code, date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS naver_snapshot (
        code          VARCHAR,
        fetched_at    TIMESTAMP,
        name          VARCHAR,
        price         DOUBLE,
        change_pct    DOUBLE,
        per           DOUBLE,
        pbr           DOUBLE,
        eps           DOUBLE,
        market_cap    BIGINT,
        foreign_rate  DOUBLE,
        PRIMARY KEY (code)
    )
    """,
    """
    -- 종목코드 ↔ 종목명 마스터 (자동완성용)
    CREATE TABLE IF NOT EXISTS ticker_master (
        code   VARCHAR PRIMARY KEY,
        name   VARCHAR,
        market VARCHAR,
        updated_at TIMESTAMP
    )
    """,
]


# 사전 시드: KRX 시총 상위 + 자주 거래되는 종목 (앱 첫 실행 시 즉시 자동완성 가능)
TICKER_SEED = [
    ("005930", "삼성전자"), ("000660", "SK하이닉스"), ("373220", "LG에너지솔루션"),
    ("207940", "삼성바이오로직스"), ("005380", "현대차"), ("006400", "삼성SDI"),
    ("051910", "LG화학"), ("000270", "기아"), ("035420", "NAVER"),
    ("035720", "카카오"), ("105560", "KB금융"), ("055550", "신한지주"),
    ("012330", "현대모비스"), ("028260", "삼성물산"), ("068270", "셀트리온"),
    ("005490", "POSCO홀딩스"), ("003670", "포스코퓨처엠"), ("066570", "LG전자"),
    ("003550", "LG"), ("015760", "한국전력"), ("032830", "삼성생명"),
    ("017670", "SK텔레콤"), ("034730", "SK"), ("018260", "삼성에스디에스"),
    ("009150", "삼성전기"), ("011200", "HMM"), ("033780", "KT&G"),
    ("030200", "KT"), ("086790", "하나금융지주"), ("316140", "우리금융지주"),
    ("000810", "삼성화재"), ("024110", "기업은행"), ("259960", "크래프톤"),
    ("352820", "하이브"), ("377300", "카카오페이"), ("323410", "카카오뱅크"),
    ("042700", "한미반도체"), ("000720", "현대건설"), ("097950", "CJ제일제당"),
    ("180640", "한진칼"), ("011170", "롯데케미칼"), ("251270", "넷마블"),
    ("036570", "엔씨소프트"), ("293490", "카카오게임즈"), ("112040", "위메이드"),
    ("009830", "한화솔루션"), ("000880", "한화"), ("489790", "한화비전"),
    ("272210", "한화시스템"), ("079550", "LIG넥스원"), ("047810", "한국항공우주"),
    ("064350", "현대로템"), ("042660", "한화오션"), ("329180", "HD현대중공업"),
    ("267260", "HD현대일렉트릭"), ("241560", "두산밥캣"), ("034020", "두산에너빌리티"),
    ("000150", "두산"), ("267250", "HD현대"), ("010140", "삼성중공업"),
    ("009540", "HD한국조선해양"), ("010950", "S-Oil"), ("096770", "SK이노베이션"),
    ("078930", "GS"), ("004020", "현대제철"), ("001040", "CJ"),
    ("139480", "이마트"), ("282330", "BGF리테일"), ("004170", "신세계"),
    ("071050", "한국금융지주"), ("006800", "미래에셋증권"), ("016360", "삼성증권"),
    ("088980", "맥쿼리인프라"),
    # KOSDAQ 상위
    ("247540", "에코프로비엠"), ("086520", "에코프로"), ("196170", "알테오젠"),
    ("091990", "셀트리온헬스케어"), ("066970", "엘앤에프"), ("028300", "HLB"),
    ("058470", "리노공업"), ("357780", "솔브레인"), ("145020", "휴젤"),
    ("214150", "클래시스"), ("293480", "하나마이크론"), ("277810", "레인보우로보틱스"),
    ("095340", "ISC"), ("141080", "리가켐바이오"),
]


def init_db() -> None:
    con = duckdb.connect(str(DB_PATH))
    try:
        for sql in SCHEMA_SQL:
            con.execute(sql)
    finally:
        con.close()


# --- pykrx 컬럼 매핑 -------------------------------------------------------
# pykrx 한국어 컬럼 → 영문 컬럼
INVESTOR_COLS = {
    "개인": "individual",
    "외국인합계": "foreign_all",
    "기관합계": "institution",
    "금융투자": "financial",
    "연기금 등": "pension",
    "연기금등": "pension",
    "기타법인": "other_corp",
}
FUND_COLS = {
    "BPS": "bps", "PER": "per", "PBR": "pbr",
    "EPS": "eps", "DIV": "div_y", "DPS": "dps",
}
CAP_COLS = {
    "시가총액": "market_cap",
    "거래량": "volume",
    "거래대금": "value",
    "상장주식수": "listed_shr",
}
FOREIGN_COLS = {
    "상장주식수": "listed_shr",
    "한도수량": "limit_shr",
    "보유수량": "foreign_shr",
    "지분율": "foreign_rate",
    "한도소진률": "exhaust_rate",
    "한도소진율": "exhaust_rate",
}


# --- 캐시 정책 -------------------------------------------------------------
def _cache_fresh(con, table: str, code: str, start, end) -> pd.DataFrame | None:
    """캐시가 있고 최신이면 반환, 아니면 None."""
    df = con.execute(
        f"SELECT * FROM {table} WHERE code = ? AND date BETWEEN ? AND ? ORDER BY date",
        [code, start, end],
    ).df()
    if df.empty:
        return None
    latest = pd.to_datetime(df["date"].max()).date()
    if (end - latest).days <= CACHE_FRESH_DAYS:
        return df
    return None


def _ensure_cols(df: pd.DataFrame, mapping: dict, required: list[str]) -> pd.DataFrame:
    """pykrx 한글 컬럼을 영문으로 rename하고 누락 컬럼은 NaN으로 채움."""
    df = df.rename(columns=mapping)
    for col in required:
        if col not in df.columns:
            df[col] = pd.NA
    return df


# --- 데이터 페처 (테이블별) ------------------------------------------------
def fetch_ohlcv(code: str, days: int = DEFAULT_DAYS, progress_cb=None) -> pd.DataFrame:
    """OHLCV 우선순위: KRX OpenAPI(키 있을 때) → pykrx 폴백."""
    # 1순위: KRX OpenAPI (정식 데이터, 키 필요)
    if os.environ.get("KRX_API_KEY"):
        try:
            df = fetch_ohlcv_via_krx_api(code, days, progress_cb=progress_cb)
            if not df.empty:
                return df
        except Exception:
            pass

    # 2순위: pykrx 스크래핑 (인증 불필요, 항상 백업)
    end = _dt.now().date()
    start = end - timedelta(days=days)
    con = duckdb.connect(str(DB_PATH))
    try:
        cached = _cache_fresh(con, "ohlcv_daily", code, start, end)
        if cached is not None:
            return cached.drop(columns=["code"]).reset_index(drop=True)

        with _silence_stderr():
            df = stock.get_market_ohlcv(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), code)
        if df.empty:
            raise ValueError(f"종목 {code} OHLCV 데이터 없음")
        df = df.reset_index()
        df.columns = ["date", "open", "high", "low", "close", "volume", "change_rate"]
        df = df[["date", "open", "high", "low", "close", "volume"]].copy()
        df.insert(0, "code", code)
        con.execute("INSERT OR REPLACE INTO ohlcv_daily SELECT * FROM df")
        return df.drop(columns=["code"]).reset_index(drop=True)
    finally:
        con.close()


def fetch_investor(code: str, days: int = DEFAULT_DAYS) -> pd.DataFrame:
    end = datetime.now().date()
    start = end - timedelta(days=days)
    con = duckdb.connect(str(DB_PATH))
    try:
        cached = _cache_fresh(con, "investor_value_daily", code, start, end)
        if cached is not None:
            return cached.drop(columns=["code"]).reset_index(drop=True)

        # 투자자별 순매수 거래대금 (원). KRX OpenAPI 인증 필요 → 실패 시 빈 DataFrame
        try:
            with _silence_stderr():
                df = stock.get_market_trading_value_by_date(
                    start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), code
                )
        except Exception:
            return pd.DataFrame()
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.reset_index()
        df = df.rename(columns={"날짜": "date"})
        cols = ["individual", "foreign_all", "institution", "financial", "pension", "other_corp"]
        df = _ensure_cols(df, INVESTOR_COLS, cols)
        df = df[["date"] + cols].copy()
        for c in cols:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype("int64")
        df.insert(0, "code", code)
        con.execute("INSERT OR REPLACE INTO investor_value_daily SELECT * FROM df")
        return df.drop(columns=["code"]).reset_index(drop=True)
    finally:
        con.close()


def fetch_fundamental(code: str, days: int = DEFAULT_DAYS) -> pd.DataFrame:
    end = datetime.now().date()
    start = end - timedelta(days=days)
    con = duckdb.connect(str(DB_PATH))
    try:
        cached = _cache_fresh(con, "fundamental_daily", code, start, end)
        if cached is not None:
            return cached.drop(columns=["code"]).reset_index(drop=True)

        try:
            with _silence_stderr():
                df = stock.get_market_fundamental(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), code)
        except Exception:
            return pd.DataFrame()
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.reset_index().rename(columns={"날짜": "date"})
        cols = ["bps", "per", "pbr", "eps", "div_y", "dps"]
        df = _ensure_cols(df, FUND_COLS, cols)
        df = df[["date"] + cols].copy()
        df.insert(0, "code", code)
        con.execute("INSERT OR REPLACE INTO fundamental_daily SELECT * FROM df")
        return df.drop(columns=["code"]).reset_index(drop=True)
    finally:
        con.close()


def fetch_marketcap(code: str, days: int = DEFAULT_DAYS) -> pd.DataFrame:
    end = datetime.now().date()
    start = end - timedelta(days=days)
    con = duckdb.connect(str(DB_PATH))
    try:
        cached = _cache_fresh(con, "marketcap_daily", code, start, end)
        if cached is not None:
            return cached.drop(columns=["code"]).reset_index(drop=True)

        try:
            with _silence_stderr():
                df = stock.get_market_cap(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), code)
        except Exception:
            return pd.DataFrame()
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.reset_index().rename(columns={"날짜": "date"})
        cols = ["market_cap", "volume", "value", "listed_shr"]
        df = _ensure_cols(df, CAP_COLS, cols)
        df = df[["date"] + cols].copy()
        for c in cols:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype("int64")
        df.insert(0, "code", code)
        con.execute("INSERT OR REPLACE INTO marketcap_daily SELECT * FROM df")
        return df.drop(columns=["code"]).reset_index(drop=True)
    finally:
        con.close()


def fetch_foreign_rate(code: str, days: int = DEFAULT_DAYS) -> pd.DataFrame:
    end = datetime.now().date()
    start = end - timedelta(days=days)
    con = duckdb.connect(str(DB_PATH))
    try:
        cached = _cache_fresh(con, "foreign_rate_daily", code, start, end)
        if cached is not None:
            return cached.drop(columns=["code"]).reset_index(drop=True)

        try:
            with _silence_stderr():
                df = stock.get_exhaustion_rates_of_foreign_investment(
                    start.strftime("%Y%m%d"), end.strftime("%Y%m%d"), code
                )
        except Exception:
            return pd.DataFrame()
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.reset_index().rename(columns={"날짜": "date"})
        cols = ["listed_shr", "limit_shr", "foreign_shr", "foreign_rate", "exhaust_rate"]
        df = _ensure_cols(df, FOREIGN_COLS, cols)
        if "limit_rate" not in df.columns:
            df["limit_rate"] = pd.NA
        df = df[["date", "listed_shr", "limit_shr", "foreign_shr",
                 "limit_rate", "foreign_rate", "exhaust_rate"]].copy()
        for c in ["listed_shr", "limit_shr", "foreign_shr"]:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0).astype("int64")
        for c in ["limit_rate", "foreign_rate", "exhaust_rate"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df.insert(0, "code", code)
        con.execute("INSERT OR REPLACE INTO foreign_rate_daily SELECT * FROM df")
        return df.drop(columns=["code"]).reset_index(drop=True)
    finally:
        con.close()


# --- KRX OpenAPI (openapi.krx.co.kr) 클라이언트 ---------------------------
import contextlib as _contextlib
import io as _io
import json as _json
import urllib.error
import urllib.parse as _urlparse


# _silence_stderr는 파일 상단에서 pykrx import 전에 이미 정의됨

KRX_API_BASE = "https://data-dbg.krx.co.kr/svc/apis"


# 401 받은 서비스만 그 세션에서 차단 (시간 낭비 방지). 다른 서비스는 정상 호출.
_krx_unauthorized_services: set[str] = set()


def _krx_api_get(service: str, **params) -> dict | None:
    """KRX OpenAPI 호출.
    - 환경변수 KRX_API_KEY 필수
    - 서비스별 401 캐싱: 한 서비스가 401이어도 다른 서비스는 계속 시도
    """
    key = os.environ.get("KRX_API_KEY")
    if not key or service in _krx_unauthorized_services:
        return None
    key = key.strip()
    qs = _urlparse.urlencode(params)
    url = f"{KRX_API_BASE}/{service}" + (f"?{qs}" if qs else "")
    req = _urlreq.Request(url, headers={"AUTH_KEY": key, "Accept": "application/json"})
    try:
        with _urlreq.urlopen(req, timeout=15) as r:
            body = r.read().decode("utf-8", errors="replace")
        return _json.loads(body)
    except urllib.error.HTTPError as e:
        if e.code == 401:
            _krx_unauthorized_services.add(service)
        return None
    except Exception:
        return None


def krx_api_diagnose() -> dict:
    """KRX OpenAPI 키 동작 진단. 401(권한 없음)·404·정상을 명시적으로 구분."""
    key = os.environ.get("KRX_API_KEY")
    if not key:
        return {"status": "no_key", "message": "KRX_API_KEY 환경변수 미설정"}
    key = key.strip()
    # 최근 영업일 한 번만 시도하고 HTTP 코드 그대로 보기
    # idx/kospi_dd_trd는 KOSPI 시리즈 지수 일별시세 (보통 가장 먼저 승인되는 무료 서비스)
    last_status = None
    last_body = ""
    saw_ok_empty = False  # 200 OK 받았지만 데이터가 빈 적이 있는지
    for back in range(1, 14):  # 최근 영업일은 데이터 미반영일 수 있어 1일 전부터, 2주 거슬러 시도
        d = (_dt.now() - timedelta(days=back)).strftime("%Y%m%d")
        url = f"{KRX_API_BASE}/idx/kospi_dd_trd?basDd={d}"
        req = _urlreq.Request(url, headers={"AUTH_KEY": key, "Accept": "application/json"})
        try:
            with _urlreq.urlopen(req, timeout=10) as r:
                body = r.read().decode("utf-8", errors="replace")
            data = _json.loads(body)
            if isinstance(data, dict) and "OutBlock_1" in data:
                rows = data["OutBlock_1"]
                if not rows:
                    saw_ok_empty = True
                    continue  # 다른 날짜 시도
                return {
                    "status": "ok",
                    "tested_date": d,
                    "rows": len(rows),
                    "sample_keys": list(rows[0].keys())[:12],
                    "first_row_sample": {k: rows[0][k] for k in list(rows[0].keys())[:6]},
                }
            last_status, last_body = 200, body[:300]
        except urllib.error.HTTPError as e:
            last_status = e.code
            try:
                last_body = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                last_body = ""
            if e.code == 401:
                return {
                    "status": "unauthorized",
                    "http_code": 401,
                    "message": "키는 인식되지만 사용 권한 없음. KRX 마이페이지에서 서비스(sto/stk_bydd_trd 등) 신청 + 승인 대기 필요.",
                    "response": last_body,
                }
        except Exception as e:
            return {"status": "network_error", "error": f"{type(e).__name__}: {e}"}
    if saw_ok_empty:
        return {
            "status": "ok_no_recent_data",
            "message": "키/권한 정상이지만 최근 2주 영업일 데이터가 비어있음 (서비스 백필 대기 가능성).",
        }
    return {
        "status": "fail",
        "http_code": last_status,
        "response": last_body,
        "message": "응답 없음/예상 외 형식. KRX 서비스 신청 상태 확인 필요.",
    }


def fetch_krx_ohlcv_day(basDd: str, code: str | None = None) -> pd.DataFrame:
    """KRX OpenAPI 일별 매매 (특정일자 전 종목). code 지정 시 해당 종목만 필터."""
    result = _krx_api_get("sto/stk_bydd_trd", basDd=basDd)
    if not result or "OutBlock_1" not in result:
        return pd.DataFrame()
    df = pd.DataFrame(result["OutBlock_1"])
    if df.empty:
        return df
    if code and "ISU_SRT_CD" in df.columns:
        df = df[df["ISU_SRT_CD"] == code].copy()
    return df


# KRX OpenAPI 시장별 일별매매 서비스
_KRX_MARKET_SERVICES = {
    "KOSPI": "sto/stk_bydd_trd",
    "KOSDAQ": "sto/ksq_bydd_trd",
    "KONEX": "sto/knx_bydd_trd",
}


def _krx_num(v) -> float | None:
    """KRX 응답 숫자 파싱 (문자열에 쉼표·공백 포함 가능, 빈 값은 None)."""
    if v is None or v == "" or v == "-":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).replace(",", "").replace(" ", "").strip()
    if not s or s == "-":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _detect_market(code: str) -> str | None:
    """최근 영업일 한 번 호출해 종목이 어느 시장에 있는지 탐지. KOSPI/KOSDAQ/KONEX/None."""
    for back in range(0, 7):
        d = (_dt.now() - timedelta(days=back)).strftime("%Y%m%d")
        for market, svc in _KRX_MARKET_SERVICES.items():
            res = _krx_api_get(svc, basDd=d)
            if not res or "OutBlock_1" not in res:
                continue
            for row in res["OutBlock_1"]:
                if row.get("ISU_SRT_CD") == code:
                    return market
    return None


def fetch_krx_index_daily(idx_name: str = "코스피", days: int = DEFAULT_DAYS,
                          progress_cb=None) -> pd.DataFrame:
    """KRX OpenAPI idx/kospi_dd_trd 또는 idx/kosdaq_dd_trd로 지수 일별시세 누적.
    - idx_name: '코스피' / '코스피 200' / '코스닥' 등
    - KOSDAQ 시리즈는 KOSDAQ 서비스 승인 시에만 동작 (자동 시도)
    - 캐시(krx_index_daily) 우선, 누락된 평일만 호출
    """
    if not os.environ.get("KRX_API_KEY"):
        return pd.DataFrame()

    end = _dt.now().date()
    start = end - timedelta(days=days)
    con = duckdb.connect(str(DB_PATH))
    try:
        cached = con.execute(
            "SELECT date FROM krx_index_daily WHERE idx_name = ? AND date BETWEEN ? AND ?",
            [idx_name, start, end],
        ).df()
        cached_set = set(pd.to_datetime(cached["date"]).dt.date) if not cached.empty else set()

        target_dates = []
        d = start
        while d <= end:
            if d.weekday() < 5 and d not in cached_set:
                target_dates.append(d)
            d += timedelta(days=1)

        if target_dates:
            # 승인된 지수 서비스 우선 시도 — KRX(통합) → KOSPI → KOSDAQ
            # 401 한 번 만나면 _krx_api_get가 같은 세션에서 자동 차단
            services = ["idx/krx_dd_trd", "idx/kospi_dd_trd", "idx/kosdaq_dd_trd"]
            new_rows: list = []
            total = len(target_dates)
            for i, d in enumerate(target_dates):
                if progress_cb:
                    progress_cb(i + 1, total)
                hit = False
                for svc in services:
                    res = _krx_api_get(svc, basDd=d.strftime("%Y%m%d"))
                    if not res or "OutBlock_1" not in res or not res["OutBlock_1"]:
                        continue
                    for row in res["OutBlock_1"]:
                        if row.get("IDX_NM") != idx_name:
                            continue
                        c = _krx_num(row.get("CLSPRC_IDX"))
                        if c is None:
                            continue
                        new_rows.append({
                            "date": d,
                            "idx_class": row.get("IDX_CLSS", ""),
                            "idx_name": idx_name,
                            "open":  _krx_num(row.get("OPNPRC_IDX")) or c,
                            "high":  _krx_num(row.get("HGPRC_IDX")) or c,
                            "low":   _krx_num(row.get("LWPRC_IDX")) or c,
                            "close": c,
                            "cmp_prev":  _krx_num(row.get("CMPPREVDD_IDX")),
                            "fluc_rate": _krx_num(row.get("FLUC_RT")),
                            "volume":     int(_krx_num(row.get("ACC_TRDVOL")) or 0),
                            "value":      int(_krx_num(row.get("ACC_TRDVAL")) or 0),
                            "market_cap": int(_krx_num(row.get("MKTCAP")) or 0),
                        })
                        hit = True
                        break
                    if hit:
                        break

            if new_rows:
                df = pd.DataFrame(new_rows)
                con.execute(
                    "INSERT OR REPLACE INTO krx_index_daily "
                    "(date, idx_class, idx_name, open, high, low, close, "
                    " cmp_prev, fluc_rate, volume, value, market_cap) "
                    "SELECT date, idx_class, idx_name, open, high, low, close, "
                    "       cmp_prev, fluc_rate, volume, value, market_cap FROM df"
                )

        return con.execute(
            "SELECT date, open, high, low, close, volume, value, market_cap "
            "FROM krx_index_daily WHERE idx_name = ? AND date BETWEEN ? AND ? "
            "ORDER BY date",
            [idx_name, start, end],
        ).df().reset_index(drop=True)
    finally:
        con.close()


def fetch_ohlcv_via_krx_api(code: str, days: int = DEFAULT_DAYS,
                            progress_cb=None) -> pd.DataFrame:
    """KRX OpenAPI로 OHLCV 누적. 캐시에 없는 날짜만 호출.
    progress_cb(done, total): 호출자가 상태바 업데이트용으로 받음.
    """
    if not os.environ.get("KRX_API_KEY"):
        return pd.DataFrame()

    end = _dt.now().date()
    start = end - timedelta(days=days)
    con = duckdb.connect(str(DB_PATH))
    try:
        # 이미 캐시된 평일 날짜 집합
        cached = con.execute(
            "SELECT date FROM ohlcv_daily WHERE code = ? AND date BETWEEN ? AND ?",
            [code, start, end],
        ).df()
        cached_set = set(pd.to_datetime(cached["date"]).dt.date) if not cached.empty else set()

        # 호출할 평일만 (주말은 어차피 데이터 없음)
        target_dates: list = []
        d = start
        while d <= end:
            if d.weekday() < 5 and d not in cached_set:
                target_dates.append(d)
            d += timedelta(days=1)

        if not target_dates:
            return con.execute(
                "SELECT date, open, high, low, close, volume FROM ohlcv_daily "
                "WHERE code = ? AND date BETWEEN ? AND ? ORDER BY date",
                [code, start, end],
            ).df().reset_index(drop=True)

        market = _detect_market(code)
        if not market:
            return pd.DataFrame()  # 종목 못 찾음
        service = _KRX_MARKET_SERVICES[market]

        new_rows: list = []
        total = len(target_dates)
        for i, d in enumerate(target_dates):
            if progress_cb:
                progress_cb(i + 1, total)
            res = _krx_api_get(service, basDd=d.strftime("%Y%m%d"))
            if not res or "OutBlock_1" not in res:
                continue
            for row in res["OutBlock_1"]:
                if row.get("ISU_SRT_CD") != code:
                    continue
                op = _krx_num(row.get("TDD_OPNPRC"))
                hi = _krx_num(row.get("TDD_HGPRC"))
                lo = _krx_num(row.get("TDD_LWPRC"))
                cl = _krx_num(row.get("TDD_CLSPRC"))
                vol = _krx_num(row.get("ACC_TRDVOL"))
                if cl is None:
                    break
                new_rows.append({
                    "code": code, "date": d,
                    "open": op or cl, "high": hi or cl, "low": lo or cl,
                    "close": cl, "volume": int(vol or 0),
                })
                break

        if new_rows:
            df = pd.DataFrame(new_rows)
            con.execute(
                "INSERT OR REPLACE INTO ohlcv_daily "
                "(code, date, open, high, low, close, volume) "
                "SELECT code, date, open, high, low, close, volume FROM df"
            )

        return con.execute(
            "SELECT date, open, high, low, close, volume FROM ohlcv_daily "
            "WHERE code = ? AND date BETWEEN ? AND ? ORDER BY date",
            [code, start, end],
        ).df().reset_index(drop=True)
    finally:
        con.close()


# --- Naver Finance 스크래퍼 (KRX OpenAPI 인증 우회용) ---------------------
import re as _re
import urllib.request as _urlreq
from datetime import datetime as _dt

_NAVER_URL = "https://finance.naver.com/item/main.nhn?code={code}"
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) DataChart/2.0"}
_NAVER_FRESH_HOURS = 2  # 스냅샷 캐시 유효시간


def _http_get(url: str, encoding: str = "utf-8") -> str:
    req = _urlreq.Request(url, headers=_UA)
    with _urlreq.urlopen(req, timeout=10) as r:
        return r.read().decode(encoding, errors="replace")


def _parse_num(s: str | None) -> float | None:
    if not s:
        return None
    s = s.replace(",", "").replace("%", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def _parse_market_cap(html: str) -> int | None:
    """Naver 시가총액 형식 '4조 3,370' (단위 억원) → 정수(원)."""
    m = _re.search(r'id="_market_sum"[^>]*>([\s\S]{0,300}?)</em>', html)
    if not m:
        return None
    raw = _re.sub(r"\s+", "", m.group(1))  # "4조3,370"
    cho_m = _re.search(r"(\d[\d,]*)조", raw)
    cho_val = int(cho_m.group(1).replace(",", "")) if cho_m else 0
    after_cho = _re.sub(r"\d[\d,]*조", "", raw).replace(",", "")
    eok_m = _re.match(r"(\d+)", after_cho)
    eok = int(eok_m.group(1)) if eok_m else 0
    return cho_val * 10**12 + eok * 10**8


def fetch_naver_snapshot(code: str) -> dict:
    """Naver 종목 메인에서 종목명/PER/PBR/EPS/시총/외국인소진율 스냅샷.
    KRX OpenAPI 인증이 없을 때 펀더멘털 탭을 채우기 위한 우회 경로."""
    con = duckdb.connect(str(DB_PATH))
    try:
        cached = con.execute(
            "SELECT * FROM naver_snapshot WHERE code = ?", [code]
        ).df()
        if not cached.empty:
            ts = pd.to_datetime(cached["fetched_at"].iloc[0])
            age_hr = (_dt.now() - ts.to_pydatetime()).total_seconds() / 3600
            if age_hr < _NAVER_FRESH_HOURS:
                row = cached.iloc[0].to_dict()
                row.pop("fetched_at", None)
                return row

        html = _http_get(_NAVER_URL.format(code=code))

        def _id(tid):
            m = _re.search(rf'id="{tid}"[^>]*>([^<]+)<', html)
            return m.group(1).strip() if m else None

        # 종목명: <title>한화비전 : Npay 증권</title> 형태
        m = _re.search(r"<title>([^:<]+)\s*:", html)
        name = m.group(1).strip() if m else code

        # 현재가 / 등락률 (main 페이지에 있음)
        m = _re.search(r'id="_nowVal"[^>]*>([\d,\.]+)<', html)
        price = _parse_num(m.group(1)) if m else None
        m = _re.search(r'id="_rate"[^>]*>[\s\S]{0,80}?([\-\+]?\d+\.\d+)', html)
        change_pct = _parse_num(m.group(1)) if m else None

        per = _parse_num(_id("_per"))
        pbr = _parse_num(_id("_pbr"))
        eps = _parse_num(_id("_eps"))
        cap = _parse_market_cap(html)

        # 외국인소진율 (B/A): main 페이지의 "외국인소진율(B/A)" 행 td><em>NN.NN%</em>
        foreign_rate = None
        m = _re.search(r"외국인소진율[\s\S]{0,2000}?<td>\s*<em>\s*(\d+\.\d+)\s*%", html)
        if m:
            foreign_rate = _parse_num(m.group(1))

        snapshot = {
            "code": code,
            "fetched_at": _dt.now(),
            "name": name,
            "price": price,
            "change_pct": change_pct,
            "per": per,
            "pbr": pbr,
            "eps": eps,
            "market_cap": int(cap) if cap else None,
            "foreign_rate": foreign_rate,
        }
        con.execute("INSERT OR REPLACE INTO naver_snapshot VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [snapshot["code"], snapshot["fetched_at"], snapshot["name"],
                     snapshot["price"], snapshot["change_pct"],
                     snapshot["per"], snapshot["pbr"], snapshot["eps"],
                     snapshot["market_cap"], snapshot["foreign_rate"]])
        return snapshot
    except Exception as ex:
        return {"code": code, "name": code, "error": str(ex)}
    finally:
        con.close()


# --- Naver 분봉/주봉/월봉 차트 데이터 (siseJson.naver) ---------------------
import ast as _ast

_NAVER_SISE_JSON = (
    "https://api.finance.naver.com/siseJson.naver?"
    "symbol={code}&requestType=1&startTime={start}&endTime={end}&timeframe={tf}"
)
MINUTE_FRESH_MINUTES = 5      # 1분봉 캐시 신선도


def _parse_naver_sise_json(txt: str) -> list[list]:
    """Naver siseJson 응답 파싱 (느슨한 JSON-비슷한 Python literal)."""
    body = txt.replace("null", "None").strip()
    body = _re.sub(r"^\s*\[", "[", body, count=1)
    try:
        return _ast.literal_eval(body)
    except (SyntaxError, ValueError):
        # 행 단위 정규식 폴백
        rows: list[list] = []
        for m in _re.finditer(r'\["(\d+)",\s*(.+?)\]', body):
            ts = m.group(1)
            parts = [p.strip() for p in m.group(2).split(",")]
            row: list = [ts]
            for p in parts:
                if p == "None":
                    row.append(None)
                else:
                    try:
                        row.append(float(p) if "." in p else int(p))
                    except ValueError:
                        row.append(None)
            rows.append(row)
        return [["날짜"]] + rows


def fetch_minute_naver(code: str, days: int = 5) -> pd.DataFrame:
    """Naver 1분봉. close + 누적 volume만 신뢰 가능 (OHL은 null인 경우 많음).
    days: 최근 N일치. 분봉은 자주 변하므로 5분 캐시."""
    con = duckdb.connect(str(DB_PATH))
    try:
        end_dt = _dt.now()
        start_dt = end_dt - timedelta(days=days)

        cached = con.execute(
            """
            SELECT datetime, open, high, low, close, volume
              FROM ohlcv_minute
             WHERE code = ? AND datetime BETWEEN ? AND ?
             ORDER BY datetime
            """,
            [code, start_dt, end_dt],
        ).df()
        if not cached.empty:
            latest = pd.to_datetime(cached["datetime"].max())
            age_min = (end_dt - latest.to_pydatetime()).total_seconds() / 60
            if age_min < MINUTE_FRESH_MINUTES:
                return cached.reset_index(drop=True)

        url = _NAVER_SISE_JSON.format(
            code=code,
            start=start_dt.strftime("%Y%m%d"),
            end=end_dt.strftime("%Y%m%d"),
            tf="minute",
        )
        try:
            txt = _http_get(url, encoding="utf-8")
        except Exception:
            return pd.DataFrame()

        data = _parse_naver_sise_json(txt)
        if not data or len(data) < 2:
            return pd.DataFrame()

        rows = []
        for row in data[1:]:
            if not row or len(row) < 6:
                continue
            ts_str, o, h, l, c, v = row[0], row[1], row[2], row[3], row[4], row[5]
            try:
                # 분봉 timestamp 형식: YYYYMMDDHHMM
                dt = _dt.strptime(str(ts_str), "%Y%m%d%H%M")
            except ValueError:
                continue
            close = c if c is not None else o
            if close is None:
                continue
            rows.append({
                "code": code, "datetime": dt,
                "open": o if o is not None else close,
                "high": h if h is not None else close,
                "low": l if l is not None else close,
                "close": close,
                "volume": int(v) if v is not None else 0,
            })
        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows).sort_values("datetime").reset_index(drop=True)
        con.execute(
            "INSERT OR REPLACE INTO ohlcv_minute "
            "(code, datetime, open, high, low, close, volume) "
            "SELECT code, datetime, open, high, low, close, volume FROM df"
        )
        return df.drop(columns=["code"]).reset_index(drop=True)
    finally:
        con.close()


# --- KIS Developer (한국투자증권 OpenAPI) — 실시간 현재가 ------------------
# 환경변수:
#   KIS_ENV          : "mock"(모의) 또는 "real"(실전, 기본). 미설정 시 키 이름으로 추론
#   KIS_APP_KEY      / KIS_APP_SECRET     : 활성 키 (가장 단순한 등록 방식)
#   KIS_REAL_APP_KEY / KIS_REAL_APP_SECRET: 실전 키 (mock과 동시 보유 시)
#   KIS_MOCK_APP_KEY / KIS_MOCK_APP_SECRET: 모의 키 (실전과 동시 보유 시)
KIS_REAL_BASE = "https://openapi.koreainvestment.com:9443"
KIS_MOCK_BASE = "https://openapivts.koreainvestment.com:29443"
_kis_token_cache: dict = {"token": None, "expire": 0.0, "env": None}


def _kis_resolve() -> tuple[str | None, str | None, str]:
    """현재 활성 (appkey, appsecret, base_url) 반환. KIS_ENV로 모의/실전 분기."""
    env = (os.environ.get("KIS_ENV") or "").strip().lower()
    # 명시 분기: KIS_ENV가 'mock' 또는 'real'
    if env == "mock":
        ak = (os.environ.get("KIS_MOCK_APP_KEY")
              or os.environ.get("KIS_APP_KEY"))
        sk = (os.environ.get("KIS_MOCK_APP_SECRET")
              or os.environ.get("KIS_APP_SECRET"))
        return (ak, sk, KIS_MOCK_BASE)
    if env == "real":
        ak = (os.environ.get("KIS_REAL_APP_KEY")
              or os.environ.get("KIS_APP_KEY"))
        sk = (os.environ.get("KIS_REAL_APP_SECRET")
              or os.environ.get("KIS_APP_SECRET"))
        return (ak, sk, KIS_REAL_BASE)
    # 미설정: 단일 KIS_APP_KEY를 실전으로 가정 (이전 동작 호환)
    ak = os.environ.get("KIS_APP_KEY")
    sk = os.environ.get("KIS_APP_SECRET")
    return (ak, sk, KIS_REAL_BASE)


def kis_env_label() -> str:
    """현재 환경 라벨 — 상태바 표시용."""
    env = (os.environ.get("KIS_ENV") or "").strip().lower()
    if env == "mock":
        return "모의"
    if env == "real":
        return "실전"
    return "실전(기본)"


def kis_get_token() -> str | None:
    """KIS OAuth2 access token. 24시간 유효, 23시간 캐시. 환경 변경 시 재발급."""
    appkey, appsecret, base = _kis_resolve()
    if not appkey or not appsecret:
        return None
    cur_env = (os.environ.get("KIS_ENV") or "real").strip().lower()
    if (_kis_token_cache["token"]
            and _kis_token_cache["expire"] > _time.time()
            and _kis_token_cache.get("env") == cur_env):
        return _kis_token_cache["token"]
    try:
        body = _json.dumps({
            "grant_type": "client_credentials",
            "appkey": appkey.strip(),
            "appsecret": appsecret.strip(),
        }).encode("utf-8")
        req = _urlreq.Request(
            f"{base}/oauth2/tokenP", data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with _urlreq.urlopen(req, timeout=10) as r:
            data = _json.loads(r.read().decode("utf-8"))
        token = data.get("access_token")
        if not token:
            return None
        _kis_token_cache["token"] = token
        _kis_token_cache["expire"] = _time.time() + 23 * 3600
        _kis_token_cache["env"] = cur_env
        return token
    except Exception:
        return None


def kis_current_price(code: str) -> dict | None:
    """KIS 주식 현재가 시세 (FHKST01010100, 모의/실전 동일).
    반환: {price, open, high, low, volume(누적), change_rate}
    """
    token = kis_get_token()
    appkey, appsecret, base = _kis_resolve()
    if not token or not appkey or not appsecret:
        return None
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": appkey.strip(),
        "appsecret": appsecret.strip(),
        "tr_id": "FHKST01010100",
        "Content-Type": "application/json",
    }
    qs = _urlparse.urlencode({"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": code})
    url = f"{base}/uapi/domestic-stock/v1/quotations/inquire-price?{qs}"
    try:
        req = _urlreq.Request(url, headers=headers)
        with _urlreq.urlopen(req, timeout=8) as r:
            data = _json.loads(r.read().decode("utf-8"))
        out = data.get("output") or {}
        if not out.get("stck_prpr"):
            return None
        return {
            "price":   _krx_num(out.get("stck_prpr")),
            "open":    _krx_num(out.get("stck_oprc")),
            "high":    _krx_num(out.get("stck_hgpr")),
            "low":     _krx_num(out.get("stck_lwpr")),
            "volume":  int(_krx_num(out.get("acml_vol")) or 0),  # 누적거래량
            "change_rate": _krx_num(out.get("prdy_ctrt")),
        }
    except Exception:
        return None


def fetch_minute_kis_today(code: str, max_bars: int = 120) -> pd.DataFrame:
    """KIS REST `inquire-time-itemchartprice` (FHKST03010200)로 오늘 1분봉 조회.
    페이징해서 최대 max_bars 봉까지 받음 (30봉/호출).
    반환: DataFrame[datetime, open, high, low, close, volume] (KST naive)
    """
    token = kis_get_token()
    appkey, appsecret, base = _kis_resolve()
    if not token or not appkey or not appsecret:
        return pd.DataFrame()
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": appkey.strip(),
        "appsecret": appsecret.strip(),
        "tr_id": "FHKST03010200",
        "Content-Type": "application/json",
    }
    url_base = f"{base}/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
    rows: list = []
    seen_times: set = set()
    cur_hour = _dt.now().strftime("%H%M%S")

    for _ in range(max(1, max_bars // 30 + 1)):
        params = {
            "FID_ETC_CLS_CODE": "",
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": code,
            "FID_INPUT_HOUR_1": cur_hour,
            "FID_PW_DATA_INCU_YN": "Y",
        }
        try:
            qs = _urlparse.urlencode(params)
            req = _urlreq.Request(f"{url_base}?{qs}", headers=headers)
            with _urlreq.urlopen(req, timeout=8) as r:
                data = _json.loads(r.read().decode("utf-8"))
        except Exception:
            break
        out2 = data.get("output2") or []
        if not out2:
            break
        oldest_hhmmss = None
        for row in out2:
            hhmmss = row.get("stck_cntg_hour", "")
            d_str = row.get("stck_bsop_date", _dt.now().strftime("%Y%m%d"))
            if not hhmmss or not d_str:
                continue
            try:
                dt = _dt.strptime(f"{d_str}{hhmmss}", "%Y%m%d%H%M%S")
            except ValueError:
                continue
            key = dt.isoformat()
            if key in seen_times:
                continue
            seen_times.add(key)
            close_v = _krx_num(row.get("stck_prpr"))
            if close_v is None:
                continue
            rows.append({
                "datetime": dt,
                "open":  _krx_num(row.get("stck_oprc")) or close_v,
                "high":  _krx_num(row.get("stck_hgpr")) or close_v,
                "low":   _krx_num(row.get("stck_lwpr")) or close_v,
                "close": close_v,
                "volume": int(_krx_num(row.get("cntg_vol")) or 0),
            })
            oldest_hhmmss = hhmmss
        if len(rows) >= max_bars or not oldest_hhmmss:
            break
        # 다음 페이지: 가장 오래된 봉 시각의 1분 전
        try:
            last_dt = _dt.strptime(oldest_hhmmss, "%H%M%S")
            prev = last_dt - timedelta(minutes=1)
            new_hour = prev.strftime("%H%M%S")
            if new_hour == cur_hour:
                break
            cur_hour = new_hour
        except ValueError:
            break

    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    df = df.drop_duplicates(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
    return df


def kis_program_trade_by_stock_daily(code: str) -> pd.DataFrame:
    """KIS 종목별 프로그램매매추이(일별) — TR_ID FHPPG04650201.
    URL: /uapi/domestic-stock/v1/quotations/program-trade-by-stock-daily
    실전 KIS 키만 동작 (모의 미지원).

    반환 DataFrame: date, arb_buy, arb_sell, arb_net, nonarb_buy, nonarb_sell, nonarb_net, total_net
    (수량 = 주식수, 부호: + 매수 우위, − 매도 우위)
    필드명을 못 찾으면 빈 DataFrame 반환 (KIS 응답 구조가 변경된 경우).
    """
    token = kis_get_token()
    appkey, appsecret, base = _kis_resolve()
    if not token or not appkey or not appsecret:
        return pd.DataFrame()
    # 모의(VTS)는 미지원
    if "openapivts" in base:
        return pd.DataFrame()
    headers = {
        "authorization": f"Bearer {token}",
        "appkey": appkey.strip(),
        "appsecret": appsecret.strip(),
        "tr_id": "FHPPG04650201",
        "Content-Type": "application/json",
    }
    # KIS 프로그램매매 일별 API 는 FID_INPUT_DATE_1 (조회 기준일) 필수.
    # 보통 오늘 날짜 넣으면 그 이전 N일치(약 60일)를 묶어서 응답.
    today_yyyymmdd = _dt.now().strftime("%Y%m%d")
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": code,
        "FID_INPUT_DATE_1": today_yyyymmdd,
    }
    url = f"{base}/uapi/domestic-stock/v1/quotations/program-trade-by-stock-daily?" + _urlparse.urlencode(params)
    try:
        req = _urlreq.Request(url, headers=headers)
        with _urlreq.urlopen(req, timeout=8) as r:
            data = _json.loads(r.read().decode("utf-8"))
    except Exception as e:
        print(f"[KIS 프로그램매매] 호출 실패: {e}")
        return pd.DataFrame()

    # KIS API 응답 구조: output/output1/output2 중 list 인 곳을 찾아 사용
    out = None
    for k in ("output2", "output1", "output"):
        v = data.get(k)
        if isinstance(v, list) and v:
            out = v
            break
    if not out:
        # 권한 부족·키 미발급 시 메시지 흘려보내기
        msg = data.get("msg1") or data.get("msg_cd") or ""
        if msg:
            print(f"[KIS 프로그램매매] 응답 메시지: {msg}")
        return pd.DataFrame()

    # 첫 응답 구조 진단 — 한 번만 출력 (필드명 확인용)
    if not getattr(kis_program_trade_by_stock_daily, "_logged", False):
        print(f"[KIS 프로그램매매] 응답 키 샘플: {list(out[0].keys())[:20]}")
        kis_program_trade_by_stock_daily._logged = True

    # 후보 필드명 (KIS 명세에서 흔히 쓰는 변형 모두 대응)
    def pick(row, *names):
        for n in names:
            if n in row and row[n] not in ("", None):
                return _krx_num(row[n])
        return None

    rows = []
    for row in out:
        d_str = pick(row, "stck_bsop_date", "bsop_date", "stck_bsop_dt") or 0
        d_str = str(int(d_str)) if d_str else ""
        if len(d_str) != 8:
            continue
        try:
            d = _dt.strptime(d_str, "%Y%m%d").date()
        except ValueError:
            continue
        # 차익(smtm) / 비차익(smtn) — 그리고 흔히 쓰이는 다른 약어들도 시도
        arb_buy  = pick(row, "whol_smtm_agrm_qty", "smtm_agrm_qty", "stck_arbt_pchs_qty", "arbt_pchs_qty")
        arb_sell = pick(row, "whol_smtm_seln_qty", "smtm_seln_qty", "stck_arbt_seln_qty", "arbt_seln_qty")
        arb_net  = pick(row, "whol_smtm_ntby_qty", "smtm_ntby_qty", "stck_arbt_ntby_qty", "arbt_ntby_qty")
        nb_buy   = pick(row, "whol_smtn_agrm_qty", "smtn_agrm_qty", "stck_nabt_pchs_qty", "nabt_pchs_qty")
        nb_sell  = pick(row, "whol_smtn_seln_qty", "smtn_seln_qty", "stck_nabt_seln_qty", "nabt_seln_qty")
        nb_net   = pick(row, "whol_smtn_ntby_qty", "smtn_ntby_qty", "stck_nabt_ntby_qty", "nabt_ntby_qty")
        total    = pick(row, "whol_ntby_qty", "stck_prgr_ntby_qty", "prgr_ntby_qty")
        rows.append({
            "date": d,
            "arb_buy":     int(arb_buy or 0),
            "arb_sell":    int(arb_sell or 0),
            "arb_net":     int(arb_net or 0),
            "nonarb_buy":  int(nb_buy or 0),
            "nonarb_sell": int(nb_sell or 0),
            "nonarb_net":  int(nb_net or 0),
            "total_net":   int(total if total is not None else (arb_net or 0) + (nb_net or 0)),
        })
    if not rows:
        return pd.DataFrame()
    return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)


def kis_get_approval_key() -> str | None:
    """KIS WebSocket용 approval_key 발급. REST OAuth와는 별개의 키.
    24시간 유효, 발급 횟수 제한 있어 모듈 캐시.
    """
    appkey, appsecret, base = _kis_resolve()
    if not appkey or not appsecret:
        return None
    cur_env = (os.environ.get("KIS_ENV") or "real").strip().lower()
    cache_key = f"_approval_{cur_env}"
    cache = _kis_token_cache.setdefault(cache_key, {"key": None, "expire": 0.0})
    if cache.get("key") and cache.get("expire", 0) > _time.time():
        return cache["key"]
    try:
        body = _json.dumps({
            "grant_type": "client_credentials",
            "appkey": appkey.strip(),
            "secretkey": appsecret.strip(),
        }).encode("utf-8")
        req = _urlreq.Request(
            f"{base}/oauth2/Approval", data=body, method="POST",
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        with _urlreq.urlopen(req, timeout=10) as r:
            data = _json.loads(r.read().decode("utf-8"))
        ak = data.get("approval_key")
        if not ak:
            return None
        cache["key"] = ak
        cache["expire"] = _time.time() + 23 * 3600
        return ak
    except Exception:
        return None


def kis_diagnose() -> dict:
    """KIS 키/토큰 진단. 환경(모의/실전), 토큰 발급, 현재가 1건 호출 결과 반환."""
    appkey, appsecret, base = _kis_resolve()
    if not appkey or not appsecret:
        return {"status": "no_key", "env": kis_env_label()}
    token = kis_get_token()
    if not token:
        return {"status": "token_fail", "env": kis_env_label(), "base": base,
                "message": "OAuth2 토큰 발급 실패 — 키 오타 또는 환경(모의/실전) 불일치 가능성"}
    sample = kis_current_price("005930")  # 삼성전자로 핑
    if not sample:
        return {"status": "token_ok_price_fail", "env": kis_env_label(), "base": base,
                "message": "토큰은 발급됐으나 현재가 조회 실패 — tr_id 권한 문제 가능"}
    return {
        "status": "ok",
        "env": kis_env_label(),
        "base": base,
        "sample": {"code": "005930", "price": sample["price"], "volume": sample["volume"]},
    }


# --- KIS WebSocket 실시간 시세 (체결가 H0STCNT0 push) ----------------------
KIS_WS_REAL = "ws://ops.koreainvestment.com:21000"
KIS_WS_MOCK = "ws://ops.koreainvestment.com:31000"


class KisRealtimeWorker(QObject):
    """KIS WebSocket H0STCNT0 (실시간 주식 체결가) 구독.
    별도 스레드에서 ws 유지, 체결 push마다 Qt signal로 GUI에 전달."""

    tick = Signal(dict)        # {"code", "time", "price", "change_rate", "cum_volume", "tick_volume"}
    status = Signal(str)       # 연결 상태 메시지

    def __init__(self) -> None:
        super().__init__()
        self._thread: _threading.Thread | None = None
        self._ws = None
        self._stop = _threading.Event()
        self._current_code: str | None = None
        self._lock = _threading.Lock()

    def start(self, code: str) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                # 기존 스레드에 종목 변경만 전달 (재구독)
                self._switch_code(code)
                return
            self._current_code = code
            self._stop.clear()
            self._thread = _threading.Thread(target=self._run, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass
        self._ws = None

    def _switch_code(self, new_code: str) -> None:
        old = self._current_code
        if old == new_code:
            return
        try:
            if self._ws and old:
                self._ws.send(self._build_msg(old, register=False))
            if self._ws and new_code:
                self._ws.send(self._build_msg(new_code, register=True))
        except Exception:
            pass
        self._current_code = new_code

    def _build_msg(self, code: str, register: bool = True) -> str:
        appkey = kis_get_approval_key() or ""
        return _json.dumps({
            "header": {
                "approval_key": appkey,
                "custtype": "P",
                "tr_type": "1" if register else "2",
                "content-type": "utf-8",
            },
            "body": {"input": {"tr_id": "H0STCNT0", "tr_key": code}},
        })

    def _run(self) -> None:
        try:
            from websockets.sync.client import connect as ws_connect
        except ImportError:
            self.status.emit("websockets 라이브러리 없음")
            return
        approval = kis_get_approval_key()
        if not approval:
            self.status.emit("approval_key 발급 실패")
            return
        env = (os.environ.get("KIS_ENV") or "real").strip().lower()
        url = KIS_WS_MOCK if env == "mock" else KIS_WS_REAL
        try:
            with ws_connect(url, open_timeout=10, close_timeout=5) as ws:
                self._ws = ws
                self.status.emit(f"WS 연결 ({env})")
                if self._current_code:
                    ws.send(self._build_msg(self._current_code, register=True))
                while not self._stop.is_set():
                    try:
                        raw = ws.recv(timeout=2)
                    except TimeoutError:
                        continue
                    except Exception:
                        break
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", errors="replace")
                    self._parse(raw)
        except Exception as e:
            self.status.emit(f"WS 에러: {type(e).__name__}")
        finally:
            self._ws = None

    def _parse(self, raw: str) -> None:
        """KIS 실시간 응답 파싱.
        체결가 형식: '0|H0STCNT0|001|005930^120000^77000^5^...'
        제어 메시지: JSON ({header, body})
        """
        if not raw:
            return
        if raw[:1] in ("0", "1") and "|" in raw:
            parts = raw.split("|", 3)
            if len(parts) < 4:
                return
            tr_id = parts[1]
            if tr_id != "H0STCNT0":
                return
            data_block = parts[3]
            # 여러 체결이 한 패킷에 올 수 있음 (개수: parts[2])
            try:
                count = int(parts[2])
            except ValueError:
                count = 1
            fields_per = data_block.count("^") // max(count, 1) + 1
            chunks = data_block.split("^")
            for i in range(count):
                f = chunks[i * fields_per:(i + 1) * fields_per]
                if len(f) < 14:
                    continue
                try:
                    snap = {
                        "code": f[0],
                        "time": f[1],
                        "price": float(f[2]),
                        "change_rate": float(f[5]) if f[5] else 0.0,
                        "open": float(f[7]) if f[7] else None,
                        "high": float(f[8]) if f[8] else None,
                        "low": float(f[9]) if f[9] else None,
                        "tick_volume": int(f[12]) if f[12] else 0,
                        "cum_volume": int(f[13]) if f[13] else 0,
                    }
                    self.tick.emit(snap)
                except (ValueError, IndexError):
                    continue
        else:
            # JSON 제어 메시지 (구독 확인 등) — 무시
            pass


# --- Yahoo Finance 분봉 (진짜 OHLC, 60일치, 무료, 인증 불필요) ------------
def fetch_quarterly_revenue_yahoo(code: str) -> pd.DataFrame:
    """Yahoo Finance에서 분기별 매출액·영업이익 조회. .KS / .KQ 자동 시도.
    반환: DataFrame[date, revenue, operating_income] (단위: 원)
    """
    try:
        import yfinance as yf
    except ImportError:
        return pd.DataFrame()
    for suffix in (".KS", ".KQ"):
        try:
            t = yf.Ticker(code + suffix)
            qf = t.quarterly_financials
            if qf is None or qf.empty:
                continue
            # 컬럼은 분기말 날짜, 행은 항목명
            rows = []
            for q_date in qf.columns:
                rev = None
                op = None
                for key in ("Total Revenue", "TotalRevenue", "Revenue"):
                    if key in qf.index:
                        rev = qf.loc[key, q_date]
                        break
                for key in ("Operating Income", "OperatingIncome",
                            "Operating Revenue", "EBIT"):
                    if key in qf.index:
                        op = qf.loc[key, q_date]
                        break
                rows.append({
                    "date": pd.to_datetime(q_date),
                    "revenue": float(rev) if pd.notna(rev) else None,
                    "operating_income": float(op) if pd.notna(op) else None,
                })
            df = pd.DataFrame(rows).dropna(subset=["revenue"], how="all")
            if not df.empty:
                return df.sort_values("date").reset_index(drop=True)
        except Exception:
            continue
    return pd.DataFrame()


def fetch_minute_yahoo(code: str, interval: str = "5m", days: int = 60) -> pd.DataFrame:
    """Yahoo Finance에서 한국주식 분봉 OHLCV.
    - interval: '1m'(7일) / '5m'(60일) / '15m'(60일) 등
    - 코드+.KS(KOSPI) 시도 후 안 되면 .KQ(KOSDAQ) 시도
    - 시간대를 Asia/Seoul (naive)로 변환
    """
    try:
        import yfinance as yf
    except ImportError:
        return pd.DataFrame()
    period = f"{days}d"
    for suffix in (".KS", ".KQ"):
        try:
            df = yf.download(
                code + suffix, period=period, interval=interval,
                progress=False, auto_adjust=False, threads=False,
            )
        except Exception:
            continue
        if df is None or df.empty:
            continue
        if hasattr(df.columns, "levels"):
            df.columns = [c[0] for c in df.columns]
        # KST naive로 정규화
        # - tz-aware: KST로 변환 후 naive 만들기
        # - naive: 한국 종목(.KS/.KQ) yfinance는 이미 현지(KST) 시각으로 반환하므로 그대로 둠
        #   (이전엔 UTC로 가정해 +9 했더니 11:00 → 20:00으로 잘못 찍히는 버그 발생)
        try:
            if df.index.tz is not None:
                df.index = df.index.tz_convert("Asia/Seoul").tz_localize(None)
        except (TypeError, AttributeError):
            pass
        df = df.reset_index()
        # yfinance 분봉은 'Datetime' 컬럼, 일봉은 'Date'
        time_col = next((c for c in df.columns if c.lower() in ("datetime", "date")), None)
        if not time_col:
            continue
        df = df.rename(columns={
            time_col: "datetime",
            "Open": "open", "High": "high", "Low": "low",
            "Close": "close", "Volume": "volume",
        })
        df = df[["datetime", "open", "high", "low", "close", "volume"]].copy()
        df["volume"] = df["volume"].fillna(0).astype("int64")
        df = df.dropna(subset=["close"]).sort_values("datetime").reset_index(drop=True)
        if not df.empty:
            return df
    return pd.DataFrame()


def resample_to_5min(df_1min: pd.DataFrame,
                     cumulative_volume: bool = False) -> pd.DataFrame:
    """1분봉 → 5분봉. label/closed='left'로 라벨링 (한국 HTS 관례).

    - 5분 윈도우 [13:30, 13:35) 의 데이터는 '13:30' 봉으로 라벨링 (시작점 기준)
    - cumulative_volume=True: 입력 volume이 분 누적 (Naver) → diff로 분당 거래량 추출
    - cumulative_volume=False: 입력 volume이 이미 분당 (KIS, yfinance) → 그대로 sum
    """
    if df_1min.empty:
        return df_1min
    d = df_1min.copy()
    d["datetime"] = pd.to_datetime(d["datetime"])
    d = d.set_index("datetime").sort_index()
    if cumulative_volume:
        d["_vol"] = d["volume"].diff().fillna(d["volume"]).clip(lower=0)
    else:
        d["_vol"] = d["volume"]
    agg = d.resample("5min", label="left", closed="left").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "_vol": "sum",
    }).dropna(subset=["close"])
    agg = agg.rename(columns={"_vol": "volume"})
    return agg.reset_index()


def resample_to_weekly(df_daily: pd.DataFrame) -> pd.DataFrame:
    """일봉 → 주봉 (월~금 기준 W-FRI)."""
    if df_daily.empty:
        return df_daily
    d = df_daily.copy()
    d["date"] = pd.to_datetime(d["date"])
    d = d.set_index("date").sort_index()
    agg = d.resample("W-FRI").agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna(subset=["close"])
    return agg.reset_index()


# --- Naver 일별 외국인/기관 매매 (수급 탭용) ------------------------------
_NAVER_FRGN_URL = "https://finance.naver.com/item/frgn.naver?code={code}&page={page}"


def fetch_naver_investor_flow(code: str, pages: int = 6) -> pd.DataFrame:
    """Naver frgn.naver에서 외국인/기관 일별 순매수(주) 스크래핑.
    페이지당 약 20행, pages=6이면 약 120 거래일(~6개월). EUC-KR 인코딩."""
    con = duckdb.connect(str(DB_PATH))
    try:
        end = _dt.now().date()
        start = end - timedelta(days=pages * 30)
        cached = con.execute(
            """
            SELECT date, close, volume, foreign_net, institution_net,
                   foreign_qty, foreign_rate
              FROM naver_investor_flow
             WHERE code = ? AND date BETWEEN ? AND ?
             ORDER BY date
            """,
            [code, start, end],
        ).df()
        if not cached.empty:
            latest = pd.to_datetime(cached["date"].max()).date()
            # 직전 영업일까지 데이터가 들어있을 때만 캐시 재사용.
            # (5일 캐시는 주말/연휴 직후 빈데이터 그대로 반환되는 문제 → 평일에 4/29 마지막 같은 현상)
            today = pd.Timestamp.now().normalize()
            last_bday = (today - pd.tseries.offsets.BDay(1)).date() if today.weekday() < 5 else today.date()
            # 평일 16시 이후엔 오늘 자체가 last_bday 후보
            if pd.Timestamp.now().hour >= 16 and today.weekday() < 5:
                last_bday = today.date()
            if latest >= last_bday:
                return cached.reset_index(drop=True)

        rows = []
        seen_dates = set()
        for page in range(1, pages + 1):
            try:
                html = _http_get(_NAVER_FRGN_URL.format(code=code, page=page), encoding="euc-kr")
            except Exception:
                continue
            m = _re.search(r"외국인[^<]{0,20}기관[\s\S]{0,80000}?</table>", html)
            if not m:
                continue
            for tr in _re.findall(r"<tr[^>]*>([\s\S]*?)</tr>", m.group()):
                cells = _re.findall(r"<td[^>]*>([\s\S]*?)</td>", tr)
                if not cells:
                    continue
                vals = [
                    _re.sub(r"\s+", " ", _re.sub(r"<[^>]+>", " ", c)).strip()
                    for c in cells
                ]
                if not vals or not _re.match(r"\d{4}\.\d{2}\.\d{2}", vals[0] or ""):
                    continue
                if len(vals) < 9:
                    continue
                if vals[0] in seen_dates:
                    continue
                seen_dates.add(vals[0])
                try:
                    d = _dt.strptime(vals[0], "%Y.%m.%d").date()
                    rows.append({
                        "code": code,
                        "date": d,
                        "close": float(vals[1].replace(",", "")),
                        "volume": int(vals[4].replace(",", "")),
                        "foreign_net": int(vals[5].replace(",", "").replace("+", "")),
                        "institution_net": int(vals[6].replace(",", "").replace("+", "")),
                        "foreign_qty": int(vals[7].replace(",", "")),
                        "foreign_rate": float(vals[8].replace("%", "").strip()),
                    })
                except (ValueError, IndexError):
                    continue

        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
        con.execute(
            "INSERT OR REPLACE INTO naver_investor_flow "
            "(code, date, close, volume, foreign_net, institution_net, foreign_qty, foreign_rate) "
            "SELECT code, date, close, volume, foreign_net, institution_net, foreign_qty, foreign_rate FROM df"
        )
        return df.drop(columns=["code"]).reset_index(drop=True)
    finally:
        con.close()


# --- 종목명 조회 -----------------------------------------------------------
def get_name(code: str) -> str:
    try:
        with _silence_stderr():
            n = stock.get_market_ticker_name(code)
        if n:
            return n
    except Exception:
        pass
    # KRX 인증 막혔으면 Naver 스냅샷 사용
    try:
        snap = fetch_naver_snapshot(code)
        return snap.get("name") or code
    except Exception:
        return code


# --- 종목 자동완성 (코드 ↔ 이름 매핑) -----------------------------------
def seed_ticker_master() -> None:
    """앱 시작 시 한 번 호출 — 시드 리스트와 기존 cache 의 이름을 ticker_master에 머지."""
    con = duckdb.connect(str(DB_PATH))
    try:
        # 1) 하드코딩 시드 (즉시 자동완성 가능)
        seed_df = pd.DataFrame(TICKER_SEED, columns=["code", "name"])
        seed_df["market"] = ""
        seed_df["updated_at"] = pd.Timestamp.now()
        con.execute(
            "INSERT OR IGNORE INTO ticker_master "
            "(code, name, market, updated_at) "
            "SELECT code, name, market, updated_at FROM seed_df"
        )
        # 2) naver_snapshot 의 이름들도 끌어와 cache 보강
        try:
            con.execute(
                "INSERT OR IGNORE INTO ticker_master (code, name, market, updated_at) "
                "SELECT code, name, '', fetched_at FROM naver_snapshot WHERE name IS NOT NULL"
            )
        except Exception:
            pass
    finally:
        con.close()


def upsert_ticker(code: str, name: str) -> None:
    """load_all 등에서 종목 조회 성공 시 호출 — ticker_master 갱신."""
    if not code or not name or len(code) != 6:
        return
    con = duckdb.connect(str(DB_PATH))
    try:
        con.execute(
            "INSERT OR REPLACE INTO ticker_master (code, name, market, updated_at) "
            "VALUES (?, ?, '', ?)",
            [code, name, _dt.now()],
        )
    finally:
        con.close()


def load_all_tickers() -> list[tuple[str, str]]:
    """ticker_master 전체 (code, name) 반환. 자동완성 model 채우기용."""
    con = duckdb.connect(str(DB_PATH))
    try:
        rows = con.execute(
            "SELECT code, name FROM ticker_master WHERE name IS NOT NULL ORDER BY name"
        ).fetchall()
        return [(r[0], r[1]) for r in rows]
    finally:
        con.close()


def resolve_ticker(query: str) -> str | None:
    """쿼리(코드 또는 이름)를 6자리 코드로 변환.
    - 6자리 숫자: 그대로 반환
    - "이름 (코드)" 형식: 괄호 안 코드 추출
    - 텍스트: ticker_master 정확 → 접두 → 부분 일치 순서
    - 매치 없음: None
    """
    q = (query or "").strip()
    if not q:
        return None
    if q.isdigit() and len(q) == 6:
        return q
    # "한화비전 (489790)" 같은 completer 출력 형식에서 코드 추출
    m = _re.search(r"\((\d{6})\)", q)
    if m:
        return m.group(1)
    # 괄호 / 추가 정보 떼고 이름만 추출
    name_only = _re.sub(r"\s*\([^)]*\)\s*", "", q).strip()
    if not name_only:
        return None
    con = duckdb.connect(str(DB_PATH))
    try:
        r = con.execute("SELECT code FROM ticker_master WHERE name = ?", [name_only]).fetchone()
        if r:
            return r[0]
        r = con.execute(
            "SELECT code FROM ticker_master WHERE name LIKE ? ORDER BY length(name) LIMIT 1",
            [name_only + "%"],
        ).fetchone()
        if r:
            return r[0]
        r = con.execute(
            "SELECT code FROM ticker_master WHERE name LIKE ? ORDER BY length(name) LIMIT 1",
            ["%" + name_only + "%"],
        ).fetchone()
        if r:
            return r[0]
        return None
    finally:
        con.close()


# --- 일목균형표 (Ichimoku Kinko Hyo) -------------------------------------
def compute_ichimoku(df: pd.DataFrame, conv: int = 9, base: int = 26,
                     span_b_period: int = 52, shift: int = 26) -> dict:
    """일목균형표 5선 계산. df는 datetime-index 가진 OHLC dataframe.

    반환 dict (모두 pandas Series, 선행스팬·후행스팬은 미래 shift봉 추가된 확장 인덱스):
      tenkan   : 전환선 (conv봉 평균 (max+min)/2)
      kijun    : 기준선 (base봉 평균)
      senkou_a : 선행스팬1 = (전환+기준)/2, 26봉 앞으로 plot
      senkou_b : 선행스팬2 = span_b_period봉 평균, 26봉 앞으로 plot
      chikou   : 후행스팬 = 종가, 26봉 뒤로 plot
    """
    if df.empty or len(df) < span_b_period:
        empty = pd.Series(dtype=float)
        return {"tenkan": empty, "kijun": empty,
                "senkou_a": empty, "senkou_b": empty, "chikou": empty}
    high = df["high"]
    low = df["low"]
    close = df["close"]
    tenkan = (high.rolling(conv).max() + low.rolling(conv).min()) / 2
    kijun = (high.rolling(base).max() + low.rolling(base).min()) / 2
    span_a_calc = (tenkan + kijun) / 2
    span_b_calc = (high.rolling(span_b_period).max() + low.rolling(span_b_period).min()) / 2

    # 미래 shift봉 인덱스 생성: HTS와 같이 거래일(영업일) 기준으로 26봉 forward
    if len(df.index) >= 2:
        diffs = pd.Series(df.index[1:] - df.index[:-1])
        delta = diffs.mode().iloc[0] if not diffs.empty else pd.Timedelta(days=1)
    else:
        delta = pd.Timedelta(days=1)
    last = df.index[-1]
    if delta == pd.Timedelta(days=1) or delta == pd.Timedelta(days=2) or delta == pd.Timedelta(days=3):
        # 일봉 (영업일 갭 1~3일 혼재). 영업일 기준으로 forward (주말 skip)
        future = pd.bdate_range(start=last + pd.tseries.offsets.BDay(1),
                                periods=shift, freq="B")
    else:
        # 5분봉/주봉/월봉: 모드 delta 그대로 사용 (장 시간 외 skip은 사소함)
        future = pd.DatetimeIndex([last + delta * (i + 1) for i in range(shift)])
    ext_idx = df.index.append(future)

    # 선행스팬: 오늘 계산값을 오늘+26봉 자리에 plot
    senkou_a = pd.Series(index=ext_idx, dtype=float)
    senkou_a.iloc[shift:shift + len(span_a_calc)] = span_a_calc.values
    senkou_b = pd.Series(index=ext_idx, dtype=float)
    senkou_b.iloc[shift:shift + len(span_b_calc)] = span_b_calc.values

    # 후행스팬: 오늘 종가를 오늘-26봉 자리에 plot
    chikou = pd.Series(index=ext_idx, dtype=float)
    if len(close) > shift:
        chikou.iloc[:len(close) - shift] = close.iloc[shift:].values

    return {"tenkan": tenkan, "kijun": kijun,
            "senkou_a": senkou_a, "senkou_b": senkou_b, "chikou": chikou}


# --- RSI / MACD ----------------------------------------------------------
def compute_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI. close: 가격 시리즈. 0~100 값.
    >70 과매수, <30 과매도, 50 중립. 다이버전스 판독용."""
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    # Wilder smoothing = EMA with alpha=1/period
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    rsi = 100 - (100 / (1 + rs))
    return rsi


def compute_macd(close: pd.Series, fast: int = 12, slow: int = 26,
                 signal: int = 9) -> dict:
    """MACD(12/26/9). 반환 dict: macd, signal, histogram (모두 pandas Series)."""
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    macd = ema_fast - ema_slow
    signal_line = macd.ewm(span=signal, adjust=False).mean()
    hist = macd - signal_line
    return {"macd": macd, "signal": signal_line, "histogram": hist}


# --- GUI 헬퍼 -------------------------------------------------------------
def _as_list(axes_obj) -> list:
    """finplot.create_plot_widget 반환값이 단일/튜플/리스트 다 다를 수 있어 정규화."""
    if isinstance(axes_obj, (list, tuple)):
        return list(axes_obj)
    return [axes_obj]


def _wrap_ax(ax) -> QWidget:
    """finplot 차트 위젯을 PySide6 QWidget 컨테이너로 감싸 addTab/addWidget이 받게 함.
    PyQt5/6 바인딩 객체가 PySide6 시그니처에 직접 안 맞을 때 우회.
    """
    container = QWidget()
    layout = QVBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    inner = ax.ax_widget
    # setParent로 Qt 객체를 컨테이너에 귀속 (sip/shiboken 호환)
    try:
        inner.setParent(container)
    except Exception:
        pass
    layout.addWidget(inner)
    return container


# --- GUI ------------------------------------------------------------------
class DataChartWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("DataChart v2 — 종합 데이터 차트")
        self.resize(1360, 820)

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        # 상단 컨트롤 바 — 스텔스 모드에서 숨길 위젯들은 self._hideables에 모음
        bar = QHBoxLayout()
        self._bar_layout = bar
        self._root_layout = root
        self._hideables: list[QWidget] = []
        self._lbl_code = QLabel("종목코드")
        bar.addWidget(self._lbl_code)
        self._hideables.append(self._lbl_code)
        self.code_input = QLineEdit(DEFAULT_CODE)
        self.code_input.setMaximumWidth(160)  # 한글 종목명 표시용으로 조금 더 넓게
        self.code_input.setPlaceholderText("종목명 또는 6자리 코드")
        self.code_input.returnPressed.connect(self.load_all)
        # 클릭했을 때만 포커스 획득 (Tab 순환·자동 포커스에 잡히지 않게)
        self.code_input.setFocusPolicy(Qt.ClickFocus)
        bar.addWidget(self.code_input)
        # 자동완성 — ticker_master에서 종목명 검색
        from PySide6.QtWidgets import QCompleter
        from PySide6.QtCore import QStringListModel
        seed_ticker_master()  # 초기 시드 + 기존 cache 머지
        self._completer_model = QStringListModel()
        self._completer = QCompleter(self._completer_model, self)
        self._completer.setCaseSensitivity(Qt.CaseInsensitive)
        self._completer.setFilterMode(Qt.MatchContains)
        self._completer.setMaxVisibleItems(10)
        # 사용자 선택 시: "한화비전 (489790)" → 코드만 추출 후 즉시 load
        def _on_completer_activated(text: str) -> None:
            import re as _re_local
            m = _re_local.search(r"\((\d{6})\)", text)
            if m:
                self.code_input.setText(m.group(1))
                self.load_all()
        self._completer.activated[str].connect(_on_completer_activated)
        self.code_input.setCompleter(self._completer)
        self._refresh_completer_model()

        self._load_btn = QPushButton("불러오기")
        self._load_btn.clicked.connect(self.load_all)
        bar.addWidget(self._load_btn)
        self._hideables.append(self._load_btn)

        # 종목명 표시
        self.name_label = QLabel("-")
        self.name_label.setStyleSheet("font-weight: bold; padding: 0 12px; color: #2266cc;")
        bar.addWidget(self.name_label)

        # 큰 현재가 라벨 (상승=빨강, 하락=파랑)
        self.price_label = QLabel("-")
        self.price_label.setStyleSheet(
            "font-size: 18px; font-weight: bold; padding: 2px 10px; "
            "background-color: #f5f5f5; border-radius: 4px;"
        )
        bar.addWidget(self.price_label)

        self._lbl_tf = QLabel("주기")
        bar.addWidget(self._lbl_tf)
        self._hideables.append(self._lbl_tf)
        self.tf_combo = QComboBox()
        self.tf_combo.addItem("일봉", "day")
        self.tf_combo.addItem("주봉", "week")
        self.tf_combo.addItem("5분봉", "min5")
        self.tf_combo.currentIndexChanged.connect(self._on_timeframe_changed)
        bar.addWidget(self.tf_combo)
        self._hideables.append(self.tf_combo)

        # 일목균형표 토글
        self.cb_ichimoku = QCheckBox("일목균형표")
        self.cb_ichimoku.setToolTip("전환선(9)·기준선(26)·선행스팬1·2(26봉 forward)·후행스팬(26봉 backward)")
        self.cb_ichimoku.stateChanged.connect(self._on_ichimoku_toggled)
        bar.addWidget(self.cb_ichimoku)
        self._hideables.append(self.cb_ichimoku)

        # RSI 토글 (기본 ON)
        self.cb_rsi = QCheckBox("RSI")
        self.cb_rsi.setToolTip("RSI(14) — Wilder smoothing. 70 과매수 / 30 과매도 / 50 중립")
        self.cb_rsi.setChecked(True)
        self.cb_rsi.stateChanged.connect(self._on_rsi_toggled)
        bar.addWidget(self.cb_rsi)
        self._hideables.append(self.cb_rsi)

        # MACD 토글 (기본 ON)
        self.cb_macd = QCheckBox("MACD")
        self.cb_macd.setToolTip("MACD(12/26/9) — EMA12 − EMA26, signal=EMA9, histogram=차이")
        self.cb_macd.setChecked(True)
        self.cb_macd.stateChanged.connect(self._on_macd_toggled)
        bar.addWidget(self.cb_macd)
        self._hideables.append(self.cb_macd)

        # 지수 비교 (KRX OpenAPI 승인된 서비스 사용)
        self._lbl_cmp = QLabel("비교")
        bar.addWidget(self._lbl_cmp)
        self._hideables.append(self._lbl_cmp)
        self.cmp_combo = QComboBox()
        self.cmp_combo.addItem("없음", None)
        self.cmp_combo.addItem("코스피", "코스피")
        self.cmp_combo.addItem("코스피 200", "코스피 200")
        self.cmp_combo.addItem("KRX 300", "KRX 300")
        self.cmp_combo.addItem("KRX 100", "KRX 100")
        self.cmp_combo.addItem("코스닥", "코스닥")
        self.cmp_combo.addItem("코스닥 150", "코스닥 150")
        self.cmp_combo.setToolTip("종목 가격과 지수를 시작일 기준 동일점으로 정규화해 같은 축에 오버레이")
        self.cmp_combo.currentIndexChanged.connect(self._on_timeframe_changed)
        bar.addWidget(self.cmp_combo)
        self._hideables.append(self.cmp_combo)

        self.status = QLabel("준비")
        bar.addWidget(self.status)
        self._hideables.append(self.status)
        bar.addStretch()

        # 스텔스 모드 토글 (회사에서 몰래 보기용)
        self.btn_stealth = QPushButton("📕")
        self.btn_stealth.setCheckable(True)
        self.btn_stealth.setToolTip("스텔스 모드: 종목명+가격만 흑색 작게 (단축키: A)")
        self.btn_stealth.setMaximumWidth(32)
        self.btn_stealth.clicked.connect(self._toggle_stealth)
        bar.addWidget(self.btn_stealth)

        # 화면 캡쳐 버튼 (단축키 C)
        self.btn_capture = QPushButton("📋")
        self.btn_capture.setToolTip("화면 캡쳐 → 클립보드 (Ctrl+V로 AI에 붙여넣기, 단축키: C)")
        self.btn_capture.setMaximumWidth(32)
        self.btn_capture.clicked.connect(self._capture_to_clipboard)
        bar.addWidget(self.btn_capture)
        self._hideables.append(self.btn_capture)
        # 단축키 A — 입력칸 포커스면 무시. QShortcut 대신 eventFilter로 처리
        # (QShortcut은 QLineEdit에서 키를 가로채는 문제가 있음)
        QApplication.instance().installEventFilter(self)

        root.addLayout(bar)
        self._stealth_on = False
        self._normal_geometry = None

        # 탭 구성
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, stretch=1)

        # 탭 1: 캔들차트 — 가격/거래량/RSI/MACD를 별개 plot widget으로 분리, QSplitter로 쌓고 X축 연동
        price_w = fplt.create_plot_widget(master=self, rows=1, init_zoom_periods=120)
        vol_w = fplt.create_plot_widget(master=self, rows=1, init_zoom_periods=120)
        rsi_w = fplt.create_plot_widget(master=self, rows=1, init_zoom_periods=120)
        macd_w = fplt.create_plot_widget(master=self, rows=1, init_zoom_periods=120)
        self.price_ax = price_w[0] if isinstance(price_w, (list, tuple)) else price_w
        self.vol_ax = vol_w[0] if isinstance(vol_w, (list, tuple)) else vol_w
        self.rsi_ax = rsi_w[0] if isinstance(rsi_w, (list, tuple)) else rsi_w
        self.macd_ax = macd_w[0] if isinstance(macd_w, (list, tuple)) else macd_w
        # X축 동기화
        self.vol_ax.setXLink(self.price_ax)
        self.rsi_ax.setXLink(self.price_ax)
        self.macd_ax.setXLink(self.price_ax)
        self._format_volume_y_axis(self.vol_ax)
        # 좌상단에 "최대 거래량" 오버레이
        import pyqtgraph as _pg
        self._vol_legend = _pg.TextItem(anchor=(0, 0), color="#666")
        self._vol_legend.setParentItem(self.vol_ax.vb)
        self._vol_legend.setPos(8, 8)
        # RSI 오버레이 (현재값 표시)
        self._rsi_legend = _pg.TextItem(anchor=(0, 0), color="#666")
        self._rsi_legend.setParentItem(self.rsi_ax.vb)
        self._rsi_legend.setPos(8, 8)
        # MACD 오버레이
        self._macd_legend = _pg.TextItem(anchor=(0, 0), color="#666")
        self._macd_legend.setParentItem(self.macd_ax.vb)
        self._macd_legend.setPos(8, 8)
        self.axs_price = [self.price_ax, self.vol_ax, self.rsi_ax, self.macd_ax]
        chart_split = QSplitter(Qt.Vertical)
        chart_split.setChildrenCollapsible(False)
        chart_split.addWidget(self.price_ax.ax_widget)
        chart_split.addWidget(self.vol_ax.ax_widget)
        chart_split.addWidget(self.rsi_ax.ax_widget)
        chart_split.addWidget(self.macd_ax.ax_widget)
        chart_split.setStretchFactor(0, 3)
        chart_split.setStretchFactor(1, 1)
        chart_split.setStretchFactor(2, 1)
        chart_split.setStretchFactor(3, 1)
        chart_split.setSizes([500, 150, 150, 150])  # RSI/MACD 기본 visible
        # RSI/MACD 패널 widget 핸들 보관 (토글 시 show/hide)
        self._rsi_panel = self.rsi_ax.ax_widget
        self._macd_panel = self.macd_ax.ax_widget
        self._chart_split = chart_split
        self.tabs.addTab(chart_split, "차트")

        # 탭 2: 수급 — 상단 누적 순매수 차트 + 하단 일별 테이블 (MTS 스타일)
        flow_axes = _as_list(fplt.create_plot_widget(
            master=self, rows=1, init_zoom_periods=120
        ))
        self.flow_ax = flow_axes[0]
        self.axs_flow = [self.flow_ax]
        self._format_y_axis_kmb(self.flow_ax, label_text="누적 순매수 (주)")
        # 일별 테이블
        self.flow_table = QTableWidget()
        self.flow_table.setColumnCount(10)
        self.flow_table.setHorizontalHeaderLabels([
            "날짜", "종가", "등락(%)", "거래량",
            "외국인", "기관", "개인", "외국인 지분율(%)",
            "프로그램 차익(주)", "프로그램 비차익(주)",
        ])
        self.flow_table.verticalHeader().setVisible(False)
        self.flow_table.setAlternatingRowColors(True)
        self.flow_table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.flow_table.setSelectionBehavior(QTableWidget.SelectRows)
        hh = self.flow_table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeToContents)
        hh.setStretchLastSection(True)
        # 차트 + 테이블 분할
        flow_split = QSplitter(Qt.Vertical)
        flow_split.addWidget(self.flow_ax.ax_widget)
        flow_split.addWidget(self.flow_table)
        flow_split.setStretchFactor(0, 1)
        flow_split.setStretchFactor(1, 2)  # 테이블이 더 넓게 (MTS 처럼 표 위주)
        flow_split.setSizes([250, 500])
        self.tabs.addTab(flow_split, "수급")

        # 탭 3: 펀더멘털 (요약 라벨 + PER/PBR 추이 차트)
        fund_widget = QWidget()
        fund_layout = QVBoxLayout(fund_widget)
        self.fund_form = QFormLayout()
        self.lbl_name = QLabel("-")
        self.lbl_close = QLabel("-")
        self.lbl_cap = QLabel("-")
        self.lbl_per = QLabel("-")
        self.lbl_pbr = QLabel("-")
        self.lbl_eps = QLabel("-")
        self.lbl_bps = QLabel("-")
        self.lbl_div = QLabel("-")
        self.lbl_foreign = QLabel("-")
        self.fund_form.addRow("종목명", self.lbl_name)
        self.fund_form.addRow("최근 종가", self.lbl_close)
        self.fund_form.addRow("시가총액", self.lbl_cap)
        self.fund_form.addRow("PER", self.lbl_per)
        self.fund_form.addRow("PBR", self.lbl_pbr)
        self.fund_form.addRow("EPS", self.lbl_eps)
        self.fund_form.addRow("BPS", self.lbl_bps)
        self.fund_form.addRow("배당수익률(%)", self.lbl_div)
        self.fund_form.addRow("외국인 지분율(%)", self.lbl_foreign)
        fund_layout.addLayout(self.fund_form)

        # 펀더멘털 탭: 3개 plot widget을 QSplitter로 쌓고 X축 연동
        per_w = fplt.create_plot_widget(master=self, rows=1, init_zoom_periods=120)
        fr_w = fplt.create_plot_widget(master=self, rows=1, init_zoom_periods=120)
        rev_w = fplt.create_plot_widget(master=self, rows=1, init_zoom_periods=120)
        self.fund_ax = per_w[0] if isinstance(per_w, (list, tuple)) else per_w
        self.fund_ax2 = fr_w[0] if isinstance(fr_w, (list, tuple)) else fr_w
        self.fund_revenue_ax = rev_w[0] if isinstance(rev_w, (list, tuple)) else rev_w
        self.fund_ax2.setXLink(self.fund_ax)
        self.fund_revenue_ax.setXLink(self.fund_ax)
        self._format_y_axis_kmb(self.fund_revenue_ax, label_text="(억원)")
        self.axs_fund = [self.fund_ax, self.fund_ax2, self.fund_revenue_ax]
        fund_split = QSplitter(Qt.Vertical)
        fund_split.setChildrenCollapsible(False)
        fund_split.addWidget(self.fund_ax.ax_widget)
        fund_split.addWidget(self.fund_ax2.ax_widget)
        fund_split.addWidget(self.fund_revenue_ax.ax_widget)
        fund_split.setSizes([200, 200, 200])
        fund_layout.addWidget(fund_split, stretch=1)
        self.tabs.addTab(fund_widget, "펀더멘털")

        # finplot.refresh()는 master.axs에서 모든 axes를 찾아 다시 그림
        self.axs = self.axs_price + self.axs_flow + self.axs_fund

        # KRX OpenAPI 키 상태 한 번 진단해 사용자에게 알림
        self._krx_status = self._check_krx_api_once()

        # 실시간 라이브 봉 (KIS 키 있을 때 5분봉 모드에서 활성화)
        self._rt_timer = QTimer(self)
        self._rt_timer.setInterval(1000)  # 1초 간격
        self._rt_timer.timeout.connect(self._poll_live_tick)
        self._live_bar = None      # dict | None - 진행 중인 5분봉 누적 상태
        self._min5_history = None  # 확정된 과거 5분봉 (yfinance 결과)
        self._last_kis_price = None

        # 가격 라벨 전용 폴링 (5분봉 아닌 모드에서도 가격 라벨만 실시간 갱신)
        self._price_label_timer = QTimer(self)
        self._price_label_timer.setInterval(2000)  # 2초 간격 (가격만 표시)
        self._price_label_timer.timeout.connect(self._poll_price_label_only)

        # KIS WebSocket 실시간 체결가 (틱 단위 push)
        self._kis_ws = KisRealtimeWorker()
        self._kis_ws.tick.connect(self._on_realtime_tick)
        self._kis_ws.status.connect(lambda s: print(f"[KIS WS] {s}"))
        # 렌더 throttle 상태
        self._last_chart_refresh = 0.0   # 마지막 차트 refresh 시각
        self._last_label_color = None    # setStyleSheet 비용 회피용

        # 첫 로드
        self.load_all()
        # 시작 시 메인 윈도우에 포커스 — code_input이 자동 포커스 못 갖게.
        # 그래야 앱 켜자마자 A 단축키가 먹힘.
        try:
            self.setFocus()
            self.code_input.clearFocus()
        except Exception:
            pass
        # 기본값 = 스텔스 모드 ON (회사용). 데이터 로드 완료 직후 한 번 토글.
        # show()가 끝난 뒤 geometry 가 잡히도록 짧게 큐잉.
        QTimer.singleShot(0, self._enter_stealth_on_start)

    def _enter_stealth_on_start(self) -> None:
        try:
            if not self.btn_stealth.isChecked():
                self.btn_stealth.setChecked(True)
                self._toggle_stealth()
        except Exception:
            pass

    def _check_krx_api_once(self) -> str:
        """앱 시작 시 KRX_API_KEY 한 번 진단 → 상태 문자열 반환."""
        if not os.environ.get("KRX_API_KEY"):
            return "KRX 키 없음(Naver 폴백 사용)"
        try:
            d = krx_api_diagnose()
        except Exception:
            return "KRX 진단 실패"
        s = d.get("status")
        if s == "ok":
            return f"KRX OpenAPI 정상({d.get('rows', 0)}건)"
        if s == "unauthorized":
            return "KRX 키 등록됨·서비스 미승인(Naver 폴백)"
        return f"KRX {s}(Naver 폴백)"

    # --- 로딩 -----------------------------------------------------------
    def _on_timeframe_changed(self, _idx: int) -> None:
        """주기 변경 시 차트 탭만 다시 그림 + 5분봉이면 라이브 틱 시작."""
        if not hasattr(self, "_last_ohlcv") or self._last_ohlcv is None:
            return
        self._render_price(self._last_ohlcv, self.code_input.text().strip())
        fplt.refresh()
        # 5분봉 모드 + KIS 키 등록 시 1초 폴링 시작
        # 주기 변경 시 WS는 그대로 유지 (코드 동일하면 재구독 안 함)
        # 5분봉이 아닌 모드로 전환하면 라이브 봉 상태만 초기화
        if self.tf_combo.currentData() != "min5":
            self._live_bar = None

    def _on_rsi_toggled(self, _state: int) -> None:
        """RSI 패널 show/hide + 데이터 그리기."""
        on = self.cb_rsi.isChecked()
        self._rsi_panel.setVisible(on)
        # split sizes 조정 — 켜질 때 적당히 공간 할당, 꺼질 때 0
        self._adjust_chart_splits()
        d = getattr(self, "_last_render_data", None)
        if on and d is not None and not d.empty:
            self._draw_rsi(d)
        else:
            self.rsi_ax.reset()

    def _on_macd_toggled(self, _state: int) -> None:
        """MACD 패널 show/hide + 데이터 그리기."""
        on = self.cb_macd.isChecked()
        self._macd_panel.setVisible(on)
        self._adjust_chart_splits()
        d = getattr(self, "_last_render_data", None)
        if on and d is not None and not d.empty:
            self._draw_macd(d)
        else:
            self.macd_ax.reset()

    def _adjust_chart_splits(self) -> None:
        """RSI/MACD 켜져있는 개수에 따라 splitter 비율 재조정."""
        rsi_on = self.cb_rsi.isChecked()
        macd_on = self.cb_macd.isChecked()
        # 가격:거래량:RSI:MACD = 6:2:2:2 (켜진 것만 비율 차지)
        sizes = [600, 200, 200 if rsi_on else 0, 200 if macd_on else 0]
        try:
            self._chart_split.setSizes(sizes)
        except Exception:
            pass

    def _on_ichimoku_toggled(self, _state: int) -> None:
        """일목균형표 토글 — 차트 전체를 다시 그리지 않고 senkou span 아이템만 추가/제거.
        → reset() 안 하므로 zoom·pan 100% 보존, vol_ax X-link 깨질 일도 없음."""
        d = getattr(self, "_last_render_data", None)
        if self.cb_ichimoku.isChecked():
            # ON: 데이터 캐시 있으면 그대로 senkou만 추가
            if d is None or d.empty:
                # 캐시 없으면 어쩔 수 없이 풀 재렌더 (앱 첫 진입 직후 케이스)
                if hasattr(self, "_last_ohlcv") and self._last_ohlcv is not None:
                    self._render_price(self._last_ohlcv, self.code_input.text().strip())
                    fplt.refresh()
                return
            # 이미 켜져있던 잔여 아이템 제거 후 재그림 (중복 방지)
            self._remove_ichimoku()
            self._draw_ichimoku(d)
        else:
            # OFF: senkou 아이템만 제거
            self._remove_ichimoku()

    @staticmethod
    def _is_market_open() -> bool:
        """KRX 정규장 시간(09:00~15:30 KST, 평일) 체크."""
        now = _dt.now()
        if now.weekday() >= 5:
            return False
        t = now.time()
        return (t.hour, t.minute) >= (9, 0) and (t.hour, t.minute) <= (15, 30)

    @staticmethod
    def _bar_start_5min(now: _dt | None = None) -> _dt:
        """주어진 시각이 속한 5분 경계의 시작 시각 (예: 10:23:15 → 10:20:00)."""
        n = now or _dt.now()
        return n.replace(second=0, microsecond=0,
                         minute=(n.minute // 5) * 5)

    @staticmethod
    def _bar_start_for_tf(tf: str, now: _dt | None = None) -> _dt:
        """주기별 라이브 봉 시작 시각."""
        n = now or _dt.now()
        if tf == "min5":
            return n.replace(second=0, microsecond=0,
                             minute=(n.minute // 5) * 5)
        if tf == "week":
            # 이번 주 월요일 00:00 (W-FRI 기준이라 일요일 종가에 합쳐짐, 시각화엔 큰 차이 X)
            monday = n - timedelta(days=n.weekday())
            return monday.replace(hour=0, minute=0, second=0, microsecond=0)
        # day (default)
        return n.replace(hour=0, minute=0, second=0, microsecond=0)

    def eventFilter(self, obj, event):
        """전역 단축키. QLineEdit 포커스 중에는 무시 (타이핑 방해 X).
        - A : 스텔스 모드 토글
        - D : 일봉 전환
        - F : 5분봉 전환  (D 옆 키 — 한 손 빠른 전환)
        - W : 주봉 전환
        - [ : 창 투명도 ↓ (더 투명)
        - ] : 창 투명도 ↑ (더 불투명)
        - \\ : 투명도 100% 즉시 복원
        """
        from PySide6.QtCore import QEvent
        if event.type() == QEvent.KeyPress and not event.modifiers():
            fw = QApplication.focusWidget()
            if isinstance(fw, QLineEdit):
                return super().eventFilter(obj, event)
            key = event.key()
            if key == Qt.Key_A:
                self.btn_stealth.toggle()
                self._toggle_stealth()
                return True
            if key == Qt.Key_D:
                self._set_timeframe("day")
                return True
            if key == Qt.Key_F:
                self._set_timeframe("min5")
                return True
            if key == Qt.Key_W:
                self._set_timeframe("week")
                return True
            if key == Qt.Key_BracketLeft:   # [
                self._adjust_opacity(-0.05)
                return True
            if key == Qt.Key_BracketRight:  # ]
                self._adjust_opacity(+0.05)
                return True
            if key == Qt.Key_Backslash:     # \
                self.setWindowOpacity(1.0)
                try:
                    self.status.setText("투명도 100% (복원)")
                except Exception:
                    pass
                return True
            if key == Qt.Key_C:             # C : 화면 캡쳐 → 클립보드
                self._capture_to_clipboard()
                return True
        return super().eventFilter(obj, event)

    def _capture_to_clipboard(self) -> None:
        """현재 화면(컨트롤 바 + 탭 내용)을 캡쳐해 클립보드에 복사.
        Ctrl+V 로 AI 챗(Claude/ChatGPT) 입력란에 바로 붙여넣을 수 있음.
        - 일반 모드: 컨트롤 바 + 차트(또는 수급·펀더 탭) 전체
        - 스텔스 모드: 작은 라벨만 (의미 없음 — 자동으로 일반 모드로 전환할까?)
        """
        try:
            target = self.centralWidget() if self.centralWidget() else self
            pix = target.grab()
            cb = QApplication.clipboard()
            cb.setPixmap(pix)
            try:
                self.status.setText("📋 화면 캡쳐 — Ctrl+V 로 AI에 붙여넣기 (C 단축키)")
            except Exception:
                pass
        except Exception as e:
            try:
                self.status.setText(f"캡쳐 실패: {e}")
            except Exception:
                pass

    def _adjust_opacity(self, delta: float) -> None:
        """창 투명도 조절. 0.2(매우 투명) ~ 1.0(불투명) 범위.
        너무 투명하면 안 보여서 0.2 이하로는 안 내림."""
        try:
            cur = self.windowOpacity()
        except Exception:
            cur = 1.0
        new = max(0.2, min(1.0, cur + delta))
        self.setWindowOpacity(new)
        try:
            self.status.setText(f"투명도 {int(new * 100)}%  ([·] 조절, \\ 복원)")
        except Exception:
            pass

    def _set_timeframe(self, tf: str) -> None:
        """주기 콤보박스 값 변경 (currentIndexChanged 트리거됨)."""
        for i in range(self.tf_combo.count()):
            if self.tf_combo.itemData(i) == tf:
                if self.tf_combo.currentIndex() != i:
                    self.tf_combo.setCurrentIndex(i)
                return

    def _refresh_completer_model(self) -> None:
        """ticker_master에서 (code, name) 읽어 'name (code)' 형식 문자열 리스트로 자동완성 모델 갱신."""
        try:
            tickers = load_all_tickers()
            items = [f"{name} ({code})" for code, name in tickers]
            self._completer_model.setStringList(items)
        except Exception:
            pass

    def _toggle_stealth(self) -> None:
        """스텔스 모드: 차트/탭/색상 다 숨기고 종목명+가격만 작은 흑색으로."""
        self._stealth_on = self.btn_stealth.isChecked()
        if self._stealth_on:
            # 평상시 상태 저장 (복원용)
            self._normal_geometry = self.saveGeometry()
            cw = self.centralWidget()
            self._orig_root_margins = self._root_layout.contentsMargins()
            self._orig_root_spacing = self._root_layout.spacing()
            self._orig_bar_margins = self._bar_layout.contentsMargins()
            self._orig_bar_spacing = self._bar_layout.spacing()
            self._orig_min_size = self.minimumSize()
            self._orig_max_size = self.maximumSize()

            # 차트·컨트롤 위젯들 숨김
            for w in self._hideables:
                w.hide()
            self.tabs.hide()

            # 레이아웃 마진/간격 최소화
            self._root_layout.setContentsMargins(2, 2, 2, 2)
            self._root_layout.setSpacing(0)
            self._bar_layout.setContentsMargins(2, 1, 2, 1)
            self._bar_layout.setSpacing(4)

            # 종목코드 입력란 숨김 (포커스 가져가면 A 단축키 안 먹히는 문제 방지)
            self.code_input.hide()
            # 혹시 포커스 잡혀있으면 떼어내기
            try:
                self.code_input.clearFocus()
                self.setFocus()
            except Exception:
                pass

            # 종목명·가격 작은 흑색
            self.name_label.setStyleSheet(
                "font-size: 11px; padding: 0 3px; color: #333; font-weight: normal;"
            )
            self.price_label.setStyleSheet(
                "font-size: 11px; padding: 0 3px; color: #333; "
                "background-color: transparent; font-weight: normal;"
            )

            # 토글 버튼도 작게
            self.btn_stealth.setText("📖")
            self.btn_stealth.setMaximumWidth(24)
            self.btn_stealth.setMinimumWidth(24)

            self.setWindowTitle("Memo")

            # 진짜 작은 고정 사이즈
            self.setMinimumSize(0, 0)
            self.setMaximumSize(16777215, 16777215)
            # 위젯 변경이 layout에 반영된 뒤 adjust
            self.adjustSize()
            self.resize(360, 56)  # % 추가로 약간 더 넓게
            # 색 캐시 무효화 (다음 라벨 갱신 때 흑색 유지)
            self._last_label_color = "stealth"
        else:
            # 마진·간격·사이즈 복원
            self._root_layout.setContentsMargins(self._orig_root_margins)
            self._root_layout.setSpacing(self._orig_root_spacing)
            self._bar_layout.setContentsMargins(self._orig_bar_margins)
            self._bar_layout.setSpacing(self._orig_bar_spacing)
            self.setMinimumSize(self._orig_min_size)
            self.setMaximumSize(self._orig_max_size)

            # 위젯들 복원
            for w in self._hideables:
                w.show()
            self.tabs.show()

            self.code_input.show()
            self.code_input.setMaximumWidth(100)
            self.code_input.setMinimumWidth(0)
            self.code_input.setStyleSheet("")
            self.name_label.setStyleSheet(
                "font-weight: bold; padding: 0 12px; color: #2266cc;"
            )
            self.price_label.setStyleSheet(
                "font-size: 18px; font-weight: bold; padding: 2px 10px; "
                "background-color: #f5f5f5; border-radius: 4px;"
            )
            self.btn_stealth.setText("📕")
            self.btn_stealth.setMaximumWidth(32)
            self.btn_stealth.setMinimumWidth(0)
            self.setWindowTitle("DataChart v2 — 종합 데이터 차트")

            if self._normal_geometry:
                self.restoreGeometry(self._normal_geometry)
            self._last_label_color = None  # 다음 갱신 때 색상 재적용

    def _update_price_label(self, price: float, change_pct: float, is_live: bool = False) -> None:
        """가격 라벨 갱신. 스텔스 모드면 흑색·간략(가격+%만), 평소엔 빨강/파랑+태그."""
        if self._stealth_on:
            self.price_label.setText(f"{price:,.0f}  {change_pct:+.2f}%")
            return
        color = "#cc0000" if change_pct >= 0 else "#0066cc"
        tag = "  ⚡LIVE" if is_live else ""
        self.price_label.setText(f"{price:,.0f}원  {change_pct:+.2f}%{tag}")
        if color != self._last_label_color:
            bg = "#fff8e1" if is_live else "#f5f5f5"
            self.price_label.setStyleSheet(
                f"font-size: 18px; font-weight: bold; padding: 2px 10px; "
                f"color: {color}; background-color: {bg}; border-radius: 4px;"
            )
            self._last_label_color = color

    def _on_realtime_tick(self, snap: dict) -> None:
        """KIS WebSocket 체결 push 핸들러.
        - 가격 라벨: 매 틱 즉시 갱신 (가벼움, 가장 즉각적인 시각 반응)
        - 차트 라이브 봉: 200ms throttle로 렌더 큐 쌓이는 것 방지
        """
        if snap.get("code") != self.code_input.text().strip():
            return
        try:
            price = float(snap["price"])
            change_pct = float(snap.get("change_rate") or 0.0)
        except (KeyError, ValueError, TypeError):
            return

        # 가격 라벨 — 매 틱 갱신 (스텔스 모드면 흑색 간략 표시)
        self._update_price_label(price, change_pct, is_live=True)

        # 라이브 봉 누적 (주기별 분기)
        # 중요: KIS inquire-price의 open/high/low는 '오늘 일봉' OHL이지 5분봉 OHL이 아님.
        #       5분봉 모드에선 체결가(price)로만 누적해야 봉이 일봉 저가까지 늘어지지 않음.
        tf = self.tf_combo.currentData()
        cum_vol = snap.get("cum_volume") or 0
        bar_start = self._bar_start_for_tf(tf)
        is_new_bar = (self._live_bar is None
                      or self._live_bar.get("tf") != tf
                      or self._live_bar.get("start") != bar_start)

        if tf == "min5":
            # 5분봉: 체결가 단위 누적. KIS 일봉 OHL은 무시
            if is_new_bar:
                self._live_bar = {
                    "tf": tf, "start": bar_start,
                    "open": price, "high": price, "low": price, "close": price,
                    "volume_at_start": cum_vol, "volume": 0,
                }
            else:
                lb = self._live_bar
                lb["high"] = max(lb["high"], price)
                lb["low"] = min(lb["low"], price)
                lb["close"] = price
                lb["volume"] = max(0, cum_vol - lb["volume_at_start"])
        else:
            # 일/주봉: KIS의 오늘 일봉 OHL 사용 (앱 시작 전 발생한 OHL까지 정확히 반영)
            snap_open = float(snap.get("open") or price)
            snap_high = float(snap.get("high") or price)
            snap_low = float(snap.get("low") or price)
            if is_new_bar:
                self._live_bar = {
                    "tf": tf, "start": bar_start,
                    "open": snap_open,
                    "high": max(snap_high, price),
                    "low": min(snap_low, price),
                    "close": price,
                    "volume_at_start": 0,
                    "volume": cum_vol,
                }
            else:
                lb = self._live_bar
                lb["high"] = max(lb["high"], snap_high, price)
                lb["low"] = min(lb["low"], snap_low, price)
                lb["close"] = price
                lb["volume"] = cum_vol
        self._last_kis_price = price

        # 차트 렌더 throttle: 마지막 refresh로부터 200ms 이내면 skip
        now = _time.time()
        if now - self._last_chart_refresh < 0.2:
            return
        self._last_chart_refresh = now
        if tf == "min5":
            self._render_min5_with_live()
        else:
            # 일/주봉: 전체 _render_price 재호출 (live_today를 자동 합성)
            if self._last_ohlcv is not None:
                self._render_price(self._last_ohlcv, snap.get("code") or "")
        try:
            fplt.refresh()
        except Exception:
            pass

    def _poll_price_label_only(self) -> None:
        """KIS 현재가만 가져와 상단 가격 라벨 실시간 갱신 (차트는 안 건드림).
        5분봉이 아닐 때도 실시간 가격을 보여주기 위함."""
        if not self._is_market_open():
            return
        code = self.code_input.text().strip()
        if not (code.isdigit() and len(code) == 6):
            return
        snap = kis_current_price(code)
        if not snap or not snap.get("price"):
            return
        try:
            price = float(snap["price"])
            change_pct = float(snap.get("change_rate") or 0.0)
            self._update_price_label(price, change_pct, is_live=True)
        except Exception:
            pass

    def _poll_live_tick(self) -> None:
        """1초마다 KIS 현재가 조회 → 진행 중인 5분봉 OHLC 누적 → 차트 업데이트."""
        if not self._is_market_open():
            return  # 장 외 시간엔 폴링 비활성 (NXT/시간외 시세 혼란 방지)
        code = self.code_input.text().strip()
        if not (code.isdigit() and len(code) == 6):
            return
        snap = kis_current_price(code)
        if not snap or not snap.get("price"):
            return
        price = snap["price"]
        cum_vol = snap["volume"]
        bar_start = self._bar_start_5min()
        if self._live_bar is None or self._live_bar["start"] != bar_start:
            # 새 5분 봉 시작
            self._live_bar = {
                "start": bar_start,
                "open":  price,
                "high":  price,
                "low":   price,
                "close": price,
                "volume_at_start": cum_vol,
                "volume": 0,
            }
        else:
            lb = self._live_bar
            lb["high"]   = max(lb["high"], price)
            lb["low"]    = min(lb["low"], price)
            lb["close"]  = price
            lb["volume"] = max(0, cum_vol - lb["volume_at_start"])
        self._last_kis_price = price
        # 가격 라벨 실시간 갱신 (1초마다)
        try:
            change_pct = float(snap.get("change_rate") or 0.0)
            color = "#cc0000" if change_pct >= 0 else "#0066cc"
            self.price_label.setText(f"{price:,.0f}원  {change_pct:+.2f}%  🔴LIVE")
            self.price_label.setStyleSheet(
                f"font-size: 18px; font-weight: bold; padding: 2px 10px; "
                f"color: {color}; background-color: #f5f5f5; border-radius: 4px;"
            )
        except Exception:
            pass
        # 라이브 봉 포함해 차트 다시 그리기
        self._render_min5_with_live()
        fplt.refresh()

    def _render_min5_with_live(self) -> None:
        """5분봉 라이브 갱신 — _render_price를 호출해 캐시된 history + 라이브 봉 + 일목 + 비교 모두 그림."""
        if self._last_ohlcv is None:
            return
        # _render_price의 5분봉 분기에서 self._min5_history 캐시 + self._live_bar 합성을 처리
        self._render_price(self._last_ohlcv, self.code_input.text().strip())
        # 상태바에도 라이브 봉 정보 표시
        lb = self._live_bar
        if lb and lb.get("tf") == "min5":
            bar_t = lb["start"].strftime("%H:%M")
            self.status.setText(
                f"🔴 LIVE  {bar_t} 봉  O={lb['open']:,.0f} H={lb['high']:,.0f} "
                f"L={lb['low']:,.0f} C={lb['close']:,.0f} V={lb['volume']:,}"
            )

    def load_all(self) -> None:
        raw = self.code_input.text().strip()
        # 6자리 코드 그대로 / 한글 이름 → 코드 자동 변환
        code = resolve_ticker(raw)
        if not code:
            self.status.setText(f"매치 없음: '{raw}' — 6자리 코드 또는 정확한 종목명")
            return
        # 입력란을 정규화된 코드로 갱신 (사용자가 이름 입력했으면 코드 표시)
        if raw != code and code is not None:
            try:
                self.code_input.blockSignals(True)
                self.code_input.setText(code)
                self.code_input.blockSignals(False)
            except Exception:
                pass
        # 종목/주기 변경 시 진행 중이던 라이브 봉 + 5분봉 캐시 초기화
        self._live_bar = None
        self._last_kis_price = None
        self._min5_history = None

        name = get_name(code)
        # ticker_master 갱신 (이번에 조회한 종목 cache)
        try:
            upsert_ticker(code, name)
            self._refresh_completer_model()
        except Exception:
            pass
        self.name_label.setText(f"📊 {name}")
        self.status.setText(f"{name}({code}) 조회 중...")
        QApplication.processEvents()

        def _ohlcv_progress(done: int, total: int) -> None:
            self.status.setText(f"{name}({code}) KRX OpenAPI 호출 {done}/{total}...")
            QApplication.processEvents()

        try:
            ohlcv = fetch_ohlcv(code, progress_cb=_ohlcv_progress)
            investor = fetch_investor(code)
            fundamental = fetch_fundamental(code)
            cap = fetch_marketcap(code)
            foreign = fetch_foreign_rate(code)
        except Exception as e:
            self.status.setText(f"오류: {e}")
            return
        self._last_ohlcv = ohlcv

        # KRX 인증 우회: 빈 데이터는 Naver로 보강
        snapshot = fetch_naver_snapshot(code) if (
            fundamental.empty or cap.empty or (foreign is None or foreign.empty)
        ) else {}
        # 수급 ~3개월치 (페이지당 ~20행, 6 page ≈ 120 거래일 ≈ 6개월 — 넉넉히)
        flow_naver = fetch_naver_investor_flow(code, pages=6) if investor.empty else pd.DataFrame()
        # KIS 종목별 프로그램매매 일별 (실전 키 있을 때만)
        program_df = kis_program_trade_by_stock_daily(code)

        self._render_price(ohlcv, code)
        self._render_flow(investor, flow_naver, program_df)
        self._render_fundamental(code, name, ohlcv, fundamental, cap, foreign, snapshot)

        last_close = ohlcv["close"].iloc[-1]
        warn = ""
        if investor.empty or fundamental.empty:
            src = "Naver" if (not flow_naver.empty or snapshot) else "없음"
            warn = f"  ·  수급/펀더 출처: {src}"
        krx_info = f"  ·  {self._krx_status}" if getattr(self, "_krx_status", "") else ""
        min_info = ""
        if self.tf_combo.currentData() == "min5" and getattr(self, "_minute_source", ""):
            min_info = f"  ·  분봉 출처: {self._minute_source}"
        kis_info = ""
        ak, _, _ = _kis_resolve()
        if ak:
            kis_info = f"  ·  KIS LIVE({kis_env_label()}) 가능"
        self.status.setText(
            f"{name}({code})  ·  {len(ohlcv)}봉  ·  종가 {last_close:,.0f}원{warn}{krx_info}{min_info}{kis_info}"
        )

        # 실시간 시세 시작 결정 (정규장 시간 + KIS 키 있을 때)
        # 1순위: WebSocket (틱 단위 push, 진짜 실시간)
        # 2순위: REST 폴링 (WS 실패 또는 미사용 시)
        ak2, _, _ = _kis_resolve()
        if ak2 and self._is_market_open():
            self._kis_ws.start(code)
            # WS가 가격 라벨 + 라이브 봉 모두 처리하므로 REST 폴링은 정지
            if self._rt_timer.isActive():
                self._rt_timer.stop()
            if self._price_label_timer.isActive():
                self._price_label_timer.stop()
        else:
            self._kis_ws.stop()
            if self._rt_timer.isActive():
                self._rt_timer.stop()
            if self._price_label_timer.isActive():
                self._price_label_timer.stop()
        fplt.refresh()

    _YAXIS_PATCHED = False

    @classmethod
    def _ensure_yaxis_patch(cls):
        """finplot.YAxisItem.tickStrings를 한 번만 클래스 레벨로 monkey-patch.
        축 인스턴스에 `_dc_vol_format` 함수가 붙어있으면 그걸 사용, 아니면 원본 호출."""
        if cls._YAXIS_PATCHED:
            return
        try:
            ya_cls = fplt.YAxisItem
            orig = ya_cls.tickStrings
            def _patched(self, values, scale, spacing):
                fmt = getattr(self, "_dc_vol_format", None)
                if fmt is not None:
                    try:
                        # finplot은 정규화된 [0,1] 좌표값을 넘기고 vb.yscale.xform이 실제 데이터값으로 변환
                        xform = self.vb.yscale.xform if (self.vb and getattr(self.vb, "yscale", None)) else (lambda x: x)
                        real_values = [xform(v) for v in values]
                        return fmt(real_values)
                    except Exception:
                        pass
                return orig(self, values, scale, spacing)
            ya_cls.tickStrings = _patched
            cls._YAXIS_PATCHED = True
        except Exception:
            pass

    def _format_volume_y_axis(self, ax) -> None:
        """거래량 Y축 — 주기에 따라 동적 포맷 (finplot YAxisItem 클래스 패치 후 인스턴스 플래그).
        - 일/주봉: K 단위 (1,000,000 → 1000K, 500,000 → 500K)
        - 5분봉: raw 값 + 콤마 (1,500 / 300)
        """
        self._ensure_yaxis_patch()
        def vol_format(values):
            try:
                tf = self.tf_combo.currentData()
            except Exception:
                tf = "day"
            out = []
            if tf == "min5":
                for v in values:
                    absv = abs(v)
                    if absv >= 1:
                        out.append(f"{int(v):,}")
                    elif absv > 0:
                        out.append(f"{v:.2g}")
                    else:
                        out.append("0")
            else:
                for v in values:
                    absv = abs(v)
                    if absv >= 1000:
                        out.append(f"{v / 1000:.0f}K")
                    elif absv >= 1:
                        out.append(f"{int(v):,}")
                    elif absv > 0:
                        out.append(f"{v:.2g}")
                    else:
                        out.append("0")
            return out
        for axis_name in ("right", "left"):
            try:
                axis = ax.getAxis(axis_name)
                if axis is None:
                    continue
                # 인스턴스 플래그로 패치된 tickStrings에서 우리 포맷 호출하게 함
                axis._dc_vol_format = vol_format
                try:
                    axis.enableAutoSIPrefix(False)
                except Exception:
                    pass
                try:
                    axis.setLabel(text="거래량 (주)")
                except Exception:
                    pass
                # tick 캐시 무효화 → 즉시 새 포맷으로 재그림
                try:
                    axis.picture = None
                    axis.update()
                except Exception:
                    pass
            except Exception:
                pass

    @staticmethod
    def _format_y_axis_kmb(ax, label_text: str = "") -> None:
        """Y축 tick을 항상 raw 값 기반 K/M으로 명시. autoSIPrefix 끄고 직접 포맷.
        - 1,000 단위 ↑: K (예: 5K = 5,000주)
        - 1,000,000 단위 ↑: M (예: 1.5M = 150만주)
        - 그 미만: 천단위 콤마 (300, 1,500)
        라벨은 단순 텍스트("거래량 (주)") — autoSIPrefix가 prefix 못 붙이게.
        """
        def tick_strings(values, scale, spacing):
            out = []
            for v in values:
                absv = abs(v)
                if absv >= 1e9:
                    out.append(f"{v / 1e9:.1f}B")
                elif absv >= 1e6:
                    out.append(f"{v / 1e6:.1f}M")
                elif absv >= 1e3:
                    out.append(f"{v / 1e3:.0f}K")
                elif absv >= 1:
                    out.append(f"{int(v):,}")
                elif absv > 0:
                    # 매우 작거나 fractional 값 (있어선 안 되지만 fail-safe)
                    out.append(f"{v:.2g}")
                else:
                    out.append("0")
            return out
        for axis_name in ("right", "left"):
            try:
                axis = ax.getAxis(axis_name)
                if axis is None:
                    continue
                # 1) autoSIPrefix 끄기 — pyqtgraph가 자체 scale 적용 못 하게
                try:
                    axis.enableAutoSIPrefix(False)
                except Exception:
                    pass
                # 2) tickStrings monkey-patch
                axis.tickStrings = tick_strings
                # 3) 단순 텍스트 라벨 (units= 안 줘서 prefix 못 붙게)
                if label_text:
                    try:
                        axis.setLabel(text=label_text)
                    except Exception:
                        pass
            except Exception:
                pass

    @staticmethod
    def _fmt_kmb(v: float) -> str:
        """숫자를 K/M/B 약어로 포맷 (절대값 기반)."""
        absv = abs(v)
        if absv >= 1e9:
            return f"{v / 1e9:.1f}B"
        if absv >= 1e6:
            return f"{v / 1e6:.1f}M"
        if absv >= 1e3:
            return f"{v / 1e3:.0f}K"
        return f"{int(v):,}"

    # --- 차트 공통 그리기 헬퍼 -----------------------------------------
    # 4종 이동평균 사양: (기간, 색, 두께) — 20만 강조, 나머지는 얇게
    _MA_SPECS = (
        (5,   "#2e8b57", 0.5),  # 녹색 sea green
        (20,  "#8b4513", 1.5),  # 갈색 saddle brown — 강조
        (112, "#9933cc", 0.5),  # 보라
        (224, "#cccc33", 0.5),  # 노랑(올리브)
    )

    def _draw_candle_volume_indicators(self, d: pd.DataFrame) -> None:
        """캔들 + 4종 MA + 거래량(raw, Y축에 K/M 포맷터) + 일목균형표(옵션)."""
        fplt.candlestick_ochl(d[["open", "close", "high", "low"]], ax=self.price_ax)
        for period, color, w in self._MA_SPECS:
            if len(d) >= period:
                fplt.plot(d["close"].rolling(period).mean(),
                          ax=self.price_ax, color=color, width=w)
        # 거래량 raw 데이터 그대로 — 주기별 K/raw 포맷 (vol_ax.reset() 후 매번 재적용)
        fplt.volume_ocv(d[["open", "close", "volume"]], ax=self.vol_ax)
        self._format_volume_y_axis(self.vol_ax)
        # 좌상단 "최대 거래량" 오버레이 갱신
        try:
            max_vol = float(d["volume"].max() or 0)
            avg_vol = float(d["volume"].mean() or 0)
            self._vol_legend.setText(
                f"최대 {self._fmt_kmb(max_vol)}주  ·  평균 {self._fmt_kmb(avg_vol)}주"
            )
        except Exception:
            pass
        # 일목균형표 (옵션)
        if self.cb_ichimoku.isChecked():
            self._draw_ichimoku(d)

        # RSI / MACD (옵션) — 켜져있으면 데이터 갱신
        if self.cb_rsi.isChecked():
            self._draw_rsi(d)
        if self.cb_macd.isChecked():
            self._draw_macd(d)

    def _draw_rsi(self, d: pd.DataFrame) -> None:
        """RSI(14) 그리기 — 보라색 선 + 30/70/50 점선 가이드 + 우상단 현재값."""
        self.rsi_ax.reset()
        if len(d) < 14:
            return
        rsi = compute_rsi(d["close"], period=14)
        rsi_clean = rsi.dropna()
        if rsi_clean.empty:
            return
        fplt.plot(rsi, ax=self.rsi_ax, color="#7733cc", width=1.0)
        # 30/70 점선 + 50 가는 점선
        import pyqtgraph as pg
        for level, color, w in [(70, "#cc4444", 0.6), (50, "#888888", 0.3), (30, "#4488cc", 0.6)]:
            line = pg.InfiniteLine(pos=level, angle=0,
                                   pen=pg.mkPen(color, style=Qt.DashLine, width=w))
            self.rsi_ax.addItem(line)
        # 좌상단 현재값 라벨
        try:
            cur = float(rsi_clean.iloc[-1])
            zone = "과매수" if cur >= 70 else ("과매도" if cur <= 30 else "중립")
            self._rsi_legend.setText(f"RSI(14): {cur:.1f}  ·  {zone}")
        except Exception:
            pass

    def _draw_macd(self, d: pd.DataFrame) -> None:
        """MACD(12/26/9) — MACD선(파랑), Signal선(주황), Histogram(0선 위/아래 색 분리), 0 가이드."""
        self.macd_ax.reset()
        if len(d) < 26:
            return
        m = compute_macd(d["close"], fast=12, slow=26, signal=9)
        macd, signal, hist = m["macd"], m["signal"], m["histogram"]
        if macd.dropna().empty:
            return
        import pyqtgraph as pg
        # 0 가이드선
        zero_line = pg.InfiniteLine(pos=0, angle=0,
                                    pen=pg.mkPen("#888888", style=Qt.DashLine, width=0.4))
        self.macd_ax.addItem(zero_line)
        # Histogram — 양수 빨강, 음수 파랑 (한국 시장 관례)
        # finplot.bar 가 OHLC 모양 막대를 못 그려서 직접 BarGraphItem 사용
        try:
            from PySide6.QtGui import QColor
            x_idx = list(range(len(hist)))
            heights = hist.fillna(0).values
            # 양/음 분리해서 그려야 색 구분 가능
            pos_x = [i for i, v in zip(x_idx, heights) if v >= 0]
            pos_h = [v for v in heights if v >= 0]
            neg_x = [i for i, v in zip(x_idx, heights) if v < 0]
            neg_h = [v for v in heights if v < 0]
            if pos_x:
                bar_pos = pg.BarGraphItem(x=pos_x, height=pos_h, width=0.7,
                                          brush="#ff9988", pen=pg.mkPen("#dd2200", width=0))
                self.macd_ax.addItem(bar_pos)
            if neg_x:
                bar_neg = pg.BarGraphItem(x=neg_x, height=neg_h, width=0.7,
                                          brush="#88aaff", pen=pg.mkPen("#0066dd", width=0))
                self.macd_ax.addItem(bar_neg)
        except Exception:
            pass
        # MACD 선 / Signal 선 (line plot은 finplot 으로)
        fplt.plot(macd, ax=self.macd_ax, color="#0066dd", width=1.0)
        fplt.plot(signal, ax=self.macd_ax, color="#ff8800", width=1.0)
        # 좌상단 현재값 라벨
        try:
            cm = float(macd.dropna().iloc[-1])
            cs = float(signal.dropna().iloc[-1])
            ch = float(hist.dropna().iloc[-1])
            cross = "골든" if cm > cs and ch > 0 else ("데드" if cm < cs and ch < 0 else "혼조")
            self._macd_legend.setText(
                f"MACD: {cm:+.1f}  ·  Sig: {cs:+.1f}  ·  Hist: {ch:+.1f}  ·  {cross}"
            )
        except Exception:
            pass

    def _draw_ichimoku(self, d: pd.DataFrame) -> None:
        """일목균형표 — 선행스팬1·2를 점선으로 표시.
        파라미터: 전환 9, 기준 26, 선행스팬2 52, 선행/후행 shift 26 (HTS 표준).
        그려진 plot 아이템을 self._ichi_items에 저장 → 토글 OFF 시 제거 가능."""
        ichi = compute_ichimoku(d, conv=9, base=26, span_b_period=52, shift=26)
        if ichi["senkou_a"].dropna().empty:
            return
        item_a = fplt.plot(ichi["senkou_a"], ax=self.price_ax, color="#dd2200")
        item_b = fplt.plot(ichi["senkou_b"], ax=self.price_ax, color="#0066dd")
        # 점선 펜으로 override
        try:
            import pyqtgraph as pg
            from PySide6.QtGui import QColor
            a = item_a if hasattr(item_a, "setPen") else getattr(item_a, "plot_obj", item_a)
            b = item_b if hasattr(item_b, "setPen") else getattr(item_b, "plot_obj", item_b)
            if a is not None and hasattr(a, "setPen"):
                a.setPen(pg.mkPen(QColor("#dd2200"), width=1, style=Qt.DashLine))
            if b is not None and hasattr(b, "setPen"):
                b.setPen(pg.mkPen(QColor("#0066dd"), width=1, style=Qt.DashLine))
        except Exception:
            pass
        # 토글 OFF 시 제거할 수 있도록 추적
        self._ichi_items = [item_a, item_b]

    def _remove_ichimoku(self) -> None:
        """일목균형표 plot 아이템만 제거 (price_ax 전체 reset 안 함 → zoom 보존)."""
        items = getattr(self, "_ichi_items", None)
        if not items:
            return
        for it in items:
            try:
                self.price_ax.removeItem(it)
            except Exception:
                pass
        self._ichi_items = []

    @staticmethod
    def _make_diagonal_brush(color_rgb: tuple, tile: int = 8, alpha: int = 220):
        """Qt.BDiagPattern이 pyqtgraph FillBetweenItem에서 무시되는 문제 우회.
        QPixmap에 대각선을 직접 그려서 텍스처 brush로 반환 (타일링되어 빗금처럼 보임)."""
        from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QPixmap
        pix = QPixmap(tile, tile)
        pix.fill(QColor(0, 0, 0, 0))   # 투명 배경
        painter = QPainter(pix)
        painter.setRenderHint(QPainter.Antialiasing, True)
        color = QColor(*color_rgb, alpha)
        painter.setPen(QPen(color, 1))
        # 좌하 → 우상 대각선 (BDiagPattern 모양)
        painter.drawLine(0, tile, tile, 0)
        # 타일 경계가 자연스럽게 이어지도록 인접 라인 추가
        painter.drawLine(-tile, tile, 0, 0)
        painter.drawLine(tile, tile + tile, tile + tile, tile)
        painter.end()
        return QBrush(pix)

    def _draw_kumo_from_items(self, item_a, item_b) -> None:
        """선행스팬1·2 사이를 빗금으로 채움.
        pyqtgraph FillBetweenItem이 Qt brush 패턴을 무시하므로
        픽스맵 텍스처를 만들어 brush로 사용."""
        import pyqtgraph as pg
        from PySide6.QtGui import QColor
        a = item_a if hasattr(item_a, "xData") else getattr(item_a, "plot_obj", item_a)
        b = item_b if hasattr(item_b, "xData") else getattr(item_b, "plot_obj", item_b)
        if a is None or b is None:
            return
        # 1) 옅은 솔리드 fill — 구름대 영역을 시각적으로 잡아두기
        solid = pg.mkBrush(QColor(95, 184, 95, 50))
        fill_solid = pg.FillBetweenItem(a, b, brush=solid)
        self.price_ax.addItem(fill_solid)
        # 2) 빗금 텍스처 fill — 위에 겹쳐서 hatch 효과
        hatch_brush = self._make_diagonal_brush((40, 130, 60), tile=8, alpha=220)
        fill_hatch = pg.FillBetweenItem(a, b, brush=hatch_brush)
        self.price_ax.addItem(fill_hatch)

    def _draw_compare_index(self, d: pd.DataFrame) -> None:
        """선택된 비교 지수를 종목 가격과 동일점 정규화해 오버레이.
        KRX API가 평일 단위로 호출되므로 캐시 빈 첫 fetch는 수백 회 → progress_cb로
        status 갱신 + processEvents 로 UI 응답성 유지 (안 하면 'Not Responding')."""
        cmp_idx = self.cmp_combo.currentData()
        if not cmp_idx:
            return
        def _prog(done: int, total: int) -> None:
            try:
                self.status.setText(f"{cmp_idx} 지수 불러오는 중 ({done}/{total})...")
                QApplication.processEvents()
            except Exception:
                pass
        self.status.setText(f"{cmp_idx} 지수 불러오는 중...")
        QApplication.processEvents()
        idx_df = fetch_krx_index_daily(cmp_idx, days=DEFAULT_DAYS, progress_cb=_prog)
        if idx_df.empty:
            self.status.setText(f"{cmp_idx} 지수 데이터 없음 (KRX 키/서비스 권한 확인)")
            return
        self.status.setText(f"{cmp_idx} 오버레이 완료")
        k = idx_df.copy()
        k["date"] = pd.to_datetime(k["date"])
        k = k.set_index("date").sort_index()
        k = k.loc[k.index >= d.index.min()]
        if k.empty:
            return
        k_norm = k["close"] / k["close"].iloc[0] * d["close"].iloc[0]
        # 좀 더 두껍게 + 짙은 색으로 (얇은 노랑은 캡쳐에서 잘 안 보임)
        line_color = "#ff8800"  # 주황 — MA·캔들 어떤 색과도 안 겹침
        item = fplt.plot(k_norm, ax=self.price_ax, color=line_color, width=1.5,
                         legend=f"비교: {cmp_idx} (정규화)")
        # 우측 끝점에 명시적 라벨 부착 — AI 캡쳐 분석 시 라인 정체 인식하게 함
        try:
            import pyqtgraph as pg
            from PySide6.QtGui import QColor
            last_x = len(d) - 1
            last_y = float(k_norm.iloc[-1])
            text = pg.TextItem(text=f"  {cmp_idx} (정규화)", color=QColor(line_color),
                               anchor=(0, 0.5))
            text.setPos(last_x, last_y)
            self.price_ax.addItem(text)
            # 토글/주기 변경 시 정리 위해 리스트로 보관 (선택)
            if not hasattr(self, "_compare_items"):
                self._compare_items = []
            self._compare_items.append(text)
        except Exception:
            pass

    # --- 렌더러 ---------------------------------------------------------
    def _render_price(self, daily_df: pd.DataFrame, code: str) -> None:
        """선택된 주기(일/주/5분)에 맞춰 캔들 + MA + 거래량 렌더."""
        self.price_ax.reset()
        self.vol_ax.reset()
        self.rsi_ax.reset()
        self.macd_ax.reset()
        # reset()이 X-link을 끊을 수 있어 매번 재확립
        try:
            self.vol_ax.setXLink(self.price_ax)
            self.rsi_ax.setXLink(self.price_ax)
            self.macd_ax.setXLink(self.price_ax)
        except Exception:
            pass
        tf = self.tf_combo.currentData()

        # 일/주봉 모드에 오늘 봉 합성:
        # 1순위 - WS로 누적된 _live_bar (장중)
        # 2순위 - KIS REST inquire-price 스냅샷 (장 외 시간에도 오늘 OHLC 사용)
        live_today = None
        if self._live_bar and self._live_bar.get("tf") in ("day", "week"):
            lb = self._live_bar
            live_today = pd.DataFrame([{
                "date": pd.Timestamp(lb["start"]),
                "open": lb["open"],
                "high": lb["high"],
                "low": lb["low"],
                "close": lb["close"],
                "volume": int(lb["volume"]),
            }])
        else:
            # WS 비활성(장 외 시간) → KIS 스냅샷으로 오늘 봉 합성
            ak_t, _, _ = _kis_resolve()
            if ak_t:
                snap = kis_current_price(code)
                if snap and snap.get("price"):
                    today_naive = pd.Timestamp.now().normalize()
                    p = float(snap["price"])
                    live_today = pd.DataFrame([{
                        "date": today_naive,
                        "open":  float(snap.get("open") or p),
                        "high":  float(snap.get("high") or p),
                        "low":   float(snap.get("low") or p),
                        "close": p,
                        "volume": int(snap.get("volume") or 0),
                    }])

        if tf == "week":
            d = resample_to_weekly(daily_df)
            if live_today is not None:
                d = d[~(pd.to_datetime(d["date"]) == live_today["date"].iloc[0])]
                d = pd.concat([d, live_today], ignore_index=True)
            time_col = "date"
            ma_short, ma_long = 4, 12  # 4주(약 1개월), 12주(약 3개월)
        elif tf == "min5":
            # 캐시 활용: WS tick으로 자주 재렌더되므로 매번 fetch는 비쌈
            if self._min5_history is None or self._min5_history.empty:
                self.status.setText(f"{code} 5분봉 로딩 중 (Yahoo Finance)...")
                QApplication.processEvents()
                d_hist = fetch_minute_yahoo(code, interval="5m", days=60)
                src = "Yahoo 5m"
                if d_hist.empty:
                    self.status.setText(f"{code} 5분봉 폴백 (Naver siseJson)...")
                    QApplication.processEvents()
                    df_1min = fetch_minute_naver(code, days=5)
                    if df_1min.empty:
                        self.status.setText("5분봉 데이터 없음")
                        return
                    # Naver는 누적 거래량으로 옴 → diff 적용
                    d_hist = resample_to_5min(df_1min, cumulative_volume=True)
                    src = "Naver 합성"
                # KIS 오늘 1분봉으로 yfinance 지연 갭(~15분) 보강
                ak_kis, _, _ = _kis_resolve()
                if ak_kis:
                    self.status.setText(f"{code} 오늘 갭 보강 중 (KIS 분봉)...")
                    QApplication.processEvents()
                    kis_1min = fetch_minute_kis_today(code, max_bars=120)
                    if not kis_1min.empty:
                        # KIS cntg_vol은 분당 거래량 → diff 불필요 (cumulative_volume=False)
                        kis_5m = resample_to_5min(kis_1min, cumulative_volume=False)
                        if not kis_5m.empty:
                            d_hist["datetime"] = pd.to_datetime(d_hist["datetime"])
                            kis_5m["datetime"] = pd.to_datetime(kis_5m["datetime"])
                            # KIS가 커버하는 시간대 이후의 yfinance 봉은 KIS로 교체
                            kis_min_t = kis_5m["datetime"].min()
                            d_hist = d_hist[d_hist["datetime"] < kis_min_t]
                            d_hist = pd.concat([d_hist, kis_5m], ignore_index=True)
                            d_hist = d_hist.sort_values("datetime").reset_index(drop=True)
                            src = src + " + KIS 갭보강"
                self._minute_source = src
                self._min5_history = d_hist.copy()
            d = self._min5_history.copy()
            # 라이브 봉 합성
            if self._live_bar and self._live_bar.get("tf") == "min5":
                lb = self._live_bar
                live_row = pd.DataFrame([{
                    "datetime": pd.Timestamp(lb["start"]),
                    "open": lb["open"], "high": lb["high"],
                    "low": lb["low"], "close": lb["close"],
                    "volume": int(lb["volume"]),
                }])
                d["datetime"] = pd.to_datetime(d["datetime"])
                d = d[d["datetime"] != live_row["datetime"].iloc[0]]
                d = pd.concat([d, live_row], ignore_index=True)
            time_col = "datetime"
            ma_short, ma_long = 12, 60   # 사용 안 함, 호환 변수

        else:  # day
            d = daily_df.copy()
            d["date"] = pd.to_datetime(d["date"])
            if live_today is not None:
                d = d[d["date"] != live_today["date"].iloc[0]]
                d = pd.concat([d, live_today], ignore_index=True)
            time_col = "date"
            ma_short, ma_long = 20, 60

        if d.empty:
            return
        d[time_col] = pd.to_datetime(d[time_col])
        d = d.set_index(time_col).sort_index()

        # 일목 토글 시 재사용할 데이터 캐시 (datetime-index 가진 OHLCV)
        self._last_render_data = d
        # 캔들 + 4종 이동평균(5/20/112/224) + 거래량 + 일목균형표 (공통 헬퍼)
        self._draw_candle_volume_indicators(d)

        # 현재가 라벨 + 차트 가로 점선
        # KIS 키 있으면 실시간 KIS 현재가, 없으면 OHLCV 마지막 종가 (지연)
        try:
            import pyqtgraph as pg
            last_close = float(d["close"].iloc[-1])
            prev_close = float(d["close"].iloc[-2]) if len(d) > 1 else last_close

            display_price = last_close
            change_pct = ((last_close - prev_close) / prev_close * 100) if prev_close else 0.0
            is_live = False
            ak, _, _ = _kis_resolve()
            if ak:
                snap = kis_current_price(code)
                if snap and snap.get("price"):
                    display_price = float(snap["price"])
                    if snap.get("change_rate") is not None:
                        change_pct = float(snap["change_rate"])
                    is_live = True

            box_color = "#cc0000" if change_pct >= 0 else "#0066cc"
            hline = pg.InfiniteLine(pos=display_price, angle=0,
                                    pen=pg.mkPen(box_color, width=1, style=Qt.DashLine))
            self.price_ax.addItem(hline)
            self._update_price_label(display_price, change_pct, is_live=is_live)
        except Exception:
            pass

        # 지수 비교 오버레이 (일/주봉만)
        if tf in ("day", "week"):
            self._draw_compare_index(d)

    def _render_flow(self, df: pd.DataFrame, naver_df: pd.DataFrame | None = None,
                     program_df: pd.DataFrame | None = None) -> None:
        self.flow_ax.reset()
        # KRX 데이터(거래대금 원) 우선
        if not df.empty:
            d = df.copy()
            d["date"] = pd.to_datetime(d["date"])
            d = d.set_index("date").sort_index()
            scale = 1e8  # 억원
            fplt.plot(d["foreign_all"].cumsum() / scale, ax=self.flow_ax,
                      legend="외국인 누적순매수(억원)", color="#cc3333")
            fplt.plot(d["institution"].cumsum() / scale, ax=self.flow_ax,
                      legend="기관 누적순매수(억원)", color="#3366cc")
            fplt.plot(d["individual"].cumsum() / scale, ax=self.flow_ax,
                      legend="개인 누적순매수(억원)", color="#999999")
            self._fill_flow_table(d, source="KRX", program_df=program_df)
            return

        # KRX 비었으면 Naver 폴백 (단위는 주식수 기반 → 종가 곱해서 거래대금 추정)
        if naver_df is None or naver_df.empty:
            self._fill_flow_table(pd.DataFrame(), source="Naver", program_df=program_df)
            return
        d = naver_df.copy()
        d["date"] = pd.to_datetime(d["date"])
        d = d.set_index("date").sort_index()
        # 외국인/기관 순매수 거래대금 추정 = 순매수 주식수 × 종가
        scale = 1e8  # 억원
        foreign_value = (d["foreign_net"] * d["close"]).cumsum() / scale
        inst_value = (d["institution_net"] * d["close"]).cumsum() / scale
        fplt.plot(foreign_value, ax=self.flow_ax,
                  legend="외국인 누적순매수(억원·Naver)", color="#cc3333")
        fplt.plot(inst_value, ax=self.flow_ax,
                  legend="기관 누적순매수(억원·Naver)", color="#3366cc")
        self._fill_flow_table(d, source="Naver", program_df=program_df)

    def _fill_flow_table(self, d: pd.DataFrame, source: str,
                         program_df: pd.DataFrame | None = None) -> None:
        """일별 투자자별 순매매 + 프로그램매매 차익/비차익 테이블 채움.
        최신 날짜가 위 (MTS 관례).
        - source='KRX': foreign_all / institution / individual (거래대금 원→억원)
        - source='Naver': foreign_net / institution_net + 개인은 -(외+기) (주)
        - program_df: KIS 프로그램매매 일별 (date, arb_net, nonarb_net 주)
        """
        from PySide6.QtGui import QBrush, QColor
        self.flow_table.setRowCount(0)
        if d is None or d.empty:
            return

        # 프로그램매매 데이터 인덱싱 (날짜로 빠른 조회)
        prog_idx = None
        if program_df is not None and not program_df.empty:
            pdf = program_df.copy()
            pdf["date"] = pd.to_datetime(pdf["date"])
            prog_idx = pdf.set_index("date")

        d_sorted = d.sort_index(ascending=False)  # 최신 위
        rows = []
        # 등락률 계산용으로 시간순 close 변화 미리 만들기
        close_seq = d.sort_index()["close"] if "close" in d.columns else None
        pct_chg = close_seq.pct_change() * 100 if close_seq is not None else None

        for date, row in d_sorted.iterrows():
            close = row.get("close", None)
            volume = row.get("volume", None)
            if source == "KRX":
                fr = row.get("foreign_all", 0) / 1e8 if "foreign_all" in row else None
                ins = row.get("institution", 0) / 1e8 if "institution" in row else None
                indi = row.get("individual", 0) / 1e8 if "individual" in row else None
                fr_unit = "억원"
                f_rate = None
            else:
                fr = row.get("foreign_net")
                ins = row.get("institution_net")
                indi = -(fr + ins) if (fr is not None and ins is not None) else None
                fr_unit = "주"
                f_rate = row.get("foreign_rate")
            chg = pct_chg.loc[date] if (pct_chg is not None and date in pct_chg.index) else None
            # 프로그램매매 lookup
            arb_net = nonarb_net = None
            if prog_idx is not None:
                d_norm = pd.Timestamp(date).normalize()
                if d_norm in prog_idx.index:
                    pr = prog_idx.loc[d_norm]
                    arb_net = pr.get("arb_net")
                    nonarb_net = pr.get("nonarb_net")
            rows.append((date, close, chg, volume, fr, ins, indi, f_rate, fr_unit,
                         arb_net, nonarb_net))

        self.flow_table.setRowCount(len(rows))
        red = QBrush(QColor("#cc2200"))
        blue = QBrush(QColor("#0066cc"))
        gray = QBrush(QColor("#666666"))

        def _fmt_int(v) -> str:
            if v is None or pd.isna(v):
                return "-"
            return f"{int(v):,}"

        def _fmt_signed(v, unit: str = "") -> str:
            if v is None or pd.isna(v):
                return "-"
            if unit == "억원":
                return f"{v:+,.1f}"
            return f"{int(v):+,}"

        for i, r in enumerate(rows):
            (date, close, chg, volume, fr, ins, indi, f_rate, fr_unit,
             arb_net, nonarb_net) = r
            cells = [
                pd.Timestamp(date).strftime("%Y-%m-%d"),
                _fmt_int(close),
                f"{chg:+.2f}" if (chg is not None and not pd.isna(chg)) else "-",
                _fmt_int(volume),
                _fmt_signed(fr, fr_unit),
                _fmt_signed(ins, fr_unit),
                _fmt_signed(indi, fr_unit),
                f"{f_rate:.2f}" if (f_rate is not None and not pd.isna(f_rate)) else "-",
                _fmt_signed(arb_net),
                _fmt_signed(nonarb_net),
            ]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                # 등락·순매매·프로그램매매 부호별 색
                if col == 2:
                    if chg is not None and not pd.isna(chg):
                        item.setForeground(red if chg > 0 else (blue if chg < 0 else gray))
                elif col in (4, 5, 6):
                    val = (fr, ins, indi)[col - 4]
                    if val is not None and not pd.isna(val):
                        item.setForeground(red if val > 0 else (blue if val < 0 else gray))
                elif col in (8, 9):
                    val = (arb_net, nonarb_net)[col - 8]
                    if val is not None and not pd.isna(val):
                        item.setForeground(red if val > 0 else (blue if val < 0 else gray))
                if col >= 1:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.flow_table.setItem(i, col, item)
        # 헤더 갱신 (10개 컬럼)
        unit_str = "억원" if source == "KRX" else "주"
        self.flow_table.setColumnCount(10)
        self.flow_table.setHorizontalHeaderLabels([
            "날짜", "종가", "등락(%)", "거래량",
            f"외국인({unit_str})",
            f"기관({unit_str})",
            f"개인({unit_str})",
            "외국인 지분율(%)",
            "프로그램 차익(주)",
            "프로그램 비차익(주)",
        ])

    def _render_fundamental(
        self,
        code: str,
        name: str,
        ohlcv: pd.DataFrame,
        fundamental: pd.DataFrame,
        cap: pd.DataFrame,
        foreign: pd.DataFrame,
        snapshot: dict | None = None,
    ) -> None:
        self.fund_ax.reset()
        self.fund_ax2.reset()
        self.fund_revenue_ax.reset()
        snap = snapshot or {}

        last_close = ohlcv["close"].iloc[-1] if not ohlcv.empty else None
        last_cap = cap["market_cap"].iloc[-1] if not cap.empty else (snap.get("market_cap"))
        last_fund = fundamental.iloc[-1] if not fundamental.empty else None
        last_foreign = foreign.iloc[-1] if (foreign is not None and not foreign.empty) else None

        display_name = name if name and name != code else (snap.get("name") or code)
        self.lbl_name.setText(f"{display_name} ({code})")
        self.lbl_close.setText(f"{last_close:,.0f}원" if last_close else "-")
        if last_cap:
            self.lbl_cap.setText(f"{last_cap/1e8:,.0f}억원")
        else:
            self.lbl_cap.setText("-")

        # PER / PBR / EPS / BPS / DIV — KRX 우선, 없으면 Naver 스냅샷
        def _set(lbl, krx_val, snap_val, fmt):
            v = krx_val if (krx_val is not None and pd.notna(krx_val)) else snap_val
            if v is None or (isinstance(v, float) and pd.isna(v)):
                lbl.setText("-")
            else:
                lbl.setText(fmt.format(v))

        if last_fund is not None:
            _set(self.lbl_per, last_fund.get("per"), snap.get("per"), "{:.2f}")
            _set(self.lbl_pbr, last_fund.get("pbr"), snap.get("pbr"), "{:.2f}")
            _set(self.lbl_eps, last_fund.get("eps"), snap.get("eps"), "{:,.0f}")
            _set(self.lbl_bps, last_fund.get("bps"), None, "{:,.0f}")
            _set(self.lbl_div, last_fund.get("div_y"), None, "{:.2f}")
        else:
            _set(self.lbl_per, None, snap.get("per"), "{:.2f}")
            _set(self.lbl_pbr, None, snap.get("pbr"), "{:.2f}")
            _set(self.lbl_eps, None, snap.get("eps"), "{:,.0f}")
            self.lbl_bps.setText("-")
            self.lbl_div.setText("-")

        if last_foreign is not None and pd.notna(last_foreign.get("foreign_rate")):
            self.lbl_foreign.setText(f"{last_foreign['foreign_rate']:.2f}%")
        elif snap.get("foreign_rate") is not None:
            self.lbl_foreign.setText(f"{snap['foreign_rate']:.2f}%")
        else:
            self.lbl_foreign.setText("-")

        # 추이 차트: PER / PBR + 외국인 지분율
        if not fundamental.empty:
            f = fundamental.copy()
            f["date"] = pd.to_datetime(f["date"])
            f = f.set_index("date").sort_index()
            fplt.plot(f["per"], ax=self.fund_ax, legend="PER", color="#3399ff")
            fplt.plot(f["pbr"], ax=self.fund_ax, legend="PBR", color="#ff9933")

        if foreign is not None and not foreign.empty:
            fr = foreign.copy()
            fr["date"] = pd.to_datetime(fr["date"])
            fr = fr.set_index("date").sort_index()
            fplt.plot(fr["foreign_rate"], ax=self.fund_ax2,
                      legend="외국인 지분율(%)", color="#cc3333")

        # 분기 매출/영업이익 (yfinance 재무제표, 단위: 억원)
        try:
            rev = fetch_quarterly_revenue_yahoo(code)
            if not rev.empty:
                r = rev.copy()
                r["date"] = pd.to_datetime(r["date"])
                r = r.set_index("date").sort_index()
                if "revenue" in r.columns:
                    rev_s = (r["revenue"] / 1e8).dropna()
                    if not rev_s.empty:
                        fplt.plot(rev_s, ax=self.fund_revenue_ax,
                                  legend="매출액(억원)", color="#2266cc", style="o")
                if "operating_income" in r.columns:
                    op_s = (r["operating_income"] / 1e8).dropna()
                    if not op_s.empty:
                        fplt.plot(op_s, ax=self.fund_revenue_ax,
                                  legend="영업이익(억원)", color="#cc6622", style="o")
        except Exception:
            pass


def main() -> None:
    init_db()
    import atexit
    # DPI 경고 회피: QApplication 생성 전에 명시적으로 컨텍스트 지정
    try:
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
    except Exception:
        pass
    app = QApplication(sys.argv)
    win = DataChartWindow()
    atexit.register(lambda: win._kis_ws.stop())
    win.show()
    fplt.show(qt_exec=False)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
