#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
경매 대시보드 전체 파이프라인 (main.py)
────────────────────────────────────────────────────────────
  1단계  scrape_auctions.py 서브프로세스 실행 (경매 목록 수집 — 기존 코드 그대로 호출)
  2단계  주소 → 법정동코드/PNU 변환 (도로명주소 API, JUSO_CONFIRM_KEY)
  3단계  공공데이터 API 연동 (DATA_GO_KR_KEY)
           ① 건축물대장(건축HUB)  → is_illegal_building, actual_use, approval_date, 면적
           ② 공동주택 공시가격     → official_price, official_price_year
           ③ 실거래가(물건유형별)   → nearby_trade_price / count / date
  4단계  투자분석 (safe_jeonse, safety_margin_pct, is_under_100m, risk_tags)
  5단계  권리분석 (매각물건명세서 키워드 스캔 → rights_risk, rights_keywords)
  6단계  docs/data/auctions.json 저장 + 백업 + docs/data/error_log.json

설계 포인트
  - scrape_auctions.py 는 auctions.json 을 "기본 필드만" 으로 덮어쓴다.
    → 실행 전에 기존 파일을 읽어 두었다가, 같은 사건(id+주소)은 분석 결과를 다시 붙인다.
    → 같은 물건을 매일 다시 조회하지 않으므로 API 일일 트래픽을 크게 아낀다.
  - API 호출: 재시도 최대 3회(지수 백오프 + 지터), 429/Retry-After 처리,
    호스트별 호출 간격 제한, 엔드포인트별 서킷브레이커(키 오류·트래픽 초과 시 즉시 차단).
  - 실거래가는 (유형, 시군구, 계약월) 단위로 디스크 캐시 → 같은 구 물건끼리 공유.
  - 출력은 {"updated", "auctions": [...]} 구조를 유지 → 기존 index.html(d.auctions) 하위 호환.

사용법
  python3 scraper/main.py                 # 전체 실행
  python3 scraper/main.py --skip-scrape   # 수집 생략, 기존 auctions.json 으로 분석만
  python3 scraper/main.py --limit 5 -v    # 신규 분석 5건만 (API 키 동작 확인용)
  python3 scraper/main.py --no-rights     # 권리분석(브라우저) 생략

환경변수
  DATA_GO_KR_KEY      공공데이터포털 인증키 (Encoding/Decoding 키 모두 가능)
  JUSO_CONFIRM_KEY    도로명주소 검색 API 승인키
  VWORLD_KEY          (선택) 브이월드 인증키 — 있으면 공시가격 조회 정확도 ↑
  VWORLD_DOMAIN       (선택) 브이월드 키 발급 시 등록한 서비스 URL
  MAX_ENRICH_PER_RUN  1회 실행당 신규 분석 최대 건수 (기본 1000)
  ENRICH_TTL_DAYS     건축물대장·공시가격 재조회 주기 (기본 30일)
  RIGHTS_MAX_PER_RUN  1회 실행당 매각물건명세서 조회 최대 건수 (기본 40)
  RIGHTS_TTL_DAYS     권리분석 재조회 주기 (기본 7일)
  SAFETY_MARGIN_PCT   "✨ 안전마진 확보" 기준 % (기본 20)
  TRADE_MONTHS        실거래가 조회 기간(개월, 기본 6)
  WORKERS             API 병렬 작업 수 (기본 4)
  SCRAPE_TIMEOUT_MIN  스크래퍼 제한 시간(분, 기본 45)
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import statistics
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests

# ════════════════════════════════════════════════════════════
# 경로 · 설정
# ════════════════════════════════════════════════════════════
KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
SCRAPER_DIR = ROOT / "scraper"
DATA_DIR = ROOT / "docs" / "data"
AUCTIONS_PATH = DATA_DIR / "auctions.json"
ERROR_LOG_PATH = DATA_DIR / "error_log.json"
CACHE_DIR = SCRAPER_DIR / ".cache"          # 실거래가·주소·분석 캐시 (git 커밋 X, Actions cache 로 보존)
BACKUP_DIR = SCRAPER_DIR / "backup"         # auctions.json 백업 (최근 N개 보관)
SCRAPER_SCRIPT = SCRAPER_DIR / "scrape_auctions.py"


def _env_int(name: str, default: int) -> int:
    """환경변수를 정수로 읽기 (잘못된 값이면 기본값)"""
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _clean_key(raw: str | None) -> str:
    """공공데이터 인증키 정리 — Encoding 키(%2B 등)를 넣어도 이중 인코딩되지 않게 디코딩"""
    raw = (raw or "").strip()
    return unquote(raw) if "%" in raw else raw


DATA_GO_KR_KEY = _clean_key(os.environ.get("DATA_GO_KR_KEY"))
JUSO_CONFIRM_KEY = (os.environ.get("JUSO_CONFIRM_KEY") or "").strip()
VWORLD_KEY = (os.environ.get("VWORLD_KEY") or "").strip()
VWORLD_DOMAIN = (os.environ.get("VWORLD_DOMAIN") or "").strip()

MAX_ENRICH_PER_RUN = _env_int("MAX_ENRICH_PER_RUN", 1000)
ENRICH_TTL_DAYS = _env_int("ENRICH_TTL_DAYS", 30)
RIGHTS_MAX_PER_RUN = _env_int("RIGHTS_MAX_PER_RUN", 40)
RIGHTS_TTL_DAYS = _env_int("RIGHTS_TTL_DAYS", 7)
SAFETY_MARGIN_PCT = _env_int("SAFETY_MARGIN_PCT", 20)
TRADE_MONTHS = _env_int("TRADE_MONTHS", 6)
WORKERS = max(1, _env_int("WORKERS", 4))
SCRAPE_TIMEOUT_MIN = _env_int("SCRAPE_TIMEOUT_MIN", 45)

MAX_RETRIES = 3            # 재시도 최대 3회 (최초 1회 + 재시도 3회)
BACKOFF_BASE = 1.0         # 지수 백오프 기본 대기(초): 1 → 2 → 4
HTTP_TIMEOUT = 20          # 요청 타임아웃(초)
BREAKER_THRESHOLD = 5      # 엔드포인트 연속 실패 N회 → 이번 실행 동안 차단
BACKUP_KEEP = 7            # 백업 보관 개수
ERROR_LOG_MAX = 500        # error_log.json 최대 기록 수

# 호스트별 최소 호출 간격(초) — 공공 API 초당 호출 제한 대응
HOST_MIN_INTERVAL = {
    "apis.data.go.kr": 0.06,
    "business.juso.go.kr": 0.05,
    "www.juso.go.kr": 0.05,
    "api.vworld.kr": 0.1,
}

# API 엔드포인트
JUSO_URLS = [
    "https://business.juso.go.kr/addrlink/addrLinkApi.do",
    "https://www.juso.go.kr/addrlink/addrLinkApi.do",      # 구 주소 (예비)
]
BLD_BASE = "https://apis.data.go.kr/1613000/BldRgstHubService"          # 건축HUB 건축물대장
NSDI_APT_PRICE_URL = (                                                   # 공동주택가격 (공공데이터포털 경유)
    "https://apis.data.go.kr/1611000/nsdi/ApartHousingPriceService/attr/getApartHousingPriceAttr"
)
VWORLD_APT_PRICE_URL = "https://api.vworld.kr/ned/data/getApartHousingPriceAttr"   # 공동주택가격
VWORLD_INDV_PRICE_URL = "https://api.vworld.kr/ned/data/getIndvdHousingPriceAttr"  # 개별주택가격
TRADE_APIS = {   # 실거래가 (유형 → 서비스/오퍼레이션)
    "apt":  ("RTMSDataSvcAptTrade",  "getRTMSDataSvcAptTrade"),    # 아파트 매매
    "rh":   ("RTMSDataSvcRHTrade",   "getRTMSDataSvcRHTrade"),     # 연립·다세대 매매
    "offi": ("RTMSDataSvcOffiTrade", "getRTMSDataSvcOffiTrade"),   # 오피스텔 매매
    "sh":   ("RTMSDataSvcSHTrade",   "getRTMSDataSvcSHTrade"),     # 단독·다가구 매매
    "nrg":  ("RTMSDataSvcNrgTrade",  "getRTMSDataSvcNrgTrade"),    # 상업업무용 매매
}
TRADE_TYPE_LABEL = {"apt": "아파트", "rh": "연립다세대", "offi": "오피스텔", "sh": "단독다가구", "nrg": "상업업무용"}

# 분석 단계에서 추가되는 필드 (재실행 시 이전 결과를 이어 붙일 대상)
ENRICH_FIELDS = [
    "bjdong_code", "pnu", "road_address", "jibun_address", "umd_name",
    "building_dong", "unit_ho",
    "is_illegal_building", "actual_use", "actual_use_level", "approval_date",
    "exclusive_area", "building_area",
    "official_price", "official_price_year",
    "nearby_trade_price", "nearby_trade_count", "nearby_trade_date", "nearby_trade_basis",
    "safe_jeonse", "safety_margin_pct", "is_under_100m", "risk_tags",
    "rights_risk", "rights_keywords", "rights_checked_at",
    "enriched_at", "enrich_status",
]

RESIDENTIAL_KW = ["아파트", "다세대", "연립", "빌라", "단독", "다가구", "오피스텔"]


# ════════════════════════════════════════════════════════════
# 공통 유틸
# ════════════════════════════════════════════════════════════
def now_kst() -> datetime:
    return datetime.now(KST)


def log(msg: str) -> None:
    """타임스탬프 로그 (Actions 로그에서 보기 쉽게 즉시 flush)"""
    print(f"[{now_kst():%H:%M:%S}] {msg}", flush=True)


def vlog(msg: str) -> None:
    if ARGS and ARGS.verbose:
        log("  · " + msg)


def read_json(path: Path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json_atomic(path: Path, data, indent: int | None = 2) -> None:
    """임시 파일에 쓴 뒤 교체 → 저장 중 중단돼도 파일이 깨지지 않음"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)
        f.write("\n")
    os.replace(tmp, path)


def to_int(v) -> int | None:
    """'82,500' / '  1234 ' / 1234.0 → int, 실패 시 None"""
    if v is None:
        return None
    try:
        s = str(v).replace(",", "").strip()
        return int(float(s)) if s else None
    except ValueError:
        return None


def to_float(v) -> float | None:
    try:
        s = str(v).replace(",", "").strip()
        return float(s) if s else None
    except (TypeError, ValueError):
        return None


def digits(s) -> str:
    """'514동' → '514', '제901호' → '901', 'B01' → '01' → 앞자리 0 제거 비교용"""
    d = re.sub(r"\D", "", str(s or ""))
    return d.lstrip("0") or ("0" if d else "")


def fmt_ymd(s) -> str | None:
    """'20150312' → '2015-03-12'"""
    s = re.sub(r"\D", "", str(s or ""))
    if len(s) >= 8 and s[:8] != "00000000":
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return None


def days_since(iso: str | None) -> float:
    """ISO 시각으로부터 경과 일수 (없으면 무한대)"""
    if not iso:
        return float("inf")
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=KST)
        return (now_kst() - dt).total_seconds() / 86400
    except ValueError:
        return float("inf")


# ════════════════════════════════════════════════════════════
# 오류 수집기 → error_log.json
# ════════════════════════════════════════════════════════════
class ErrorCollector:
    def __init__(self):
        self._lock = threading.Lock()
        self.items: list[dict] = []
        self.counts: dict[str, int] = {}

    def add(self, stage: str, message: str, item_id: str | None = None, level: str = "error") -> None:
        with self._lock:
            key = f"{stage}:{level}"
            self.counts[key] = self.counts.get(key, 0) + 1
            if len(self.items) < ERROR_LOG_MAX:
                self.items.append({
                    "time": now_kst().isoformat(timespec="seconds"),
                    "stage": stage,
                    "level": level,
                    "id": item_id,
                    "message": str(message)[:500],
                })
        if level == "error" and (ARGS and ARGS.verbose):
            log(f"  ! [{stage}] {item_id or ''} {message}")


ERRORS = ErrorCollector()
ARGS: argparse.Namespace | None = None


# ════════════════════════════════════════════════════════════
# HTTP 클라이언트 — 재시도 · 백오프 · Rate limit · 서킷브레이커
# ════════════════════════════════════════════════════════════
class ApiError(Exception):
    """API 오류. retryable=재시도 가능, fatal=키/권한/엔드포인트 문제(즉시 차단)"""

    def __init__(self, message: str, code: str | None = None,
                 retryable: bool = False, fatal: bool = False, retry_after: float | None = None):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.fatal = fatal
        self.retry_after = retry_after


class EndpointBlocked(ApiError):
    """서킷브레이커로 차단된 엔드포인트 호출"""


class HttpClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (auction-dashboard data pipeline)",
            "Accept": "application/json, application/xml;q=0.9, */*;q=0.8",
        })
        self._host_lock = threading.Lock()
        self._host_next: dict[str, float] = {}
        self._breaker_lock = threading.Lock()
        self._fail_count: dict[str, int] = {}
        self._blocked: dict[str, str] = {}
        self.stats = {"calls": 0, "retries": 0, "failures": 0}

    # ── Rate limit: 호스트별 최소 호출 간격 ─────────────────
    def _throttle(self, url: str) -> None:
        host = urlparse(url).hostname or ""
        gap = HOST_MIN_INTERVAL.get(host, 0.1)
        with self._host_lock:
            now = time.monotonic()
            slot = max(now, self._host_next.get(host, 0.0))
            self._host_next[host] = slot + gap
        wait = slot - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    # ── 서킷브레이커 ────────────────────────────────────
    def is_blocked(self, key: str) -> bool:
        return key in self._blocked

    def _block(self, key: str, reason: str) -> None:
        with self._breaker_lock:
            if key not in self._blocked:
                self._blocked[key] = reason
                log(f"  ⛔ [{key}] 이번 실행 동안 호출 중단: {reason}")
                ERRORS.add("api", f"엔드포인트 차단 [{key}]: {reason}", level="error")

    def _record(self, key: str, ok: bool, reason: str = "") -> None:
        with self._breaker_lock:
            if ok:
                self._fail_count[key] = 0
                return
            self._fail_count[key] = self._fail_count.get(key, 0) + 1
            n = self._fail_count[key]
        if n >= BREAKER_THRESHOLD:
            self._block(key, f"연속 {n}회 실패 (마지막: {reason})")

    # ── GET + 재시도 ────────────────────────────────────
    def get(self, key: str, url: str, params: dict, parser):
        """parser(resp) 로 파싱한 결과 반환. 실패 시 ApiError"""
        if self.is_blocked(key):
            raise EndpointBlocked(f"차단된 엔드포인트: {key}")
        last_err: ApiError | None = None
        for attempt in range(MAX_RETRIES + 1):
            self._throttle(url)
            self.stats["calls"] += 1
            try:
                resp = self.session.get(url, params=params, timeout=HTTP_TIMEOUT)
            except requests.RequestException as e:
                last_err = ApiError(f"네트워크 오류: {type(e).__name__}: {e}", retryable=True)
            else:
                last_err = self._check_status(resp)
                if last_err is None:
                    try:
                        result = parser(resp)
                        self._record(key, True)
                        return result
                    except ApiError as e:
                        last_err = e
                    except (ValueError, ET.ParseError, KeyError, TypeError) as e:
                        last_err = ApiError(f"응답 파싱 실패: {e} / {resp.text[:150]!r}", retryable=True)

            # ── 실패 처리 ──
            if last_err.fatal:
                self._block(key, str(last_err))
                raise last_err
            if not last_err.retryable or attempt == MAX_RETRIES:
                break
            self.stats["retries"] += 1
            # 지수 백오프 + 지터 (Retry-After 가 있으면 우선)
            delay = last_err.retry_after or (BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 0.5))
            vlog(f"재시도 {attempt + 1}/{MAX_RETRIES} [{key}] {delay:.1f}s 후 — {last_err}")
            time.sleep(min(delay, 60))

        self.stats["failures"] += 1
        # 서비스 장애(재시도 소진)만 차단 카운트 — 검색어/파라미터 문제는 해당 물건만의 문제
        if last_err.retryable:
            self._record(key, False, str(last_err))
        raise last_err

    @staticmethod
    def _check_status(resp: requests.Response) -> ApiError | None:
        sc = resp.status_code
        if sc == 200:
            return None
        body = resp.text[:150].replace("\n", " ")
        if sc == 429:
            ra = to_float(resp.headers.get("Retry-After")) or 5.0
            return ApiError(f"HTTP 429 호출 제한: {body}", code="429", retryable=True, retry_after=ra)
        if sc in (401, 403):
            return ApiError(f"HTTP {sc} 인증 실패(키 확인 필요): {body}", code=str(sc), fatal=True)
        if sc == 404:
            return ApiError(f"HTTP 404 엔드포인트 없음: {body}", code="404", fatal=True)
        if sc >= 500:
            return ApiError(f"HTTP {sc} 서버 오류: {body}", code=str(sc), retryable=True)
        return ApiError(f"HTTP {sc}: {body}", code=str(sc))


HTTP = HttpClient()


# ════════════════════════════════════════════════════════════
# 응답 파서
# ════════════════════════════════════════════════════════════
def _check_datagokr_code(code: str, msg: str) -> bool:
    """공공데이터포털 결과코드 판정. 반환: True=정상, False=데이터 없음. 오류면 ApiError"""
    raw = (code or "").strip().upper()
    c = raw.replace("INFO-", "").lstrip("0")
    if raw == "" or c == "":                       # 00 / 000 / INFO-000
        return True
    if c in ("3", "200"):                           # 03 NODATA / INFO-200
        return False
    m = f"[{raw}] {msg}"
    if c == "22":                                   # 일일 트래픽 초과 → 오늘은 재시도 무의미
        raise ApiError(f"일일 트래픽 초과 {m}", code=raw, fatal=True)
    if c in ("12", "20", "30", "31", "32"):         # 서비스 없음/접근거부/미등록키/기한만료/IP
        raise ApiError(f"인증·권한 오류 {m}", code=raw, fatal=True)
    if c in ("10", "11"):                           # 파라미터 오류 → 재시도 무의미
        raise ApiError(f"요청 파라미터 오류 {m}", code=raw)
    raise ApiError(f"API 오류 {m}", code=raw, retryable=True)   # 01/02/04/05/99 등


def parse_datagokr(resp: requests.Response) -> tuple[list[dict], int]:
    """공공데이터포털 표준 응답(JSON/XML 모두) → (items, totalCount)"""
    text = resp.text.strip().lstrip("﻿")
    if text.startswith("{"):
        data = json.loads(text)
        r = data.get("response", data)
        header = r.get("header") or {}
        ok = _check_datagokr_code(str(header.get("resultCode", "")), str(header.get("resultMsg", "")))
        if not ok:
            return [], 0
        body = r.get("body") or {}
        items = body.get("items")
        if isinstance(items, dict):
            items = items.get("item", [])
        if not items:
            items = []
        if isinstance(items, dict):
            items = [items]
        total = to_int(body.get("totalCount")) or len(items)
        return [dict(i) for i in items if isinstance(i, dict)], total
    if text.startswith("<"):
        root = ET.fromstring(text)
        reason = root.findtext(".//returnReasonCode")      # OpenAPI_ServiceResponse 형식 오류
        if reason:
            _check_datagokr_code(reason, root.findtext(".//returnAuthMsg") or root.findtext(".//errMsg") or "")
            return [], 0
        ok = _check_datagokr_code(root.findtext(".//resultCode") or "", root.findtext(".//resultMsg") or "")
        if not ok:
            return [], 0
        items = [{c.tag: (c.text or "").strip() for c in it} for it in root.iter("item")]
        total = to_int(root.findtext(".//totalCount")) or len(items)
        return items, total
    # "Unexpected errors" / "API not found" 같은 게이트웨이 평문 응답
    if "not found" in text.lower():
        raise ApiError(f"게이트웨이: {text[:100]}", fatal=True)
    if "unauthorized" in text.lower() or "not registered" in text.lower():
        raise ApiError(f"게이트웨이 인증 오류: {text[:100]}", fatal=True)
    raise ApiError(f"알 수 없는 응답: {text[:120]!r}", retryable=True)


def parse_juso(resp: requests.Response) -> list[dict]:
    """도로명주소 검색 API 응답 → juso 리스트"""
    data = resp.json()
    results = data.get("results") or {}
    common = results.get("common") or {}
    code = str(common.get("errorCode", ""))
    msg = str(common.get("errorMessage", ""))
    if code == "0":
        return results.get("juso") or []
    if code in ("E0001", "E0014", "E0015"):          # 승인키 오류 / 승인 만료 등
        raise ApiError(f"주소 API 승인키 오류 [{code}] {msg}", code=code, fatal=True)
    if code.startswith("-999"):                      # 시스템 오류
        raise ApiError(f"주소 API 시스템 오류 [{code}] {msg}", code=code, retryable=True)
    raise ApiError(f"주소 검색어 오류 [{code}] {msg}", code=code)   # 검색어 문제 → 다른 검색어로


def _find_record_list(obj) -> list[dict]:
    """임의 JSON 안에서 '공시가격 레코드 리스트'를 찾아 반환 (브이월드/NSDI 응답 형식 차이 흡수)"""
    if isinstance(obj, list):
        if obj and all(isinstance(x, dict) for x in obj) and any(
            any("pblntfPc" in k or "Prc" in k or "prc" in k for k in x) for x in obj
        ):
            return obj
        for x in obj:
            r = _find_record_list(x)
            if r:
                return r
    elif isinstance(obj, dict):
        for v in obj.values():
            r = _find_record_list(v)
            if r:
                return r
    return []


def parse_price_attr(resp: requests.Response) -> list[dict]:
    """공시가격 속성조회(브이월드/NSDI) 응답 → 레코드 리스트"""
    text = resp.text.strip().lstrip("﻿")
    if text.startswith("{"):
        data = json.loads(text)
        blob = json.dumps(data, ensure_ascii=False)
        if '"status": "ERROR"' in blob or "INCORRECT_KEY" in blob or "INVALID_KEY" in blob:
            raise ApiError(f"공시가격 API 오류: {blob[:200]}", fatal=True)
        # 공공데이터포털 표준 헤더로 오는 경우
        hdr = (data.get("response") or {}).get("header") if isinstance(data.get("response"), dict) else None
        if hdr:
            if not _check_datagokr_code(str(hdr.get("resultCode", "")), str(hdr.get("resultMsg", ""))):
                return []
        return _find_record_list(data)
    if text.startswith("<"):
        root = ET.fromstring(text)
        reason = root.findtext(".//returnReasonCode")
        if reason:
            _check_datagokr_code(reason, root.findtext(".//returnAuthMsg") or "")
            return []
        rows = []
        for tag in ("field", "item"):
            for el in root.iter(tag):
                row = {c.tag: (c.text or "").strip() for c in el}
                if row:
                    rows.append(row)
        return rows
    raise ApiError(f"공시가격 API 알 수 없는 응답: {text[:100]!r}", retryable=True)


# ════════════════════════════════════════════════════════════
# 디스크 캐시 (실거래가 · 주소검색)
# ════════════════════════════════════════════════════════════
class DiskCache:
    """JSON 파일 하나에 키-값 저장. {key: {"t": iso시각, "v": 값}}"""

    def __init__(self, name: str):
        self.path = CACHE_DIR / f"{name}.json"
        self._lock = threading.Lock()
        self.data: dict = read_json(self.path, {}) or {}
        self.dirty = False

    def get(self, key: str, ttl_days: float):
        e = self.data.get(key)
        if e and days_since(e.get("t")) <= ttl_days:
            return e.get("v")
        return None

    def set(self, key: str, value) -> None:
        with self._lock:
            self.data[key] = {"t": now_kst().isoformat(timespec="seconds"), "v": value}
            self.dirty = True

    def prune(self, max_age_days: float) -> None:
        with self._lock:
            old = [k for k, e in self.data.items() if days_since(e.get("t")) > max_age_days]
            for k in old:
                del self.data[k]
            self.dirty = self.dirty or bool(old)

    def save(self) -> None:
        if self.dirty:
            write_json_atomic(self.path, self.data, indent=None)
            self.dirty = False


# ════════════════════════════════════════════════════════════
# 1단계: 경매 목록 수집 (기존 scrape_auctions.py 를 서브프로세스로 실행)
# ════════════════════════════════════════════════════════════
def backup_auctions() -> Path | None:
    """현재 auctions.json 백업 (최근 BACKUP_KEEP 개만 유지)"""
    if not AUCTIONS_PATH.exists():
        return None
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dst = BACKUP_DIR / f"auctions_{now_kst():%Y%m%d_%H%M%S}.json"
    shutil.copy2(AUCTIONS_PATH, dst)
    olds = sorted(BACKUP_DIR.glob("auctions_*.json"))
    for p in olds[:-BACKUP_KEEP]:
        p.unlink(missing_ok=True)
    log(f"  💾 백업: {dst.relative_to(ROOT)}")
    return dst


def run_scraper() -> bool:
    """scrape_auctions.py 실행. 성공 여부 반환 (실패해도 파이프라인은 기존 데이터로 계속)"""
    if not SCRAPER_SCRIPT.exists():
        ERRORS.add("scrape", f"{SCRAPER_SCRIPT} 없음")
        return False
    cmd = [sys.executable, "-u", str(SCRAPER_SCRIPT)]
    log(f"  ▶ {' '.join(cmd)}")
    try:
        proc = subprocess.run(cmd, cwd=str(ROOT), timeout=SCRAPE_TIMEOUT_MIN * 60)
    except subprocess.TimeoutExpired:
        ERRORS.add("scrape", f"스크래퍼 제한시간 {SCRAPE_TIMEOUT_MIN}분 초과")
        return False
    except OSError as e:
        ERRORS.add("scrape", f"스크래퍼 실행 실패: {e}")
        return False
    if proc.returncode != 0:
        ERRORS.add("scrape", f"스크래퍼 종료코드 {proc.returncode} — 기존 데이터로 분석 계속")
        return False
    return True


# ════════════════════════════════════════════════════════════
# 2단계: 주소 → 법정동코드 / PNU
# ════════════════════════════════════════════════════════════
_ROAD_RE = re.compile(r"^(.*?\S+(?:로|길)\s+\d+(?:-\d+)?)(?=[\s,]|$)")
_JIBUN_RE = re.compile(r"^(.*?\S+(?:동|리|가)\d*\s+(?:산\s*)?\d+(?:-\d+)?)(?=[\s,]|$)")
_HO_RE = re.compile(r"제?\s*(지하|비|B)?\s*(\d+)\s*호")
_BLDG_DONG_RE = re.compile(r"(?:^|\s)제?\s*([0-9A-Za-z가-힣]{1,6}?)\s*동(?=\s|제|\d|$)")
_SQL_WORDS = re.compile(r"\b(OR|SELECT|INSERT|DELETE|UPDATE|CREATE|DROP|EXEC|UNION|FETCH|DECLARE|TRUNCATE)\b", re.I)


def parse_address(addr: str) -> dict:
    """
    경매 주소 → 검색용 기본주소 + 건물 동/호 분리
      '서울특별시 강남구 헌릉로590길 63 514동 9층901호 (세곡동,강남데시앙파크)'
        → base='서울특별시 강남구 헌릉로590길 63', dong='514', ho='901', hint='세곡동 강남데시앙파크'
    """
    s = addr or ""
    hint = " ".join(re.findall(r"\(([^)]*)\)", s)).replace(",", " ").strip()
    s = re.sub(r"\([^)]*\)", " ", s)                      # 괄호 제거
    s = re.sub(r"외\s*\d*\s*필지", " ", s)                  # '외 2필지' 제거
    s = re.sub(r"[\[\]%=<>'\";]", " ", s)                  # 주소 API 금지 특수문자
    s = re.sub(r"\s+", " ", s).strip()

    m = _ROAD_RE.match(s) or _JIBUN_RE.match(s)
    base = m.group(1).strip() if m else s
    rest = s[len(base):] if m else s     # 번지 없는 주소(…블록/로트)는 전체에서 동·호 추출

    ho_m = list(_HO_RE.finditer(rest))
    ho = None
    if ho_m:
        g = ho_m[-1]
        ho = ("B" if g.group(1) else "") + g.group(2)
    dong_m = _BLDG_DONG_RE.search(rest) if m else re.search(r"\s([0-9A-Za-z]{1,4})동(?=\s|$)", rest)
    bldg_dong = dong_m.group(1) if dong_m else None
    dash = re.search(r"(?:^|\s)(\d{1,4})-(\d{1,5})\s*호", rest)   # '101-2002호' = 101동 2002호
    if dash:
        bldg_dong = bldg_dong or dash.group(1)
        ho = dash.group(2)
    if not m:   # 기본주소 패턴 실패 → 층/호/건물동 부분을 잘라 검색어로 사용
        base = re.split(r"\s(?:제?\s*\d+\s*층|제?\s*\d+\s*호|지하)", s)[0].strip()
        base = re.sub(r"\s+[0-9A-Za-z]{1,4}동$", "", base)
    return {"base": base, "building_dong": bldg_dong, "unit_ho": ho, "hint": hint}


def _juso_keywords(addr: str, parsed: dict) -> list[str]:
    """검색어 후보 (앞에서부터 시도)"""
    cands = [parsed["base"]]
    if parsed["hint"]:
        # 지번주소 실패 대비: 시군구 + 괄호 안 법정동/건물명
        head = " ".join(parsed["base"].split()[:2])
        cands.append(f"{head} {parsed['hint']}")
    cands.append(re.sub(r"\([^)]*\)", " ", addr))
    out = []
    for k in cands:
        k = _SQL_WORDS.sub(" ", re.sub(r"\s+", " ", k)).strip()
        if len(k) >= 4 and k not in out:
            out.append(k[:80])
    return out


def juso_search(keyword: str, cache: DiskCache) -> dict | None:
    """도로명주소 API로 1건 조회 (90일 캐시)"""
    cached = cache.get(keyword, ttl_days=90)
    if cached is not None:
        return cached or None
    params = {
        "confmKey": JUSO_CONFIRM_KEY, "keyword": keyword,
        "currentPage": 1, "countPerPage": 5, "resultType": "json",
    }
    last = None
    for url in JUSO_URLS:
        key = f"juso:{urlparse(url).hostname}"
        if HTTP.is_blocked(key):
            continue
        try:
            rows = HTTP.get(key, url, params, parse_juso)
            hit = rows[0] if rows else {}
            cache.set(keyword, hit)
            return hit or None
        except EndpointBlocked:
            continue
        except ApiError as e:
            last = e
            if not e.fatal and e.code and e.code.startswith("E"):
                cache.set(keyword, {})       # 검색어 자체 문제 → 캐시하고 다음 후보로
                return None
    if last:
        raise last
    return None


def resolve_address(item: dict, cache: DiskCache) -> bool:
    """item 에 bjdong_code, pnu, road/jibun 주소, 읍면동, 동/호 채움. 성공 여부 반환"""
    parsed = parse_address(item.get("address", ""))
    item["building_dong"] = parsed["building_dong"]
    item["unit_ho"] = parsed["unit_ho"]
    for kw in _juso_keywords(item.get("address", ""), parsed):
        hit = juso_search(kw, cache)
        if not hit:
            continue
        adm = str(hit.get("admCd") or "")
        if len(adm) != 10:
            continue
        mt = "2" if str(hit.get("mtYn", "0")) == "1" else "1"     # PNU 산 구분: 1=일반, 2=산
        bun = str(to_int(hit.get("lnbrMnnm")) or 0).zfill(4)
        ji = str(to_int(hit.get("lnbrSlno")) or 0).zfill(4)
        item["bjdong_code"] = adm
        item["pnu"] = f"{adm}{mt}{bun}{ji}"
        item["road_address"] = hit.get("roadAddrPart1") or hit.get("roadAddr") or None
        item["jibun_address"] = hit.get("jibunAddr") or None
        # 읍면동 (+리) — 실거래가 법정동 비교용
        umd = " ".join(filter(None, [(hit.get("emdNm") or "").strip(), (hit.get("liNm") or "").strip()]))
        item["umd_name"] = umd or None
        vlog(f"{item['id']} 주소 OK '{kw}' → {adm} / PNU {item['pnu']}")
        return True
    ERRORS.add("juso", f"주소 변환 실패: {item.get('address', '')[:60]}", item.get("id"), level="warn")
    return False


def pnu_parts(pnu: str) -> dict:
    """PNU(19자리) → 건축물대장 조회 파라미터"""
    return {
        "sigunguCd": pnu[:5], "bjdongCd": pnu[5:10],
        "platGbCd": "1" if pnu[10] == "2" else "0",
        "bun": pnu[11:15], "ji": pnu[15:19],
    }


def pnu_jibun(pnu: str) -> str:
    """PNU → '520-27' 형태 지번 (실거래가 지번 비교용)"""
    bun, ji = int(pnu[11:15]), int(pnu[15:19])
    return f"{bun}-{ji}" if ji else str(bun)


# ════════════════════════════════════════════════════════════
# 3단계-①: 건축물대장 (건축HUB)
# ════════════════════════════════════════════════════════════
_VIOLATION_KEYS = ("violBldYn", "vlBldYn", "vltnBldYn", "viltBldYn", "illegalYn")


def _bld_call(op: str, params: dict, item_id: str) -> list[dict]:
    """건축물대장 오퍼레이션 호출 (최대 3페이지)"""
    url = f"{BLD_BASE}/{op}"
    out: list[dict] = []
    for page in range(1, 4):
        p = {"serviceKey": DATA_GO_KR_KEY, "_type": "json", "numOfRows": 100, "pageNo": page, **params}
        items, total = HTTP.get(f"bld:{op}", url, p, parse_datagokr)
        out.extend(items)
        if len(out) >= total or not items:
            break
    return out


def _detect_violation(rows: list[dict]) -> bool:
    """위반건축물 표시 탐지 — 전용 필드가 있으면 그것을, 없으면 모든 문자열 값에서 '위반' 검색"""
    for r in rows:
        for k in _VIOLATION_KEYS:
            if str(r.get(k, "")).strip().upper() in ("Y", "1", "TRUE", "위반"):
                return True
        for v in r.values():
            if isinstance(v, str) and "위반건축물" in v.replace(" ", ""):
                return True
    return False


def _use_str(row: dict, with_etc: bool) -> str | None:
    main = (row.get("mainPurpsCdNm") or "").strip()
    etc = (row.get("etcPurps") or "").strip()
    if with_etc and etc and etc != main:
        return f"{main}({etc})" if main else etc
    return main or etc or None


def fetch_building(item: dict) -> None:
    """건축물대장 표제부 + 전유공용면적 → 용도·사용승인일·위반여부·면적"""
    pp = pnu_parts(item["pnu"])
    titles = _bld_call("getBrTitleInfo", pp, item["id"])
    if not titles:
        ERRORS.add("building", "건축물대장 표제부 없음", item["id"], level="warn")
    # 표제부 선택: 건물 동이 있으면 동 일치, 없으면 주건축물 중 연면적 최대
    title = None
    mains = [t for t in titles if "부속" not in str(t.get("mainAtchGbCdNm", ""))] or titles
    if item.get("building_dong"):
        want = digits(item["building_dong"]) or item["building_dong"]
        for t in mains:
            dn = str(t.get("dongNm", ""))
            if want and (digits(dn) == want or want in dn):
                title = t
                break
    if title is None and mains:
        title = max(mains, key=lambda t: to_float(t.get("totArea")) or 0)

    rows_for_violation = list(titles)
    if title:
        item["approval_date"] = fmt_ymd(title.get("useAprDay"))
        item["actual_use"] = _use_str(title, with_etc=False)   # 건물 단위는 주용도만 (기타용도는 층별 혼재)
        item["actual_use_level"] = "동"
        item["building_area"] = to_float(title.get("totArea"))

    # 전유부(호 단위) — 집합건물이고 호수를 알 때만
    if item.get("unit_ho"):
        want_ho = digits(item["unit_ho"])
        want_dong = digits(item.get("building_dong")) if item.get("building_dong") else None
        params = dict(pp)
        params["hoNm"] = f"{item['unit_ho'].lstrip('B')}호"
        if item.get("building_dong"):
            params["dongNm"] = f"{item['building_dong']}동"
        try:
            rows = _bld_call("getBrExposPubuseAreaInfo", params, item["id"])
        except EndpointBlocked:
            raise
        except ApiError as e:
            vlog(f"{item['id']} 전유부 동/호 필터 조회 실패 → 필터 없이 재조회 ({e})")
            rows = []
        if not rows:  # 동/호 필터 형식이 안 맞을 수 있어 필터 없이 1회 더
            try:
                rows = _bld_call("getBrExposPubuseAreaInfo", pp, item["id"])
            except EndpointBlocked:
                raise
            except ApiError as e:
                ERRORS.add("building", f"전유공용면적 조회 실패: {e}", item["id"], level="warn")
                rows = []
        unit = [r for r in rows
                if digits(r.get("hoNm")) == want_ho
                and (want_dong is None or not digits(r.get("dongNm")) or digits(r.get("dongNm")) == want_dong)]
        excl = [r for r in unit if "전유" in str(r.get("exposPubuseGbCdNm", ""))]
        if excl:
            area = sum(to_float(r.get("area")) or 0 for r in excl)
            item["exclusive_area"] = round(area, 2) if area else None
            item["actual_use"] = _use_str(excl[0], with_etc=True)
            item["actual_use_level"] = "호"
        rows_for_violation += unit

    item["is_illegal_building"] = _detect_violation(rows_for_violation)


# ════════════════════════════════════════════════════════════
# 3단계-②: 공시가격 (공동주택가격 / 개별주택가격)
# ════════════════════════════════════════════════════════════
def _pick_price_row(rows: list[dict], item: dict, unit_required: bool) -> tuple[int, int] | None:
    """공시가격 레코드 중 이 물건의 동/호에 해당하는 최신 연도 값 → (가격, 연도)"""
    want_ho = digits(item.get("unit_ho")) if item.get("unit_ho") else None
    want_dong = digits(item.get("building_dong")) if item.get("building_dong") else None
    best = None
    for r in rows:
        price = None
        for k in ("pblntfPc", "hsprc", "hsprcAmt", "housePc"):
            price = to_int(r.get(k))
            if price:
                break
        if not price:
            for k, v in r.items():
                if re.search(r"(?i)prc|pc$", k):
                    price = to_int(v)
                    if price:
                        break
        if not price:
            continue
        ho = digits(r.get("hoNm")) if r.get("hoNm") not in (None, "", "-") else None
        dong = digits(r.get("dongNm")) if r.get("dongNm") not in (None, "", "-") else None
        if unit_required:
            if not want_ho or ho != want_ho:
                continue
            if want_dong and dong and dong != want_dong:
                continue
        year = to_int(str(r.get("stdrYear") or r.get("crtnDay") or r.get("stdDay") or "")[:4])
        if best is None or (year or 0) > best[1]:
            best = (price, year or 0)
    return best


def _price_query(key: str, url: str, base: dict, item: dict, unit_required: bool) -> tuple[int, int] | None:
    """올해 → 작년 순으로 조회 (올해 공시는 4월 말 이후 등록)"""
    this_year = now_kst().year
    for year in (this_year, this_year - 1):
        params = {**base, "pnu": item["pnu"], "stdrYear": str(year), "format": "json",
                  "numOfRows": 1000, "pageNo": 1}
        rows = HTTP.get(key, url, params, parse_price_attr)
        hit = _pick_price_row(rows, item, unit_required)
        if hit:
            return hit
    return None


def fetch_official_price(item: dict) -> None:
    """공시가격 조회 — 여러 출처를 순서대로 시도 (서킷브레이커로 죽은 출처는 자동 건너뜀)"""
    ptype = item.get("property_type", "")
    is_house = any(k in ptype for k in ("단독", "다가구")) and not any(k in ptype for k in ("다세대", "연립", "아파트"))
    is_collective = any(k in ptype for k in ("아파트", "다세대", "연립", "빌라"))
    if not (is_house or is_collective):
        return   # 오피스텔·상가 등은 주택 공시가격 대상 아님 (국세청 기준시가)

    sources: list[tuple[str, str, dict, bool]] = []
    if is_collective:
        if VWORLD_KEY:
            vb = {"key": VWORLD_KEY, **({"domain": VWORLD_DOMAIN} if VWORLD_DOMAIN else {})}
            sources.append(("price:vworld_apt", VWORLD_APT_PRICE_URL, vb, True))
        if DATA_GO_KR_KEY:
            sources.append(("price:nsdi_apt", NSDI_APT_PRICE_URL, {"serviceKey": DATA_GO_KR_KEY}, True))
    else:
        if VWORLD_KEY:
            vb = {"key": VWORLD_KEY, **({"domain": VWORLD_DOMAIN} if VWORLD_DOMAIN else {})}
            sources.append(("price:vworld_indv", VWORLD_INDV_PRICE_URL, vb, False))

    for key, url, base, unit_req in sources:
        if HTTP.is_blocked(key):
            continue
        try:
            hit = _price_query(key, url, base, item, unit_req)
        except EndpointBlocked:
            continue
        except ApiError as e:
            ERRORS.add("official_price", f"{key}: {e}", item["id"], level="warn")
            continue
        if hit:
            item["official_price"], item["official_price_year"] = hit[0], (hit[1] or None)
            return

    # 최후 수단: 건축HUB 건축물대장 주택가격 (주로 단독·다가구 개별주택가격)
    if DATA_GO_KR_KEY and not HTTP.is_blocked("bld:getBrHsprcInfo"):
        try:
            rows = _bld_call("getBrHsprcInfo", pnu_parts(item["pnu"]), item["id"])
            hit = _pick_price_row(rows, item, unit_required=is_collective)
            if hit:
                item["official_price"], item["official_price_year"] = hit[0], (hit[1] or None)
        except EndpointBlocked:
            pass
        except ApiError as e:
            ERRORS.add("official_price", f"건축물대장 주택가격: {e}", item["id"], level="warn")


# ════════════════════════════════════════════════════════════
# 3단계-③: 실거래가 (물건유형별)
# ════════════════════════════════════════════════════════════
def trade_type_of(item: dict) -> str | None:
    """물건유형(+실제용도) → 실거래가 API 유형"""
    pt = item.get("property_type", "") or ""
    use = item.get("actual_use", "") or ""
    if "아파트" in pt:
        return "apt"
    if any(k in pt for k in ("다세대", "연립", "빌라")):
        return "rh"
    if any(k in pt for k in ("단독", "다가구")):
        return "sh"
    if "오피스텔" in pt or "상가" in pt or "근린" in pt:
        if "오피스텔" in use:
            return "offi"
        if any(k in use for k in ("근린생활", "판매", "업무", "숙박")):
            return "nrg"
        return "offi" if "오피스텔" in pt else "nrg"
    return None


def _recent_months(n: int) -> list[str]:
    """최근 n개월 YYYYMM (이번 달 포함)"""
    y, m = now_kst().year, now_kst().month
    out = []
    for _ in range(n):
        out.append(f"{y}{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


def _norm_trade(raw: dict) -> dict | None:
    """실거래 원본 레코드(영문/구 한글 태그 모두) → 공통 형태"""
    def g(*keys):
        for k in keys:
            v = raw.get(k)
            if v not in (None, ""):
                return str(v).strip()
        return ""
    if g("cdealType", "해제여부") in ("O", "Y"):      # 계약 해제 건 제외
        return None
    amt = to_int(g("dealAmount", "거래금액"))
    y, m, d = to_int(g("dealYear", "년")), to_int(g("dealMonth", "월")), to_int(g("dealDay", "일"))
    if not amt or not y or not m:
        return None
    return {
        "amount": amt * 10_000,                                         # 만원 → 원
        "date": f"{y:04d}-{m:02d}-{(d or 1):02d}",
        "umd": g("umdNm", "법정동"),
        "jibun": g("jibun", "지번"),
        "area": to_float(g("excluUseAr", "전용면적", "totalFloorAr", "연면적", "buildingAr", "건물면적")),
        "name": g("aptNm", "offiNm", "mhouseNm", "아파트", "단지", "연립다세대"),
    }


def fetch_trades(ttype: str, lawd: str, ym: str, cache: DiskCache) -> list[dict]:
    """(유형, 시군구, 계약월) 실거래 목록. 최근 2개월은 1일, 그 이전은 14일 캐시"""
    ck = f"{ttype}:{lawd}:{ym}"
    recent = _recent_months(2)
    cached = cache.get(ck, ttl_days=1 if ym in recent else 14)
    if cached is not None:
        return cached
    svc, op = TRADE_APIS[ttype]
    url = f"https://apis.data.go.kr/1613000/{svc}/{op}"
    rows: list[dict] = []
    for page in range(1, 11):
        params = {"serviceKey": DATA_GO_KR_KEY, "LAWD_CD": lawd, "DEAL_YMD": ym,
                  "numOfRows": 1000, "pageNo": page}
        items, total = HTTP.get(f"trade:{ttype}", url, params, parse_datagokr)
        rows.extend(t for t in (_norm_trade(i) for i in items) if t)
        if not items or page * 1000 >= total:
            break
    cache.set(ck, rows)
    return rows


def _norm_jibun(j: str) -> str | None:
    """'0520-0027' / '520-27' → '520-27', 마스킹('5**')은 None"""
    if not j or "*" in j:
        return None
    m = re.match(r"^\s*(?:산\s*)?(\d+)(?:-(\d+))?", j)
    if not m:
        return None
    bun, ji = int(m.group(1)), int(m.group(2) or 0)
    return f"{bun}-{ji}" if ji else str(bun)


def compute_nearby_trade(item: dict, trades: list[dict]) -> None:
    """유사 거래 선정 → nearby_trade_price/count/date/basis"""
    for k in ("nearby_trade_price", "nearby_trade_count", "nearby_trade_date", "nearby_trade_basis"):
        item[k] = None
    umd = item.get("umd_name")
    if not trades or not umd:
        item["nearby_trade_count"] = 0
        return
    my_jibun = pnu_jibun(item["pnu"]) if item.get("pnu") else None
    area = item.get("exclusive_area")
    if not area and trade_type_of(item) in ("sh", "nrg"):
        area = item.get("building_area")

    # 법정동 비교: 마지막 토큰(동 또는 리) 기준 → '봉담읍 수영리' vs '수영리' 표기 차이 흡수
    umd_last = umd.split()[-1]
    same_umd = [t for t in trades if t["umd"] and t["umd"].split()[-1] == umd_last]
    same_bldg = [t for t in same_umd if my_jibun and _norm_jibun(t["jibun"]) == my_jibun]

    def similar(pool):
        return [t for t in pool if area and t["area"] and abs(t["area"] - area) / area <= 0.15]

    chosen, basis, per_area = [], None, False
    if area:
        for pool, label in ((similar(same_bldg), "동일단지·유사면적"),
                            (similar(same_umd), "동일법정동·유사면적"),
                            (same_bldg, "동일단지·㎡단가환산"),
                            (same_umd, "동일법정동·㎡단가환산")):
            pool = [t for t in pool if t["area"]]
            if pool:
                chosen, basis, per_area = pool, label, True
                break
    else:
        for pool, label in ((same_bldg, "동일단지"), (same_umd, "동일법정동(면적미확인)")):
            if pool:
                chosen, basis = pool, label
                break
    if not chosen:
        item["nearby_trade_count"] = 0
        return
    if per_area:
        price = statistics.median(t["amount"] / t["area"] for t in chosen) * area
    else:
        price = statistics.median(t["amount"] for t in chosen)
    item["nearby_trade_price"] = int(round(price, -4))            # 만원 단위 반올림
    item["nearby_trade_count"] = len(chosen)
    item["nearby_trade_date"] = max(t["date"] for t in chosen)
    item["nearby_trade_basis"] = basis


# ════════════════════════════════════════════════════════════
# 4단계: 투자분석
# ════════════════════════════════════════════════════════════
def analyze_investment(item: dict) -> None:
    op = item.get("official_price")
    tp = item.get("nearby_trade_price")
    mb = item.get("min_bid")

    # 안전 전세가: 공시가격 × 126% (HUG 전세보증 가입 기준: 공시가격 140% × 담보인정 90%)
    item["safe_jeonse"] = int(round(op * 1.26, -4)) if op else None
    # 안전마진: (실거래가 - 최저가) / 실거래가 × 100
    item["safety_margin_pct"] = round((tp - mb) / tp * 100, 1) if (tp and mb) else None
    # 공시가격 1억 이하 (취득세 중과 배제 대상 여부 판단용). 공시가격 미확인이면 null
    item["is_under_100m"] = (op <= 100_000_000) if op else None

    tags: list[str] = []
    pt = item.get("property_type", "") or ""
    use = item.get("actual_use", "") or ""
    # 근생주의: 주거용으로 나온 물건인데 대장상 용도가 근린생활시설 (속칭 '근생빌라')
    residential = any(k in pt for k in RESIDENTIAL_KW) and "상가" not in pt
    if residential and "근린생활" in use:
        tags.append("🚨 근생주의")
    if item.get("is_illegal_building"):
        tags.append("🚨 위반건축물")
    sm = item.get("safety_margin_pct")
    if sm is not None and sm >= SAFETY_MARGIN_PCT and (item.get("nearby_trade_count") or 0) >= 2:
        tags.append("✨ 안전마진 확보")
    item["risk_tags"] = tags


# ════════════════════════════════════════════════════════════
# 5단계: 권리분석 (매각물건명세서 키워드 스캔)
# ════════════════════════════════════════════════════════════
# (위험도, 표시 키워드, 정규식)
RIGHTS_RULES = [
    ("위험", "유치권", r"유치권"),
    ("위험", "법정지상권", r"법정\s*지상권"),
    ("위험", "분묘기지권", r"분묘\s*기지권"),
    ("위험", "선순위 가처분", r"(?:선순위|최선순위)[^.\n]{0,10}가처분|가처분[^.\n]{0,10}(?:인수|말소되지)"),
    ("위험", "예고등기", r"예고\s*등기"),
    ("위험", "대지권 미등기", r"대지권\s*(?:미등기|없음|없는)"),
    ("위험", "보증금 인수", r"(?:보증금|임차권|전세권)[^.\n]{0,20}인수|인수[^.\n]{0,10}(?:보증금|임차)"),
    ("위험", "대항력 있는 임차인", r"대항력\s*(?:이\s*)?있는\s*임차인|대항력\s*있음"),
    ("위험", "지분 매각", r"지분\s*매각|공유\s*지분"),
    ("주의", "토지별도등기", r"토지\s*별도\s*등기"),
    ("주의", "위반건축물", r"위반\s*건축물"),
    ("주의", "선순위 전세권", r"(?:선순위|최선순위)\s*전세권"),
    ("주의", "대항력 여지", r"대항력\s*(?:여부|여지)|대항력[^.\n]{0,6}(?:불분명|알\s*수\s*없)"),
    ("주의", "점유관계 미상", r"폐문\s*부재|(?:임차|점유)\s*관계\s*(?:미상|불분명)"),
    ("주의", "HUG 관련 조건", r"주택도시보증공사|HUG"),
    ("주의", "농지취득자격증명", r"농지\s*취득\s*자격\s*증명"),
    ("주의", "제시외 건물", r"제시\s*외\s*(?:건물|건축물)"),
    ("주의", "특별매각조건", r"특별\s*매각\s*조건"),
]
_RIGHTS_COMPILED = [(lv, kw, re.compile(rx)) for lv, kw, rx in RIGHTS_RULES]
# 바로 뒤에 '없음' 류가 오면 해당 없음으로 간주
_NEGATION = re.compile(r"^\s*(?:신고|성립|주장|여지)?\s*(?:여부\s*)?[은는이가도]?\s*"
                       r"(?:없음|없다|없습니다|없는|해당\s*(?:사항)?\s*없음|부존재|불성립|미해당|성립하지\s*않)")
# 매각물건명세서 양식 문구 (모든 명세서에 공통 → 스캔 전에 제거)
_BOILERPLATE = [
    re.compile(r"※[^※\n]*"),                                       # ※ 로 시작하는 안내 주석
    re.compile(r"매각에\s*따라\s*설정된\s*것으로\s*보는\s*지상권의\s*개요"),
    re.compile(r"등기된\s*부동산에\s*관한\s*권리\s*또는\s*가처분으로\s*매각으로\s*그\s*효력이\s*소멸되지\s*아니하는\s*것"),
    re.compile(r"인수되는\s*경우가\s*발생\s*할\s*수\s*있[^.\n]*"),
]


def scan_rights_text(text: str | None) -> tuple[str, list[str]]:
    """명세서 텍스트 → (rights_risk, rights_keywords). 텍스트 없으면 '미확인'"""
    if not text or len(text.strip()) < 20:
        return "미확인", []
    t = text
    for bp in _BOILERPLATE:
        t = bp.sub(" ", t)
    t = re.sub(r"[ \t]+", " ", t)
    found: dict[str, str] = {}
    for level, kw, rx in _RIGHTS_COMPILED:
        for m in rx.finditer(t):
            if _NEGATION.match(t[m.end(): m.end() + 25]):
                continue
            found.setdefault(kw, level)
            break
    if any(lv == "위험" for lv in found.values()):
        risk = "위험"
    elif found:
        risk = "주의"
    else:
        risk = "안전"
    # 위험 키워드 먼저 정렬
    kws = sorted(found, key=lambda k: (found[k] != "위험", k))
    return risk, kws


COURTAUCTION = "https://www.courtauction.go.kr"


def _flatten_strings(obj, out: list[str], limit: int = 400_000) -> None:
    """JSON 안의 모든 문자열 수집 (명세서 텍스트 추출용)"""
    if sum(len(s) for s in out) > limit:
        return
    if isinstance(obj, str):
        if len(obj) >= 2:
            out.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _flatten_strings(v, out, limit)
    elif isinstance(obj, list):
        for v in obj:
            _flatten_strings(v, out, limit)


def _open_case_spec(ctx, page, item: dict, captured: list[str]) -> str | None:
    """
    법원경매정보 사이트에서 사건번호로 검색 → '매각물건명세서' 열기 → 텍스트 반환
    ※ 사이트(WebSquare) 구조가 바뀌면 실패할 수 있음 → 실패 시 None (rights_risk='미확인')
    """
    m = re.match(r"(\d{4})\s*타경\s*(\d+)", item["id"])
    if not m:
        return None
    year, num = m.group(1), m.group(2)
    page.goto(f"{COURTAUCTION}/pgj/index.on", wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(4_000)
    page.get_by_text("경매사건검색").first.click(timeout=15_000)
    page.wait_for_timeout(4_000)

    court_ok = year_ok = num_ok = False
    form_frame = page.main_frame
    for frame in page.frames:
        for sel in frame.locator("select").all():
            try:
                opts = [o.strip() for o in sel.locator("option").all_inner_texts()]
            except Exception:
                continue
            if not court_ok and item.get("court") in opts:
                sel.select_option(label=item["court"])
                court_ok = True
            elif not year_ok and year in opts and all(re.fullmatch(r"\d{4}|전체|선택", o or "선택") for o in opts[:5]):
                sel.select_option(label=year)
                year_ok = True
        for inp in frame.locator("input[type='text']").all():
            try:
                meta = " ".join(filter(None, [inp.get_attribute("title"), inp.get_attribute("id"),
                                              inp.get_attribute("name"), inp.get_attribute("placeholder")]))
            except Exception:
                continue
            if re.search(r"사건|csNo|saNo|SaNo|CsNo", meta) and inp.is_visible():
                inp.fill(num)
                num_ok = True
                form_frame = frame
                break
    if not (court_ok and num_ok):
        raise RuntimeError(f"검색 폼 인식 실패 (법원={court_ok}, 연도={year_ok}, 번호={num_ok})")

    # 검색 버튼: 폼이 있는 프레임 안에서 정확히 '검색' 인 버튼 우선
    btn = form_frame.get_by_role("button", name="검색", exact=True)
    if btn.count() == 0:
        btn = form_frame.locator("input[type='button'][value='검색'], a:text-is('검색')")
    btn.first.click(timeout=10_000)
    page.wait_for_timeout(5_000)
    captured.clear()

    # '매각물건명세서' 클릭 → 팝업 또는 같은 화면에 표시
    texts: list[str] = []
    try:
        with ctx.expect_page(timeout=10_000) as pinfo:
            page.get_by_text("매각물건명세서").first.click(timeout=10_000)
        pop = pinfo.value
        pop.wait_for_load_state("domcontentloaded", timeout=30_000)
        pop.wait_for_timeout(3_000)
        for fr in pop.frames:
            try:
                texts.append(fr.locator("body").inner_text(timeout=5_000))
            except Exception:
                pass
        pop.close()
    except Exception:
        page.wait_for_timeout(4_000)
        for fr in page.frames:
            try:
                texts.append(fr.locator("body").inner_text(timeout=5_000))
            except Exception:
                pass
    for body in captured:
        try:
            strs: list[str] = []
            _flatten_strings(json.loads(body), strs)
            texts.append("\n".join(strs))
        except ValueError:
            pass
    joined = "\n".join(texts)
    # 명세서 화면이 실제로 열렸는지 확인 (다른 화면 텍스트 오탐 방지)
    if not re.search(r"최선순위|점유자|매각물건명세서", joined):
        return None
    return joined


def fetch_rights_texts(targets: list[dict]) -> dict[str, str]:
    """매각물건명세서 텍스트 수집 (Playwright). 연속 3회 실패 시 이번 실행 중단"""
    if not targets:
        return {}
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        ERRORS.add("rights", "playwright 미설치 — 권리분석 생략 (pip install playwright && playwright install chromium)")
        return {}
    results: dict[str, str] = {}
    fails = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = browser.new_context(locale="ko-KR", viewport={"width": 1400, "height": 900},
                                  user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"))
        captured: list[str] = []

        def on_response(resp):
            # 사이트 내부 XHR JSON 응답 캡처 (명세서 데이터가 JSON 으로 오는 경우 대비)
            try:
                if "courtauction.go.kr" in resp.url and resp.request.resource_type in ("xhr", "fetch"):
                    body = resp.text()
                    if body[:1] in "{[" and len(body) < 3_000_000:
                        captured.append(body)
            except Exception:
                pass

        ctx.on("response", on_response)
        for it in targets:
            if fails >= 3:
                ERRORS.add("rights", "매각물건명세서 조회 연속 3회 실패 — 이번 실행 중단 (사이트 구조 변경 가능성)")
                break
            page = ctx.new_page()
            try:
                text = _open_case_spec(ctx, page, it, captured)
                if text:
                    results[it["id"]] = text
                    fails = 0
                    vlog(f"{it['id']} 명세서 {len(text):,}자 수집")
                else:
                    fails += 1
                    ERRORS.add("rights", "명세서 화면 확인 불가", it["id"], level="warn")
            except Exception as e:
                fails += 1
                ERRORS.add("rights", f"명세서 조회 실패: {type(e).__name__}: {str(e)[:200]}", it["id"], level="warn")
            finally:
                try:
                    page.close()
                except Exception:
                    pass
        browser.close()
    return results


# ════════════════════════════════════════════════════════════
# 오케스트레이션
# ════════════════════════════════════════════════════════════
def load_previous() -> tuple[dict, dict]:
    """실행 전 auctions.json 로드 → (원본 dict, id→item)"""
    d = read_json(AUCTIONS_PATH, {}) or {}
    items = d.get("auctions", []) if isinstance(d, dict) else (d if isinstance(d, list) else [])
    return (d if isinstance(d, dict) else {"auctions": items}), {i.get("id"): i for i in items if i.get("id")}


def carry_over_enrichment(items: list[dict], prev_by_id: dict, enrich_cache: DiskCache) -> int:
    """스크래퍼가 기본 필드로 덮어쓴 항목에 이전 분석 결과 복원 (주소가 같을 때만)"""
    n = 0
    for it in items:
        prev = prev_by_id.get(it["id"])
        if not prev or "enriched_at" not in prev:
            prev = (enrich_cache.data.get(it["id"]) or {}).get("v")
        if prev and prev.get("address") == it.get("address") and prev.get("enriched_at"):
            for k in ENRICH_FIELDS:
                if k in prev and k not in ("safe_jeonse", "safety_margin_pct", "is_under_100m", "risk_tags"):
                    it.setdefault(k, prev[k])
            n += 1
    return n


def needs_enrich(it: dict) -> bool:
    """분석 이력이 없거나 TTL 경과 시 재분석 (주소 변환 실패 건도 TTL 후 재시도)"""
    return days_since(it.get("enriched_at")) > ENRICH_TTL_DAYS


def enrich_one(it: dict, juso_cache: DiskCache) -> None:
    """2단계 + 3단계①② (물건 1건)"""
    if not it.get("pnu") or not it.get("bjdong_code"):
        if not JUSO_CONFIRM_KEY:
            return
        try:
            if not resolve_address(it, juso_cache):
                it["enrich_status"] = "no_address"
                it["enriched_at"] = now_kst().isoformat(timespec="seconds")
                return
        except ApiError as e:
            if not isinstance(e, EndpointBlocked):
                ERRORS.add("juso", str(e), it["id"])
            return   # enriched_at 미기록 → 다음 실행에서 재시도
    if not DATA_GO_KR_KEY and not VWORLD_KEY:
        return
    ok = True
    if DATA_GO_KR_KEY:
        try:
            fetch_building(it)
        except EndpointBlocked:
            ok = False
        except ApiError as e:
            ok = False
            ERRORS.add("building", str(e), it["id"])
    try:
        fetch_official_price(it)
    except ApiError as e:
        ERRORS.add("official_price", str(e), it["id"])
    if ok:
        it["enrich_status"] = "ok"
        it["enriched_at"] = now_kst().isoformat(timespec="seconds")


def stage_enrich(items: list[dict], juso_cache: DiskCache) -> None:
    """2단계·3단계①② — 분석이 필요한 물건만 (경매일 임박 순, 최대 MAX_ENRICH_PER_RUN 건)"""
    if not JUSO_CONFIRM_KEY:
        log("  ⚠ JUSO_CONFIRM_KEY 없음 — 주소 변환 생략 (기존 법정동코드가 있는 물건만 진행)")
    if not DATA_GO_KR_KEY:
        log("  ⚠ DATA_GO_KR_KEY 없음 — 공공데이터 API 생략")
    todo = [it for it in items if needs_enrich(it)]
    todo.sort(key=lambda x: x.get("auction_date") or "9999")
    limit = ARGS.limit if (ARGS and ARGS.limit is not None) else MAX_ENRICH_PER_RUN
    skipped = max(0, len(todo) - limit)
    todo = todo[:limit]
    log(f"  분석 대상 {len(todo)}건 (캐시 재사용 {len(items) - len(todo) - skipped}건, 다음 실행으로 이월 {skipped}건)")
    if not todo or (not JUSO_CONFIRM_KEY and not DATA_GO_KR_KEY):
        return
    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(enrich_one, it, juso_cache): it for it in todo}
        for f in as_completed(futs):
            try:
                f.result()
            except Exception as e:   # 예상 못한 오류도 전체 중단 없이 기록
                ERRORS.add("enrich", f"{type(e).__name__}: {e}", futs[f].get("id"))
            done += 1
            if done % 100 == 0 or done == len(todo):
                log(f"  … {done}/{len(todo)}건 처리 (API 호출 {HTTP.stats['calls']:,}회)")


def stage_trades(items: list[dict], trade_cache: DiskCache) -> None:
    """3단계-③ — 모든 물건의 유사 실거래가 재계산 (API 는 (유형,구,월) 단위 캐시)"""
    if not DATA_GO_KR_KEY:
        return
    months = _recent_months(TRADE_MONTHS)
    groups: dict[tuple[str, str], list[dict]] = {}
    for it in items:
        tt = trade_type_of(it)
        if tt and it.get("bjdong_code") and it.get("pnu"):
            groups.setdefault((tt, it["bjdong_code"][:5]), []).append(it)
    jobs = [(tt, lawd, ym) for (tt, lawd) in groups for ym in months]
    log(f"  실거래 조회 단위 {len(jobs)}개 ({len(groups)}개 유형·시군구 × {len(months)}개월)")
    fetched: dict[tuple[str, str, str], list[dict]] = {}

    def job(tt, lawd, ym):
        try:
            return fetch_trades(tt, lawd, ym, trade_cache)
        except EndpointBlocked:
            return []
        except ApiError as e:
            ERRORS.add("trade", f"{TRADE_TYPE_LABEL[tt]} {lawd} {ym}: {e}")
            return []

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(job, *j): j for j in jobs}
        for f in as_completed(futs):
            fetched[futs[f]] = f.result() or []

    for (tt, lawd), its in groups.items():
        pool = [t for ym in months for t in fetched.get((tt, lawd, ym), [])]
        for it in its:
            compute_nearby_trade(it, pool)


def stage_rights(items: list[dict]) -> None:
    """5단계 — 권리분석 대상 선정 → 명세서 수집 → 키워드 스캔"""
    for it in items:
        it.setdefault("rights_risk", "미확인")
        it.setdefault("rights_keywords", [])
    if (ARGS and ARGS.no_rights) or os.environ.get("RIGHTS_FETCH", "1") == "0":
        log("  권리분석 조회 생략 (--no-rights / RIGHTS_FETCH=0)")
        return
    today = now_kst().date().isoformat()
    targets = [it for it in items
               if (it.get("auction_date") or "") >= today
               and days_since(it.get("rights_checked_at")) > RIGHTS_TTL_DAYS]
    targets.sort(key=lambda x: x.get("auction_date") or "9999")
    targets = targets[:RIGHTS_MAX_PER_RUN]
    log(f"  매각물건명세서 조회 대상 {len(targets)}건")
    texts = fetch_rights_texts(targets)
    for it in targets:
        if it["id"] not in texts:
            continue     # 조회 실패 → 기존 결과 유지, 다음 실행에서 재시도
        risk, kws = scan_rights_text(texts[it["id"]])
        it["rights_risk"], it["rights_keywords"] = risk, kws
        it["rights_checked_at"] = now_kst().isoformat(timespec="seconds")
        if "위반건축물" in kws:
            it["is_illegal_building"] = True
    log(f"  명세서 분석 완료 {len(texts)}건")


def finalize_item(it: dict) -> dict:
    """필드 기본값 채우기 + 기존 필드 먼저 오도록 정렬 (프론트 하위 호환)"""
    base_keys = ["id", "court", "address", "property_type", "appraisal", "min_bid",
                 "auction_date", "failed_bids", "bid_ratio", "scraped_date"]
    defaults = {k: None for k in ENRICH_FIELDS}
    defaults.update({"risk_tags": [], "rights_risk": "미확인", "rights_keywords": [],
                     "is_illegal_building": False, "enrich_status": "pending"})
    out = {k: it.get(k) for k in base_keys}
    for k in ENRICH_FIELDS:
        v = it.get(k, defaults[k])
        out[k] = defaults[k] if (v is None and defaults[k] is not None) else v
    for k, v in it.items():          # 스크래퍼가 나중에 추가할 수 있는 필드도 보존
        if k not in out:
            out[k] = v
    return out


def save_outputs(items: list[dict], prev_doc: dict, started: float, scrape_ok: bool) -> bool:
    """6단계 — auctions.json (변경 있을 때만 updated 갱신) + error_log.json"""
    prev_items = prev_doc.get("auctions", []) if isinstance(prev_doc, dict) else []
    # 안전장치: 결과가 비었거나 급감하면 저장 거부 (기존 파일 유지)
    if not items:
        ERRORS.add("save", "결과 0건 — 저장 중단, 기존 파일 유지")
        return False
    if len(prev_items) >= 100 and len(items) < len(prev_items) * 0.3:
        ERRORS.add("save", f"건수 급감 {len(prev_items)} → {len(items)} — 저장 중단, 기존 파일 유지")
        return False

    items = sorted(items, key=lambda x: (x.get("scraped_date") or "", x.get("auction_date") or ""), reverse=True)
    changed = json.dumps(items, sort_keys=True, ensure_ascii=False) != \
        json.dumps(prev_items, sort_keys=True, ensure_ascii=False)
    stamp = now_kst()
    doc = {
        "updated": stamp.date().isoformat() if changed else prev_doc.get("updated", stamp.date().isoformat()),
        "updated_at": stamp.isoformat(timespec="seconds") if changed else prev_doc.get("updated_at"),
        "auctions": items,
    }
    if not doc["updated_at"]:
        doc["updated_at"] = stamp.isoformat(timespec="seconds")
    write_json_atomic(AUCTIONS_PATH, doc)
    log(f"  ✅ {AUCTIONS_PATH.relative_to(ROOT)} 저장 — {len(items)}건 ({'변경 있음' if changed else '변경 없음'})")

    # 요약 통계
    def cnt(pred):
        return sum(1 for i in items if pred(i))
    summary = {
        "total": len(items),
        "scrape_ok": scrape_ok,
        "with_bjdong_code": cnt(lambda i: i.get("bjdong_code")),
        "with_building_info": cnt(lambda i: i.get("approval_date") or i.get("actual_use")),
        "with_official_price": cnt(lambda i: i.get("official_price")),
        "with_trade_price": cnt(lambda i: i.get("nearby_trade_price")),
        "rights_checked": cnt(lambda i: i.get("rights_risk") not in (None, "미확인")),
        "tag_근생주의": cnt(lambda i: "🚨 근생주의" in (i.get("risk_tags") or [])),
        "tag_위반건축물": cnt(lambda i: "🚨 위반건축물" in (i.get("risk_tags") or [])),
        "tag_안전마진": cnt(lambda i: "✨ 안전마진 확보" in (i.get("risk_tags") or [])),
        "pending_enrich": cnt(lambda i: i.get("enrich_status") == "pending"),
        "api_calls": HTTP.stats["calls"],
        "api_retries": HTTP.stats["retries"],
        "api_failures": HTTP.stats["failures"],
        "blocked_endpoints": HTTP._blocked,
        "error_counts": ERRORS.counts,
    }
    write_json_atomic(ERROR_LOG_PATH, {
        "run_at": stamp.isoformat(timespec="seconds"),
        "duration_sec": round(time.time() - started, 1),
        "summary": summary,
        "errors": ERRORS.items,
    })
    log(f"  📝 {ERROR_LOG_PATH.relative_to(ROOT)} — 오류/경고 {len(ERRORS.items)}건")
    log("  요약: " + ", ".join(f"{k}={v}" for k, v in summary.items()
                               if k not in ("blocked_endpoints", "error_counts")))
    return True


def main() -> int:
    global ARGS
    ap = argparse.ArgumentParser(description="경매 데이터 수집·분석 파이프라인")
    ap.add_argument("--skip-scrape", action="store_true", help="1단계(목록 수집) 생략")
    ap.add_argument("--no-rights", action="store_true", help="5단계 명세서 조회 생략")
    ap.add_argument("--limit", type=int, default=None, help="이번 실행 신규 분석 최대 건수")
    ap.add_argument("-v", "--verbose", action="store_true", help="상세 로그")
    ARGS = ap.parse_args()

    started = time.time()
    log("═" * 56)
    log(f" 경매 데이터 파이프라인 시작 — {now_kst():%Y-%m-%d %H:%M} KST")
    log("═" * 56)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    juso_cache = DiskCache("juso")
    trade_cache = DiskCache("trades")
    enrich_cache = DiskCache("enrich")

    # 실행 전 상태 확보 + 백업
    prev_doc, prev_by_id = load_previous()
    backup_path = backup_auctions()

    # ── 1단계 ──
    log("[1단계] 경매 목록 수집 (scrape_auctions.py)")
    scrape_ok = True
    if ARGS.skip_scrape:
        log("  생략 (--skip-scrape)")
    else:
        scrape_ok = run_scraper()
        log(f"  {'✅ 완료' if scrape_ok else '⚠ 실패 — 기존 데이터로 계속'}")

    cur_doc, _ = load_previous()
    items = [dict(i) for i in cur_doc.get("auctions", []) if i.get("id")]
    if not items and backup_path:
        # 스크래퍼가 파일을 비우거나 깨뜨린 경우 → 백업으로 복원 후 그 데이터로 분석 계속
        ERRORS.add("load", "수집 후 auctions.json 이 비었거나 손상 — 백업으로 복원")
        shutil.copy2(backup_path, AUCTIONS_PATH)
        cur_doc, _ = load_previous()
        items = [dict(i) for i in cur_doc.get("auctions", []) if i.get("id")]
    if not items:
        ERRORS.add("load", "auctions.json 에 데이터 없음")
        if backup_path:
            shutil.copy2(backup_path, AUCTIONS_PATH)
        write_json_atomic(ERROR_LOG_PATH, {"run_at": now_kst().isoformat(timespec="seconds"),
                                           "summary": {"total": 0}, "errors": ERRORS.items})
        log("❌ 처리할 데이터가 없습니다.")
        return 1
    restored = carry_over_enrichment(items, prev_by_id, enrich_cache)
    log(f"  물건 {len(items)}건 로드 (이전 분석 결과 복원 {restored}건)")

    # ── 2·3단계 ──
    log("[2·3단계] 주소 변환 · 건축물대장 · 공시가격")
    stage_enrich(items, juso_cache)
    juso_cache.save()
    log("[3단계-③] 실거래가")
    stage_trades(items, trade_cache)
    trade_cache.prune(60)
    trade_cache.save()

    # ── 5단계 (4단계 태그에 위반건축물 반영 위해 먼저 수행) ──
    log("[5단계] 권리분석 (매각물건명세서)")
    try:
        stage_rights(items)
    except Exception as e:
        ERRORS.add("rights", f"권리분석 단계 오류: {type(e).__name__}: {e}")

    # ── 4단계 ──
    log("[4단계] 투자분석")
    for it in items:
        analyze_investment(it)

    # ── 6단계 ──
    log("[6단계] 저장")
    final = [finalize_item(it) for it in items]
    for it in final:   # 분석 결과 캐시 (auctions.json 이 외부에서 덮어써져도 복원 가능)
        if it.get("enriched_at"):
            enrich_cache.set(it["id"], {k: it.get(k) for k in ["address"] + ENRICH_FIELDS})
    enrich_cache.prune(120)
    enrich_cache.save()
    ok = save_outputs(final, prev_doc, started, scrape_ok)
    if not ok and backup_path:
        shutil.copy2(backup_path, AUCTIONS_PATH)
        log("  ↩ 백업으로 복원")
    log(f"완료 — {time.time() - started:.0f}초")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
