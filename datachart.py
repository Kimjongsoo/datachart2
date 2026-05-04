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

import sys
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pandas as pd
from PySide6.QtCore import Qt, QTimer
import time as _time
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QMainWindow, QPushButton, QTabWidget, QVBoxLayout, QWidget,
)
import finplot as fplt
from pykrx import stock

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
import json as _json
import urllib.error
import urllib.parse as _urlparse

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
        # KST naive로 정규화 — yfinance가 tz-aware/naive 둘 다 가능하므로 양쪽 처리
        try:
            if df.index.tz is None:
                # naive → UTC로 가정 후 KST 변환
                df.index = df.index.tz_localize("UTC").tz_convert("Asia/Seoul").tz_localize(None)
            else:
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


def resample_to_5min(df_1min: pd.DataFrame) -> pd.DataFrame:
    """1분봉 → 5분봉. close-only 데이터에서도 OHL 합성. volume은 누적이라 차분."""
    if df_1min.empty:
        return df_1min
    d = df_1min.copy()
    d["datetime"] = pd.to_datetime(d["datetime"])
    d = d.set_index("datetime").sort_index()
    # 누적 volume → 1분 거래량 차분 (장 시작 첫 봉은 그대로)
    delta_vol = d["volume"].diff().fillna(d["volume"]).clip(lower=0)
    d = d.assign(_vol=delta_vol)
    agg = d.resample("5min", label="right", closed="right").agg({
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


def fetch_naver_investor_flow(code: str, pages: int = 3) -> pd.DataFrame:
    """Naver frgn.naver에서 외국인/기관 일별 순매수(주) 스크래핑.
    페이지당 약 20행, pages=3이면 60일치. EUC-KR 인코딩."""
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
            if (end - latest).days <= CACHE_FRESH_DAYS:
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

    # 미래 shift봉 인덱스 생성: 가장 흔한 봉 간격(주말 갭 회피)
    if len(df.index) >= 2:
        diffs = pd.Series(df.index[1:] - df.index[:-1])
        delta = diffs.mode().iloc[0] if not diffs.empty else pd.Timedelta(days=1)
    else:
        delta = pd.Timedelta(days=1)
    future = pd.DatetimeIndex([df.index[-1] + delta * (i + 1) for i in range(shift)])
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

        # 상단 컨트롤 바
        bar = QHBoxLayout()
        bar.addWidget(QLabel("종목코드"))
        self.code_input = QLineEdit(DEFAULT_CODE)
        self.code_input.setMaximumWidth(100)
        self.code_input.returnPressed.connect(self.load_all)
        bar.addWidget(self.code_input)

        load_btn = QPushButton("불러오기")
        load_btn.clicked.connect(self.load_all)
        bar.addWidget(load_btn)

        # 종목명 표시
        self.name_label = QLabel("-")
        self.name_label.setStyleSheet("font-weight: bold; padding: 0 12px; color: #2266cc;")
        bar.addWidget(self.name_label)

        bar.addWidget(QLabel("주기"))
        self.tf_combo = QComboBox()
        self.tf_combo.addItem("일봉", "day")
        self.tf_combo.addItem("주봉", "week")
        self.tf_combo.addItem("5분봉", "min5")
        self.tf_combo.currentIndexChanged.connect(self._on_timeframe_changed)
        bar.addWidget(self.tf_combo)

        # 일목균형표 토글
        self.cb_ichimoku = QCheckBox("일목균형표")
        self.cb_ichimoku.setToolTip("전환선(9)·기준선(26)·선행스팬1·2(26봉 forward)·후행스팬(26봉 backward)")
        self.cb_ichimoku.stateChanged.connect(self._on_timeframe_changed)
        bar.addWidget(self.cb_ichimoku)

        # 지수 비교 (KRX OpenAPI 승인된 서비스 사용)
        bar.addWidget(QLabel("비교"))
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

        self.status = QLabel("준비")
        bar.addWidget(self.status)
        bar.addStretch()
        root.addLayout(bar)

        # 탭 구성
        self.tabs = QTabWidget()
        root.addWidget(self.tabs, stretch=1)

        # 탭 1: 캔들차트 (가격/거래량/외국인누적/매출 4단)
        price_axes = _as_list(fplt.create_plot_widget(
            master=self, rows=4, init_zoom_periods=120
        ))
        self.price_ax = price_axes[0]
        self.vol_ax = price_axes[1]
        self.foreign_cum_ax = price_axes[2]
        self.revenue_ax = price_axes[3]
        self.axs_price = [self.price_ax, self.vol_ax,
                          self.foreign_cum_ax, self.revenue_ax]
        self.tabs.addTab(_wrap_ax(self.price_ax), "차트")

        # 탭 2: 수급(투자자별 누적 순매수)
        flow_axes = _as_list(fplt.create_plot_widget(
            master=self, rows=1, init_zoom_periods=120
        ))
        self.flow_ax = flow_axes[0]
        self.axs_flow = [self.flow_ax]
        self.tabs.addTab(_wrap_ax(self.flow_ax), "수급")

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

        fund_axes = _as_list(fplt.create_plot_widget(
            master=self, rows=2, init_zoom_periods=120
        ))
        self.fund_ax, self.fund_ax2 = fund_axes[0], fund_axes[1]
        self.axs_fund = [self.fund_ax, self.fund_ax2]
        fund_layout.addWidget(_wrap_ax(self.fund_ax), stretch=1)
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

        # 첫 로드
        self.load_all()

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
        ak3, _, _ = _kis_resolve()
        if self.tf_combo.currentData() == "min5" and ak3:
            if not self._rt_timer.isActive():
                self._rt_timer.start()
        else:
            if self._rt_timer.isActive():
                self._rt_timer.stop()
            self._live_bar = None

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
        # 라이브 봉 포함해 차트 다시 그리기
        self._render_min5_with_live()
        fplt.refresh()

    def _render_min5_with_live(self) -> None:
        """5분봉 history + 진행 중인 라이브 봉을 결합해 차트 갱신."""
        if self._min5_history is None or self._min5_history.empty or not self._live_bar:
            return
        history = self._min5_history.copy()
        # 라이브 봉이 history 마지막 시간보다 새 봉인 경우만 append
        live_row = pd.DataFrame([{
            "datetime": self._live_bar["start"],
            "open":  self._live_bar["open"],
            "high":  self._live_bar["high"],
            "low":   self._live_bar["low"],
            "close": self._live_bar["close"],
            "volume": int(self._live_bar["volume"]),
        }])
        # 같은 봉 시간대가 history에 이미 있으면 교체, 없으면 append
        history["datetime"] = pd.to_datetime(history["datetime"])
        live_row["datetime"] = pd.to_datetime(live_row["datetime"])
        history = history[history["datetime"] != live_row["datetime"].iloc[0]]
        merged = pd.concat([history, live_row], ignore_index=True)
        merged = merged.set_index("datetime").sort_index()

        self.price_ax.reset()
        self.vol_ax.reset()
        fplt.candlestick_ochl(merged[["open", "close", "high", "low"]], ax=self.price_ax)
        fplt.plot(merged["close"].rolling(12).mean(), ax=self.price_ax,
                  legend="MA12", color="#3399ff")
        fplt.plot(merged["close"].rolling(60).mean(), ax=self.price_ax,
                  legend="MA60", color="#ff9933")
        fplt.volume_ocv(merged[["open", "close", "volume"]], ax=self.vol_ax)
        # 상태바에 LIVE 표시
        lb = self._live_bar
        bar_t = lb["start"].strftime("%H:%M")
        self.status.setText(
            f"🔴 LIVE  {bar_t} 봉  O={lb['open']:,.0f} H={lb['high']:,.0f} "
            f"L={lb['low']:,.0f} C={lb['close']:,.0f} V={lb['volume']:,}"
        )

    def load_all(self) -> None:
        code = self.code_input.text().strip()
        if not (code.isdigit() and len(code) == 6):
            self.status.setText("6자리 종목코드를 입력하세요")
            return
        # 종목/주기 변경 시 진행 중이던 라이브 봉 초기화
        self._live_bar = None
        self._last_kis_price = None

        name = get_name(code)
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
        flow_naver = fetch_naver_investor_flow(code) if investor.empty else pd.DataFrame()

        self._render_price(ohlcv, code)
        self._render_flow(investor, flow_naver)
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

        # 5분봉 + KIS 키 등록 + 정규장 시간이면 자동 LIVE 폴링 시작
        ak2, _, _ = _kis_resolve()
        if (self.tf_combo.currentData() == "min5"
                and ak2 and self._is_market_open()):
            if not self._rt_timer.isActive():
                self._rt_timer.start()
        fplt.refresh()

    # --- 렌더러 ---------------------------------------------------------
    def _render_price(self, daily_df: pd.DataFrame, code: str) -> None:
        """선택된 주기(일/주/5분)에 맞춰 캔들 + MA + 거래량 + 외국인누적 + 매출 렌더."""
        self.price_ax.reset()
        self.vol_ax.reset()
        self.foreign_cum_ax.reset()
        self.revenue_ax.reset()
        tf = self.tf_combo.currentData()

        if tf == "week":
            d = resample_to_weekly(daily_df)
            time_col = "date"
            ma_short, ma_long = 4, 12  # 4주(약 1개월), 12주(약 3개월)
        elif tf == "min5":
            self.status.setText(f"{code} 5분봉 로딩 중 (Yahoo Finance)...")
            QApplication.processEvents()
            # 1순위: Yahoo Finance 진짜 5분 OHLC (60일치)
            d = fetch_minute_yahoo(code, interval="5m", days=60)
            src = "Yahoo 5m"
            if d.empty:
                self.status.setText(f"{code} 5분봉 폴백 (Naver siseJson)...")
                QApplication.processEvents()
                df_1min = fetch_minute_naver(code, days=5)
                if df_1min.empty:
                    self.status.setText("5분봉 데이터 없음 (Yahoo·Naver 모두 빈 응답)")
                    return
                d = resample_to_5min(df_1min)
                src = "Naver 합성"
            self._minute_source = src
            time_col = "datetime"
            ma_short, ma_long = 12, 60  # 1시간(12*5분), 5시간(60*5분)
            # 라이브 봉 추가용 history 보관
            self._min5_history = d.copy()
        else:  # day
            d = daily_df.copy()
            time_col = "date"
            ma_short, ma_long = 20, 60

        if d.empty:
            return
        d[time_col] = pd.to_datetime(d[time_col])
        d = d.set_index(time_col).sort_index()

        fplt.candlestick_ochl(d[["open", "close", "high", "low"]], ax=self.price_ax)
        fplt.plot(d["close"].rolling(ma_short).mean(), ax=self.price_ax,
                  legend=f"MA{ma_short}", color="#3399ff")
        fplt.plot(d["close"].rolling(ma_long).mean(), ax=self.price_ax,
                  legend=f"MA{ma_long}", color="#ff9933")
        fplt.volume_ocv(d[["open", "close", "volume"]], ax=self.vol_ax)

        # 우측 현재가 박스: 빨강(상승)/파랑(하락) 색상 배경
        try:
            import pyqtgraph as pg
            last_close = float(d["close"].iloc[-1])
            prev_close = float(d["close"].iloc[-2]) if len(d) > 1 else last_close
            change = last_close - prev_close
            change_pct = (change / prev_close * 100) if prev_close else 0.0
            box_color = "#cc0000" if change >= 0 else "#0066cc"
            text = f" {last_close:,.0f}\n {change_pct:+.2f}% "
            ti = pg.TextItem(text, color="#ffffff", anchor=(0, 0.5),
                             fill=pg.mkBrush(box_color), border=pg.mkPen(box_color))
            ti.setPos(d.index[-1], last_close)
            self.price_ax.addItem(ti)
            # 가로선도 함께
            hline = pg.InfiniteLine(pos=last_close, angle=0,
                                    pen=pg.mkPen(box_color, width=1, style=Qt.DashLine))
            self.price_ax.addItem(hline)
        except Exception:
            pass

        # 외국인 누적 패널 (Naver 데이터 사용, 차트 기간에 맞춰 누적)
        try:
            flow = fetch_naver_investor_flow(code)
            if not flow.empty:
                f = flow.copy()
                f["date"] = pd.to_datetime(f["date"])
                f = f.set_index("date").sort_index()
                # 차트 기간 안으로 자르고 누적 (단위: 주식수 → 만주)
                f = f.loc[(f.index >= d.index.min()) & (f.index <= d.index.max())]
                if not f.empty:
                    cum_foreign = f["foreign_net"].cumsum() / 10000  # 만주
                    fplt.plot(cum_foreign, ax=self.foreign_cum_ax,
                              legend="외국인 누적순매수(만주)", color="#cc3333")
        except Exception:
            pass

        # 매출 패널 (yfinance 분기 재무제표, 단위: 억원)
        try:
            rev = fetch_quarterly_revenue_yahoo(code)
            if not rev.empty:
                r = rev.copy()
                r = r.set_index("date").sort_index()
                # 차트 기간 안의 분기만
                r = r.loc[r.index >= d.index.min()]
                if not r.empty and "revenue" in r.columns:
                    rev_series = (r["revenue"] / 1e8).dropna()  # 억원
                    if not rev_series.empty:
                        fplt.plot(rev_series, ax=self.revenue_ax,
                                  legend="매출액(억)", color="#88aa44", style="o")
                    if "operating_income" in r.columns:
                        op_series = (r["operating_income"] / 1e8).dropna()
                        if not op_series.empty:
                            fplt.plot(op_series, ax=self.revenue_ax,
                                      legend="영업이익(억)", color="#aa4488", style="o")
        except Exception:
            pass

        # 일목균형표 5선 (옵션)
        if self.cb_ichimoku.isChecked():
            ichi = compute_ichimoku(d)
            if not ichi["tenkan"].dropna().empty:
                fplt.plot(ichi["tenkan"],   ax=self.price_ax, legend="전환선(9)",  color="#cc0000")
                fplt.plot(ichi["kijun"],    ax=self.price_ax, legend="기준선(26)", color="#0066cc")
                fplt.plot(ichi["senkou_a"], ax=self.price_ax, legend="선행스팬1",   color="#5fb85f")
                fplt.plot(ichi["senkou_b"], ax=self.price_ax, legend="선행스팬2",   color="#cc6677")
                fplt.plot(ichi["chikou"],   ax=self.price_ax, legend="후행스팬",    color="#999900")

        # 지수 비교 오버레이: 시작일=종목 종가로 정규화
        cmp_idx = self.cmp_combo.currentData()
        if cmp_idx and tf in ("day", "week"):
            idx_df = fetch_krx_index_daily(cmp_idx, days=DEFAULT_DAYS)
            if not idx_df.empty:
                k = idx_df.copy()
                k["date"] = pd.to_datetime(k["date"])
                k = k.set_index("date").sort_index()
                k = k.loc[k.index >= d.index.min()]
                if not k.empty:
                    k_norm = k["close"] / k["close"].iloc[0] * d["close"].iloc[0]
                    fplt.plot(k_norm, ax=self.price_ax,
                              legend=f"{cmp_idx}(정규화)", color="#888800")

    def _render_flow(self, df: pd.DataFrame, naver_df: pd.DataFrame | None = None) -> None:
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
            return

        # KRX 비었으면 Naver 폴백 (단위는 주식수 기반 → 종가 곱해서 거래대금 추정)
        if naver_df is None or naver_df.empty:
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


def main() -> None:
    init_db()
    # DPI 경고 회피: QApplication 생성 전에 명시적으로 컨텍스트 지정
    try:
        QApplication.setHighDpiScaleFactorRoundingPolicy(
            Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
        )
    except Exception:
        pass
    app = QApplication(sys.argv)
    win = DataChartWindow()
    win.show()
    fplt.show(qt_exec=False)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
