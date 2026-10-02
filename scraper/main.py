#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
경매 대시보드 전체 파이프라인 (main.py)
────────────────────────────────────────────────────────────
  1단계  collect_court.py 실행 — 법원경매정보 수도권 16개 법원 · 주거용 물건 수집
         (사건·물건번호, 감정가, 이번 기일 최저가, 유찰 횟수, 특수조건, 물건비고, 면적, 법정동코드·지번)
  2단계  PNU 확정 — 수집된 법정동코드+지번 사용, 없을 때만 주소 변환 (내장 코드표 → 도로명주소 API)
  3단계  공공데이터 API (DATA_GO_KR_KEY)
           ① 건축물대장(건축HUB)  → is_illegal_building, actual_use, approval_date, 면적
           ② 주택 공시가격         → official_price, official_price_year  (브이월드 키 VWORLD_KEY 필요)
           ③ 실거래가(물건유형별)   → nearby_trade_price / count / date / basis / confidence
  4단계  투자분석 (safe_jeonse, safety_margin_pct, is_under_100m, risk_tags)
  5단계  권리분석 (rights.py) — 법원 특수조건·물건비고 + 명세서 요약(상세 API) + 현황조사서(임차인)
  6단계  docs/data/auctions.json 저장 + 백업 + docs/data/error_log.json

설계 포인트
  - 수집기 출력이 "현재 법원에 올라와 있는 물건"의 기준이다. 사라진 물건(취하·변경·매각)은 목록에서 빠진다.
  - 수집기는 기본 정보만 쓰므로, 실행 전에 기존 파일을 읽어 두었다가 같은 물건(id+주소)은 분석 결과를 다시 붙인다.
    → 같은 물건을 매일 다시 조회하지 않아 API 일일 트래픽을 아낀다.
  - API 호출: 재시도 최대 3회(지수 백오프 + 지터), 429/Retry-After 처리, 호스트별 호출 간격 제한,
    엔드포인트별 서킷브레이커(키 오류·트래픽 초과 시 즉시 차단), 접속 불가 호스트는 빠르게 포기.
  - 실거래가는 (유형, 시군구, 계약월) 단위로 디스크 캐시 → 같은 구 물건끼리 공유.
  - 출력은 {"updated", "auctions": [...]} 구조 유지 (index.html 은 d.auctions 를 읽음).

사용법
  python3 scraper/main.py                 # 전체 실행
  python3 scraper/main.py --skip-scrape   # 수집 생략, 기존 auctions.json 으로 분석만
  python3 scraper/main.py --limit 5 -v    # 신규 분석 5건만 (API 키 동작 확인용)
  python3 scraper/main.py --no-rights     # 권리분석 상세 조회(브라우저) 생략
  python3 scraper/filters.py              # 조건에 맞는 매물 리포트 출력

환경변수
  DATA_GO_KR_KEY      공공데이터포털 인증키 (Encoding/Decoding 키 모두 가능)
  JUSO_CONFIRM_KEY    도로명주소 검색 API 승인키 (지번을 못 구한 일부 물건·개편 지역 보정용)
  VWORLD_KEY          (선택) 브이월드 인증키 — 공동주택 공시가격 조회에 필요
  VWORLD_DOMAIN       (선택) 브이월드 키 발급 시 등록한 서비스 URL
  MAX_ENRICH_PER_RUN  1회 실행당 신규 분석 최대 건수 (기본 1000)
  ENRICH_TTL_DAYS     건축물대장·공시가격 재조회 주기 (기본 30일)
  RIGHTS_MAX_PER_RUN  1회 실행당 상세·현황조사서 조회 최대 건수 (기본 200)
  RIGHTS_WINDOW_DAYS  매각기일이 이 일수 안인 물건만 상세 조회 (기본 8)
  RIGHTS_TTL_DAYS     권리분석 재조회 주기 (기본 3일)
  SAFETY_MARGIN_PCT   "✨ 안전마진 확보" 기준 % (기본 20)
  TRADE_MONTHS        실거래가 조회 기간(개월, 기본 6)
  WORKERS             API 병렬 작업 수 (기본 4)
  SCRAPE_TIMEOUT_MIN  수집기 제한 시간(분, 기본 45)
  COLLECT_DAYS_AHEAD  매각기일 조회 범위 (오늘부터 N일, 기본 60)
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from filters import DEFAULT_CRITERIA, derive, filter_items   # 검색·필터 정의 (화면과 동일한 기준)  # noqa: E402
from filters import rights_grade as RT_grade                  # noqa: E402
import rights as RT                                           # 권리분석 (목록 특수조건 + 상세·현황조사서)  # noqa: E402

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
SCRAPER_SCRIPT = SCRAPER_DIR / "collect_court.py"     # 법원경매 수집기


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
RIGHTS_MAX_PER_RUN = _env_int("RIGHTS_MAX_PER_RUN", 200)
RIGHTS_WINDOW_DAYS = _env_int("RIGHTS_WINDOW_DAYS", 8)     # 매각기일이 이 일수 안으로 들어온 물건만 상세 조회 (명세서는 통상 7일 전 작성)
RIGHTS_TTL_DAYS = _env_int("RIGHTS_TTL_DAYS", 3)
SAFETY_MARGIN_PCT = _env_int("SAFETY_MARGIN_PCT", 20)
TRADE_MONTHS = _env_int("TRADE_MONTHS", 6)
WORKERS = max(1, _env_int("WORKERS", 4))
SCRAPE_TIMEOUT_MIN = _env_int("SCRAPE_TIMEOUT_MIN", 45)

MAX_RETRIES = 3            # 재시도 최대 3회 (최초 1회 + 재시도 3회)
BACKOFF_BASE = 1.0         # 지수 백오프 기본 대기(초): 1 → 2 → 4
HTTP_TIMEOUT = 20          # 응답 대기 타임아웃(초)
CONNECT_TIMEOUT = 7        # 연결 타임아웃(초) — 해외 IP 차단 시 오래 기다리지 않도록
BREAKER_THRESHOLD = 5      # 엔드포인트 연속 실패 N회 → 이번 실행 동안 차단
HOST_DEAD_THRESHOLD = 3    # 호스트 연결 실패 N회 연속 → 이번 실행 동안 해당 호스트 포기
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
    "is_illegal_building", "actual_use", "actual_use_level", "approval_date", "building_status", "building_lot_note", "bjdong_code_alt",
    "exclusive_area", "building_area",
    "official_price", "official_price_year",
    "nearby_trade_price", "nearby_trade_count", "nearby_trade_date", "nearby_trade_basis", "nearby_trade_confidence",
    "safe_jeonse", "safety_margin_pct", "is_under_100m", "risk_tags",
    "rights_risk", "rights_keywords", "rights_checked_at", "rights_has_spec", "rights_basis", "bid_history",
    "enriched_at", "enrich_status", "enriched_pnu",
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


def strip_empty(item: dict) -> dict:
    """값이 없는 필드(None·빈 문자열·빈 목록·False) 제거. 숫자 0 은 유지"""
    return {k: v for k, v in item.items()
            if not (v is None or v is False or (isinstance(v, (str, list, dict)) and len(v) == 0))}


def write_auctions(path: Path, doc: dict) -> None:
    """auctions.json 저장 — 물건 1건을 한 줄로 (파일 크기와 git 변경분을 줄임)"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    head = {k: v for k, v in doc.items() if k != "auctions"}
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(json.dumps(head, ensure_ascii=False, separators=(",", ":"))[:-1])
        f.write(',\n"auctions":[\n')
        # 값이 없는 필드(null·빈 목록·false)는 생략 — 읽는 쪽은 "없으면 미확인/아님"으로 처리
        f.write(",\n".join(json.dumps(strip_empty(i), ensure_ascii=False, separators=(",", ":")) for i in doc["auctions"]))
        f.write("\n]}\n")
    os.replace(tmp, path)


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


_SECRET_PARAM_RE = re.compile(r"(?i)\b(confmKey|serviceKey|key)=[^&\s'\")]+")


def scrub(msg) -> str:
    """오류 메시지에서 인증키 제거 — error_log.json 은 공개 저장소에 커밋되므로 필수"""
    out = _SECRET_PARAM_RE.sub(lambda m: m.group(1) + "=***", str(msg))
    for secret in (DATA_GO_KR_KEY, JUSO_CONFIRM_KEY, VWORLD_KEY):
        if secret and len(secret) >= 8:
            out = out.replace(secret, "***")
    return out


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
                    "message": scrub(message)[:500],
                })
        if level == "error" and (ARGS and ARGS.verbose):
            log(f"  ! [{stage}] {item_id or ''} {message}")


ERRORS = ErrorCollector()
ARGS: argparse.Namespace | None = None
AUDIT = {"checked": 0, "mismatched": 0}   # 목록 값과 상세 값 대조 결과 (이번 실행)
LAST_SCRAPE_OK_DATE: str | None = None    # 목록 수집이 마지막으로 성공한 날짜 (하루 여러 번 실행 시 중복 수집 방지)


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
        self._host_fail: dict[str, int] = {}     # 호스트별 연속 연결 실패 횟수
        self.dead_hosts: dict[str, str] = {}     # 이번 실행에서 접속 불가로 판정된 호스트
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
        reason = scrub(reason)[:300]
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

    def _mark_host_failure(self, host: str, reason: str) -> bool:
        """연결 실패 기록. 연속 HOST_DEAD_THRESHOLD 회면 이번 실행 동안 해당 호스트 포기 → True"""
        with self._breaker_lock:
            if host in self.dead_hosts:
                return True
            self._host_fail[host] = self._host_fail.get(host, 0) + 1
            if self._host_fail[host] < HOST_DEAD_THRESHOLD:
                return False
            self.dead_hosts[host] = f"연결 실패 {self._host_fail[host]}회 연속 ({reason})"
        log(f"  ⛔ [{host}] 접속 불가 — 이번 실행에서는 건너뜀 (다음 실행에서 자동 재시도)")
        ERRORS.add("api", f"호스트 접속 불가 [{host}]: {reason} — 실행 서버 IP 에서 연결이 안 됨", level="error")
        return True

    # ── GET + 재시도 ────────────────────────────────────
    def get(self, key: str, url: str, params: dict, parser):
        """parser(resp) 로 파싱한 결과 반환. 실패 시 ApiError"""
        if self.is_blocked(key):
            raise EndpointBlocked(f"차단된 엔드포인트: {key}")
        host = urlparse(url).hostname or ""
        if host in self.dead_hosts:
            raise EndpointBlocked(f"접속 불가 호스트: {host}")
        last_err: ApiError | None = None
        for attempt in range(MAX_RETRIES + 1):
            self._throttle(url)
            self.stats["calls"] += 1
            try:
                resp = self.session.get(url, params=params, timeout=(CONNECT_TIMEOUT, HTTP_TIMEOUT))
            except requests.RequestException as e:
                # 예외 원문에는 인증키가 포함된 URL 이 들어 있으므로 종류와 호스트만 기록
                last_err = ApiError(f"네트워크 오류: {type(e).__name__} ({host})", retryable=True)
                if isinstance(e, (requests.ConnectTimeout, requests.ConnectionError)):
                    # 연결 자체가 안 되는 경우(해외 IP 차단 등) — 호스트 단위로 빠르게 포기
                    if self._mark_host_failure(host, type(e).__name__):
                        raise EndpointBlocked(f"접속 불가 호스트: {host}")
            else:
                with self._breaker_lock:
                    self._host_fail[host] = 0
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
# 1단계: 경매 목록 수집 (collect_court.py 를 서브프로세스로 실행)
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
    """수집기 실행. 성공 여부 반환 (실패해도 파이프라인은 기존 데이터로 계속)"""
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


_BJDONG_TABLE: dict[str, str] | None = None
_LOCAL_JIBUN_RE = re.compile(r"^(산\s*)?(\d+)(?:-(\d+))?$")


def bjdong_table() -> dict[str, str]:
    """scraper/bjdong_codes.json 로드 — {'서울특별시 관악구 신림동': '1162010200', ...}"""
    global _BJDONG_TABLE
    if _BJDONG_TABLE is None:
        _BJDONG_TABLE = (read_json(SCRAPER_DIR / "bjdong_codes.json", {}) or {}).get("codes", {})
    return _BJDONG_TABLE


def resolve_local(item: dict, parsed: dict) -> bool:
    """
    지번 주소('시도 시군구 읍면동(리) 번지')를 내장 코드표로 변환 — 네트워크·인증키 불필요.
    도로명 주소는 번지를 알 수 없으므로 False (→ 주소 API 로 넘어감)
    """
    table = bjdong_table()
    tokens = parsed["base"].split()
    # 뒤에서부터 번지 토큰 찾기: '... 신림동 520-27' / '... 수영리 산 12-3'
    for n in range(min(len(tokens) - 1, 6), 1, -1):
        name = " ".join(tokens[:n])
        code = table.get(name)
        if not code:
            continue
        m = _LOCAL_JIBUN_RE.match("".join(tokens[n:n + 2]) if tokens[n:n + 1] == ["산"] else (tokens[n] if len(tokens) > n else ""))
        if not m:
            return False
        bun, ji = m.group(2).zfill(4), (m.group(3) or "0").zfill(4)
        if len(bun) > 4 or len(ji) > 4:
            return False
        item["bjdong_code"] = code
        item["pnu"] = f"{code}{'2' if m.group(1) else '1'}{bun}{ji}"
        item["road_address"] = None
        item["jibun_address"] = parsed["base"]
        # 읍면동(+리): '봉담읍 수영리' / '신림동'
        item["umd_name"] = " ".join(tokens[n - 2:n]) if (tokens[n - 1].endswith("리") and tokens[n - 2][-1] in "읍면") else tokens[n - 1]
        return True
    return False


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
    tried = False
    for url in JUSO_URLS:
        key = f"juso:{urlparse(url).hostname}"
        if HTTP.is_blocked(key):
            continue
        tried = True
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
    if not tried:   # 모든 주소 API 가 차단된 상태 — '주소 없음'이 아니라 '조회 불가'
        raise EndpointBlocked("주소 API 사용 불가")
    return None


def resolve_address(item: dict, cache: DiskCache) -> bool:
    """item 에 bjdong_code, pnu, road/jibun 주소, 읍면동, 동/호 채움. 성공 여부 반환"""
    parsed = parse_address(item.get("address", ""))
    item["building_dong"] = parsed["building_dong"]
    item["unit_ho"] = parsed["unit_ho"]
    # ① 지번 주소는 내장 코드표로 즉시 변환 (전체의 약 90%)
    if resolve_local(item, parsed):
        vlog(f"{item['id']} 주소 OK (내장 코드표) → PNU {item['pnu']}")
        return True
    # ② 도로명 주소 등은 주소 API 로 조회
    if not JUSO_CONFIRM_KEY:
        raise EndpointBlocked("JUSO_CONFIRM_KEY 없음")
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


def _norm_name(s) -> str:
    """건물명 비교용: 공백·괄호·'아파트' 등 제거"""
    return re.sub(r"[\s()\[\],.·\-]|아파트|오피스텔|빌라|맨션", "", str(s or "")).lower()


def _find_titles(item: dict, juso_cache: "DiskCache | None") -> tuple[list[dict], dict]:
    """
    표제부 조회 → (표제부 목록, 실제로 맞은 조회 파라미터)
      1) PNU 그대로
      2) 같은 본번 전체에서 건물명이 같은 대장 찾기 (법원의 대표지번과 건축물대장 지번이 다른 경우)
      3) 주소 API 로 현재 법정동코드를 다시 구해 재조회 (행정구역 개편 지역)
    """
    pp = pnu_parts(item["pnu"])
    titles = _bld_call("getBrTitleInfo", pp, item["id"])
    if titles:
        return titles, pp
    want = _norm_name(item.get("building_name"))
    if want and len(want) >= 2:
        wide = {k: v for k, v in pp.items() if k != "ji"}
        rows = _bld_call("getBrTitleInfo", wide, item["id"])
        hit = [r for r in rows if _norm_name(r.get("bldNm")) and (_norm_name(r.get("bldNm")) == want
                                                                   or want in _norm_name(r.get("bldNm"))
                                                                   or _norm_name(r.get("bldNm")) in want)]
        if hit:
            pp2 = {**pp, "ji": str(hit[0].get("ji") or "0000").zfill(4)}
            same = [r for r in hit if str(r.get("ji") or "").zfill(4) == pp2["ji"]]
            item["building_lot_note"] = f"건축물대장 지번 {int(pp2['bun'])}-{int(pp2['ji'])} (건물명 일치)"
            return same, pp2
    if JUSO_CONFIRM_KEY and juso_cache is not None and not item.get("_juso_tried"):
        item["_juso_tried"] = True
        try:
            hit = juso_search(item.get("jibun_address") or item.get("address", ""), juso_cache)
        except ApiError:
            hit = None
        adm = str((hit or {}).get("admCd") or "")
        if len(adm) == 10 and adm != item["pnu"][:10]:
            new_pnu = adm + item["pnu"][10:]
            pp3 = pnu_parts(new_pnu)
            titles = _bld_call("getBrTitleInfo", pp3, item["id"])
            if titles:
                item["bjdong_code_alt"] = item["pnu"][:10]     # 개편 전 코드 (실거래가 과거 월 조회용)
                item["pnu"], item["bjdong_code"] = new_pnu, adm
                return titles, pp3
    return [], pp


def fetch_building(item: dict, juso_cache: "DiskCache | None" = None) -> None:
    """건축물대장 표제부 + 전유공용면적 → 용도·사용승인일·위반여부·면적"""
    titles, pp = _find_titles(item, juso_cache)
    item.pop("_juso_tried", None)
    if not titles:
        item["building_status"] = "not_found"       # 대장을 찾지 못함 (지번 불일치·미등재 등)
        ERRORS.add("building", "건축물대장 표제부 없음", item["id"], level="warn")
    else:
        item["building_status"] = "ok"
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
    if titles and item.get("unit_ho"):
        want_ho = digits(item["unit_ho"])
        want_dong = digits(item.get("building_dong")) if item.get("building_dong") else None
        try:   # hoNm 은 '호' 없이 숫자만 넣어야 필터가 동작한다 (예: 301)
            rows = _bld_call("getBrExposPubuseAreaInfo", {**pp, "hoNm": item["unit_ho"].lstrip("B")}, item["id"])
        except EndpointBlocked:
            raise
        except ApiError as e:
            vlog(f"{item['id']} 전유부 호 필터 조회 실패 → 필터 없이 재조회 ({e})")
            rows = []
        if not rows:  # 호 표기가 다를 수 있어 필터 없이 1회 더
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
        # 지하 호수(B01)와 지상 호수(01) 구분
        if item["unit_ho"].startswith("B"):
            unit = [r for r in unit if "지하" in str(r.get("flrGbCdNm", ""))] or unit
        else:
            unit = [r for r in unit if "지하" not in str(r.get("flrGbCdNm", ""))] or unit
        excl = [r for r in unit if "전유" in str(r.get("exposPubuseGbCdNm", ""))]
        if excl:
            area = sum(to_float(r.get("area")) or 0 for r in excl)
            item["exclusive_area"] = round(area, 2) if area else item.get("exclusive_area")
            item["actual_use"] = _use_str(excl[0], with_etc=True)
            item["actual_use_level"] = "호"
        rows_for_violation += unit

    item["is_illegal_building"] = _detect_violation(rows_for_violation) or bool(item.get("is_illegal_building"))


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
        # (공공데이터포털 경유 NSDI 공동주택가격 API 는 폐지되어 HTTP 400 만 반환 → 사용하지 않음)
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
        "year": to_int(g("buildYear", "건축년도")),
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
    """
    유사 거래 선정 → nearby_trade_price / count / date / basis / confidence
      비교 순서: 같은 단지(같은 지번)·유사 면적 → 같은 법정동·유사 면적 → ㎡단가 환산
      같은 법정동 비교는 건축연도가 ±5년인 거래가 3건 이상이면 그것만 사용 (신축·구축 가격 차 반영)
      confidence(신뢰도): high = 같은 단지·유사 면적 / medium = 같은 법정동·유사 면적·유사 연식 3건 이상 / low = 그 외
    """
    for k in ("nearby_trade_price", "nearby_trade_count", "nearby_trade_date", "nearby_trade_basis", "nearby_trade_confidence"):
        item[k] = None
    umd = item.get("umd_name")
    if not trades or not umd:
        item["nearby_trade_count"] = 0
        return
    my_jibun = pnu_jibun(item["pnu"]) if item.get("pnu") else None
    ttype = trade_type_of(item)
    area = item.get("exclusive_area")
    if not area and ttype in ("sh", "nrg"):
        area = item.get("building_area")
    year = to_int(str(item.get("approval_date") or "")[:4])

    # 법정동 비교: 마지막 토큰(동 또는 리) 기준 → '봉담읍 수영리' vs '수영리' 표기 차이 흡수
    umd_last = umd.split()[-1]
    same_umd = [t for t in trades if t["umd"] and t["umd"].split()[-1] == umd_last]
    same_bldg = [t for t in same_umd if my_jibun and _norm_jibun(t["jibun"]) == my_jibun]

    def similar(pool):
        return [t for t in pool if area and t["area"] and abs(t["area"] - area) / area <= 0.15]

    def same_age(pool):
        """건축연도 ±5년 거래가 3건 이상이면 그것만 사용 → (pool, 적용 여부)"""
        if not year:
            return pool, False
        sub = [t for t in pool if t.get("year") and abs(t["year"] - year) <= 5]
        return (sub, True) if len(sub) >= 3 else (pool, False)

    chosen, basis, per_area, aged = [], None, False, False
    if area:
        for pool, label, same in ((similar(same_bldg), "동일단지·유사면적", True),
                                  (similar(same_umd), "동일법정동·유사면적", False),
                                  (same_bldg, "동일단지·㎡단가환산", True),
                                  (same_umd, "동일법정동·㎡단가환산", False)):
            pool = [t for t in pool if t["area"]]
            if pool:
                if not same:
                    pool, aged = same_age(pool)
                chosen, basis, per_area = pool, label + ("·유사연식" if aged else ""), True
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
    if basis.startswith("동일단지·유사면적"):
        conf = "high"
    elif basis.startswith("동일법정동·유사면적") and aged and ttype != "apt":
        conf = "medium"          # 아파트는 단지별 가격 차가 커서 다른 단지 비교는 신뢰도 낮음
    else:
        conf = "low"
    item["nearby_trade_confidence"] = conf


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
    # 안전마진 확보: 마진이 기준 이상이고, 비교 거래의 신뢰도가 보통 이상이며, 권리상 위험이 없을 때만 붙인다.
    #   유찰이 여러 번 된 물건은 대개 '낙찰자가 인수하는 보증금·권리'가 있어 최저가만 낮은 것이므로,
    #   권리 등급이 안전/대항력 포기로 확인됐거나, 미확인이라도 유찰 2회 이하일 때만 인정한다.
    sm = item.get("safety_margin_pct")
    grade = RT_grade(item.get("rights_risk"), item.get("rights_keywords"))
    rights_ok = grade in ("safe", "waiver") or (grade == "unknown" and (item.get("failed_bids") or 0) <= 2)
    if (sm is not None and sm >= SAFETY_MARGIN_PCT and rights_ok
            and item.get("nearby_trade_confidence") in ("high", "medium")):
        tags.append("✨ 안전마진 확보")
    item["risk_tags"] = tags


# ════════════════════════════════════════════════════════════
# 5단계: 권리분석 (매각물건명세서 키워드 스캔)
# ════════════════════════════════════════════════════════════
# 규칙과 판정 로직은 rights.py 에 있다. (목록 특수조건·물건비고 + 상세 API + 현황조사서 API)
scan_rights_text = RT.scan_rights_text      # 이전 버전 호환


# ════════════════════════════════════════════════════════════
# 오케스트레이션
# ════════════════════════════════════════════════════════════
def load_previous() -> tuple[dict, dict]:
    """실행 전 auctions.json 로드 → (원본 dict, id→item)"""
    d = read_json(AUCTIONS_PATH, {}) or {}
    items = d.get("auctions", []) if isinstance(d, dict) else (d if isinstance(d, list) else [])
    return (d if isinstance(d, dict) else {"auctions": items}), {i.get("id"): i for i in items if i.get("id")}


RIGHTS_FIELDS = ("rights_risk", "rights_keywords", "rights_checked_at", "rights_has_spec", "rights_basis", "bid_history")
COMPUTED_FIELDS = ("safe_jeonse", "safety_margin_pct", "is_under_100m", "risk_tags")


def carry_over_enrichment(items: list[dict], prev_by_id: dict, enrich_cache: DiskCache) -> int:
    """
    수집기가 새로 쓴 물건에 이전 분석 결과 복원
      · 권리분석 결과: 주소가 같으면 복원
      · 건축물대장·공시가격·실거래가: 분석 당시의 PNU(enriched_pnu)가 지금 수집된 PNU 와 같을 때만 복원
        (PNU 가 바뀌었으면 다른 필지를 조회했던 것이므로 버리고 다시 분석)
    """
    n = 0
    for it in items:
        prev = prev_by_id.get(it["id"])
        if not prev or not (prev.get("enriched_at") or prev.get("rights_checked_at")):
            prev = (enrich_cache.data.get(it["id"]) or {}).get("v")
        if not prev or prev.get("address") != it.get("address"):
            continue
        for k in RIGHTS_FIELDS:
            if k in prev:
                it.setdefault(k, prev[k])
        if prev.get("enriched_at") and prev.get("enriched_pnu") == it.get("pnu"):
            for k in ENRICH_FIELDS:
                if k in prev and k not in COMPUTED_FIELDS and k not in RIGHTS_FIELDS:
                    if k in ("pnu", "bjdong_code", "bjdong_code_alt"):
                        if prev.get(k):
                            it[k] = prev[k]          # 주소 API 로 보정된 코드 유지
                    else:
                        it.setdefault(k, prev[k])
        if prev.get("enriched_at") or prev.get("rights_checked_at"):
            n += 1
    return n


def needs_enrich(it: dict) -> bool:
    """분석 이력이 없거나 TTL 경과 시 재분석. 주소 변환 실패 건은 매번 재시도 (검색 결과는 캐시돼 호출 부담 없음)"""
    if it.get("enrich_status") != "ok":
        return True
    if it.get("building_status") == "not_found":
        return days_since(it.get("enriched_at")) > 7      # 대장을 못 찾은 물건은 1주 뒤 다시 시도
    return days_since(it.get("enriched_at")) > ENRICH_TTL_DAYS


def enrich_one(it: dict, juso_cache: DiskCache) -> None:
    """2단계 + 3단계①② (물건 1건)"""
    it["enriched_pnu"] = it.get("pnu")       # 수집 시점 PNU (다음 실행에서 복원 여부 판단용)
    # 건물 동·호 (건축물대장 전유부·공시가격 조회용) — 법원이 준 PNU 를 쓰는 경우에도 필요
    if it.get("unit_ho") is None and it.get("building_dong") is None:
        parsed = parse_address(" ".join(x for x in [it.get("jibun_address") or it.get("address", ""), it.get("unit") or ""] if x))
        it["building_dong"], it["unit_ho"] = parsed["building_dong"], parsed["unit_ho"]
    # 법원 목록에 법정동코드·지번이 있으면 그대로 사용, 없을 때만 주소 변환
    if not it.get("pnu") or not it.get("bjdong_code"):
        try:
            if not resolve_address(it, juso_cache):
                it["enrich_status"] = "no_address"
                it["enriched_at"] = now_kst().isoformat(timespec="seconds")
                return
        except ApiError as e:
            if not isinstance(e, EndpointBlocked):
                ERRORS.add("juso", str(e), it["id"])
            return   # enriched_at 미기록 → 다음 실행에서 재시도
    if it.get("enrich_status") != "ok":
        it["enrich_status"] = "pending"      # 주소는 확보, 공공데이터 조회 대기
    if not DATA_GO_KR_KEY and not VWORLD_KEY:
        return
    ok = True
    if DATA_GO_KR_KEY:
        try:
            fetch_building(it, juso_cache)
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
        log("  ⚠ JUSO_CONFIRM_KEY 없음 — 지번 주소만 변환 (도로명 주소 물건은 대기)")
    if not DATA_GO_KR_KEY:
        log("  ⚠ DATA_GO_KR_KEY 없음 — 공공데이터 API 생략")
    todo = [it for it in items if needs_enrich(it)]
    todo.sort(key=lambda x: x.get("auction_date") or "9999")
    limit = ARGS.limit if (ARGS and ARGS.limit is not None) else MAX_ENRICH_PER_RUN
    skipped = max(0, len(todo) - limit)
    todo = todo[:limit]
    log(f"  분석 대상 {len(todo)}건 (캐시 재사용 {len(items) - len(todo) - skipped}건, 다음 실행으로 이월 {skipped}건)")
    if not todo:
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
    lawds: dict[tuple[str, str], set[str]] = {}       # 그룹별 조회할 시군구 코드 (행정구역 개편 지역은 옛 코드 포함)
    for it in items:
        tt = trade_type_of(it)
        if tt and it.get("bjdong_code") and it.get("pnu"):
            key = (tt, it["bjdong_code"][:5])
            groups.setdefault(key, []).append(it)
            lawds.setdefault(key, {key[1]})
            if it.get("bjdong_code_alt"):
                lawds[key].add(it["bjdong_code_alt"][:5])
    jobs = sorted({(tt, lawd, ym) for (tt, _), codes in lawds.items() for lawd in codes for ym in months})
    log(f"  실거래 조회 단위 {len(jobs)}개 ({len(groups)}개 유형·시군구 × {len(months)}개월)")
    fetched: dict[tuple[str, str, str], list[dict] | None] = {}

    def job(tt, lawd, ym):
        try:
            return fetch_trades(tt, lawd, ym, trade_cache)
        except EndpointBlocked:
            return None
        except ApiError as e:
            ERRORS.add("trade", f"{TRADE_TYPE_LABEL[tt]} {lawd} {ym}: {e}")
            return None

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(job, *j): j for j in jobs}
        for f in as_completed(futs):
            fetched[futs[f]] = f.result()

    skipped = 0
    for key, its in groups.items():
        tt = key[0]
        parts = [fetched.get((tt, lawd, ym)) for lawd in lawds[key] for ym in months]
        if sum(1 for p in parts if p is not None) < len(months) / 2:
            skipped += len(its)       # 절반 이상 조회 실패 → 이전 값을 그대로 둠 (불완전한 자료로 덮어쓰지 않음)
            continue
        seen, pool = set(), []
        for p in parts:
            for t in p or []:
                k = (t["date"], t["umd"], t["jibun"], t["area"], t["amount"])
                if k not in seen:
                    seen.add(k)
                    pool.append(t)
        for it in its:
            compute_nearby_trade(it, pool)
    if skipped:
        log(f"  실거래 조회 실패로 기존 값 유지 {skipped}건")


def apply_court_defaults(it: dict) -> None:
    """법원 목록 정보로 채울 수 있는 값 미리 채우기 (공공 API 조회 전에도 비교·필터가 되도록)"""
    kinds = it.get("lot_kinds") or []
    area = it.get("court_area")
    if area:
        if "집합건물" in kinds and not it.get("exclusive_area"):
            it["exclusive_area"] = area              # 전유부분 면적
        elif "집합건물" not in kinds and not it.get("building_area"):
            it["building_area"] = area               # 단독·다가구 건물 면적
    if it.get("remarks") and re.search(r"위반\s*건축물", it["remarks"]):
        it["is_illegal_building"] = True             # 법원 물건비고에 위반건축물 기재


def stage_rights(items: list[dict]) -> None:
    """5단계 — 권리분석: ① 모든 물건 목록 정보 판정 ② 매각기일 임박 물건은 상세·현황조사서 조회"""
    # ① 목록 정보(특수조건·물건비고)만으로 1차 판정 — 상세 확인 결과가 있으면 그 근거를 유지
    for it in items:
        r = RT.analyze(it)
        if not it.get("rights_checked_at"):
            it.update(r)
        else:   # 이전에 상세 확인한 물건: 목록 키워드만 합치고 판정은 더 나쁜 쪽으로
            kws = list(dict.fromkeys((it.get("rights_keywords") or []) + r["rights_keywords"]))
            it["rights_keywords"] = kws
            order = {"위험": 3, "주의": 2, "안전": 1, "미확인": 0}
            if order.get(r["rights_risk"], 0) >= 2 and order.get(r["rights_risk"], 0) > order.get(it.get("rights_risk"), 0):
                it["rights_risk"] = r["rights_risk"]
    if (ARGS and ARGS.no_rights) or os.environ.get("RIGHTS_FETCH", "1") == "0":
        log("  상세 조회 생략 (--no-rights / RIGHTS_FETCH=0)")
        return
    # ② 상세 조회 대상: 매각기일이 가까운 물건 중 아직 확인 못 했거나 명세서가 없던 물건
    today = now_kst().date()
    limit_day = (today + timedelta(days=RIGHTS_WINDOW_DAYS)).isoformat()

    def due(it: dict) -> bool:
        if not it.get("case_no") or not it.get("court_code"):
            return False
        ad = it.get("auction_date") or ""
        if not (today.isoformat() <= ad <= limit_day):
            return False
        age = days_since(it.get("rights_checked_at"))
        if not it.get("rights_has_spec"):
            return age > 0.9                 # 명세서가 아직 없던 물건은 하루 뒤 재확인
        return age > RIGHTS_TTL_DAYS
    targets = sorted((it for it in items if due(it)), key=lambda x: (x.get("auction_date") or "9999", x.get("rights_has_spec") is True))
    waiting = len(targets)
    targets = targets[:RIGHTS_MAX_PER_RUN]
    log(f"  상세·현황조사서 조회 {len(targets)}건 (대상 {waiting}건 중, 매각기일 {RIGHTS_WINDOW_DAYS}일 이내)")
    got = RT.fetch_court_details(targets, log=log)
    stamp = now_kst().isoformat(timespec="seconds")
    mismatches = []
    for it in targets:
        g = got.get(it["id"])
        if not g:
            continue        # 조회 실패 → 다음 실행에서 재시도
        # 자체 검증: 목록에서 수집한 값이 물건 상세 화면의 값과 같은지 대조
        info = g["detail"].get("dspslGdsDxdyInfo") or {}
        checks = (("감정가", it.get("appraisal"), to_int(info.get("aeeEvlAmt"))),
                  ("최저가", it.get("min_bid"), to_int(info.get("fstPbancLwsDspslPrc"))),
                  ("유찰횟수", it.get("failed_bids"), to_int(info.get("flbdNcnt"))),
                  ("매각기일", it.get("auction_date"), fmt_ymd(info.get("dspslDxdyYmd"))))
        bad = [f"{name} 목록 {a} ≠ 상세 {b}" for name, a, b in checks if b is not None and a != b]
        if bad:
            mismatches.append(f"{it.get('case_no')}({it.get('item_no')}): " + ", ".join(bad))
        it.update(RT.analyze(it, g["detail"], g["curst"]))
        it["rights_checked_at"] = stamp
        # 상세의 PNU 로 보완 (목록 지번이 블록·로트 표기라 PNU 를 못 만든 물건)
        if not it.get("pnu"):
            for o in (g["detail"].get("gdsDspslObjctLst") or []):
                pn = str((o or {}).get("pnuNoCtt") or "")
                if len(pn) == 19 and pn.isdigit():
                    it["pnu"], it["bjdong_code"] = pn, pn[:10]
                    break
    AUDIT["checked"] += len(got)
    AUDIT["mismatched"] += len(mismatches)
    for m in mismatches[:20]:
        ERRORS.add("audit", "목록·상세 값 불일치 — " + m, level="warn")
    log(f"  자체 검증: 상세와 대조 {len(got)}건 중 불일치 {len(mismatches)}건")
    if targets and len(got) < len(targets):
        ERRORS.add("rights", f"상세 조회 {len(targets) - len(got)}건 실패 (다음 실행에서 재시도)", level="warn")
    log(f"  상세 분석 완료 {len(got)}건")


def finalize_item(it: dict) -> dict:
    """필드 기본값 채우기 + 기존 필드 먼저 오도록 정렬 (프론트 하위 호환)"""
    base_keys = ["id", "court", "address", "property_type", "appraisal", "min_bid",
                 "auction_date", "failed_bids", "bid_ratio", "scraped_date"]
    defaults = {k: None for k in ENRICH_FIELDS}
    defaults.update({"risk_tags": [], "rights_risk": "미확인", "rights_keywords": [], "rights_has_spec": False,
                     "is_illegal_building": False, "enrich_status": "pending"})
    out = {k: it.get(k) for k in base_keys}
    for k in ENRICH_FIELDS:
        v = it.get(k, defaults[k])
        out[k] = defaults[k] if (v is None and defaults[k] is not None) else v
    for k, v in it.items():          # 스크래퍼가 나중에 추가할 수 있는 필드도 보존
        if k not in out:
            out[k] = v
    out.update(derive(out))          # 검색용 파생 필드 (지역·종류·갭·권리등급 등) — 매번 다시 계산
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
    changed = json.dumps([strip_empty(i) for i in items], sort_keys=True, ensure_ascii=False) != \
        json.dumps([strip_empty(i) for i in prev_items], sort_keys=True, ensure_ascii=False)
    stamp = now_kst()
    cur_doc = read_json(AUCTIONS_PATH, {}) or {}       # 수집기가 방금 쓴 파일 (수집 리포트 포함)
    doc = {
        "updated": stamp.date().isoformat() if changed else prev_doc.get("updated", stamp.date().isoformat()),
        "updated_at": stamp.isoformat(timespec="seconds") if changed else prev_doc.get("updated_at"),
        "collected_at": cur_doc.get("collected_at") or prev_doc.get("collected_at"),
        "collect_report": cur_doc.get("collect_report") or prev_doc.get("collect_report"),
        "auctions": items,
    }
    if not doc["updated_at"]:
        doc["updated_at"] = stamp.isoformat(timespec="seconds")
    write_auctions(AUCTIONS_PATH, doc)
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
        "rights_detail_checked": cnt(lambda i: i.get("rights_checked_at")),
        "rights_by_grade": {g: cnt(lambda i, g=g: i.get("rights_grade") == g) for g in ("safe", "waiver", "caution", "danger", "unknown")},
        "collect_courts_failed": (cur_doc.get("collect_report") or {}).get("courts_failed"),
        "audit_list_vs_detail": dict(AUDIT),
        "tag_근생주의": cnt(lambda i: "🚨 근생주의" in (i.get("risk_tags") or [])),
        "tag_위반건축물": cnt(lambda i: "🚨 위반건축물" in (i.get("risk_tags") or [])),
        "tag_안전마진": cnt(lambda i: "✨ 안전마진 확보" in (i.get("risk_tags") or [])),
        "pending_enrich": cnt(lambda i: i.get("enrich_status") == "pending"),
        "clean_default_count": len(filter_items(items, DEFAULT_CRITERIA)),   # 첫 화면 '클린 매물' 건수
        "api_calls": HTTP.stats["calls"],
        "api_retries": HTTP.stats["retries"],
        "api_failures": HTTP.stats["failures"],
        "blocked_endpoints": HTTP._blocked,
        "unreachable_hosts": HTTP.dead_hosts,
        "last_scrape_ok_date": LAST_SCRAPE_OK_DATE,
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
    ap.add_argument("--scrape-if-needed", action="store_true",
                    help="오늘 이미 목록 수집에 성공했으면 1단계 생략 (하루 여러 번 실행용)")
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
    log("[1단계] 경매 목록 수집 (collect_court.py)")
    global LAST_SCRAPE_OK_DATE
    today = now_kst().date().isoformat()
    LAST_SCRAPE_OK_DATE = ((read_json(ERROR_LOG_PATH, {}) or {}).get("summary") or {}).get("last_scrape_ok_date")
    scrape_ok = True
    if ARGS.skip_scrape:
        log("  생략 (--skip-scrape)")
    elif ARGS.scrape_if_needed and LAST_SCRAPE_OK_DATE == today:
        log("  생략 — 오늘 이미 수집 완료")
    else:
        scrape_ok = run_scraper()
        if scrape_ok:
            LAST_SCRAPE_OK_DATE = today
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
    for it in items:
        apply_court_defaults(it)
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
    log("[5단계] 권리분석 (법원 특수조건·명세서 요약·현황조사서)")
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
        if it.get("enriched_at") or it.get("rights_checked_at"):
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
