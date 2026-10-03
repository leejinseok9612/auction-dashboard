#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
공시가격 채우기 — 국내 인터넷(내 컴퓨터)에서 실행하는 프로그램

  브이월드(공시가격 제공처)는 해외 서버의 접속을 받지 않아 GitHub 서버에서는 조회할 수 없다.
  그래서 이 파일만 내 컴퓨터에서 가끔 실행해 공시가격 표(official_prices.json)를 만들고,
  그 파일을 GitHub 에 올리면 사이트 데이터에 자동 반영된다.

  실행 (맥 터미널에 한 줄 붙여넣기):
    python3 <(curl -fsSL https://raw.githubusercontent.com/leejinseok9612/auction-dashboard/main/scraper/price_local.py)

  · 파이썬 기본 기능만 사용 (추가 설치 없음)
  · 인증키는 처음 한 번만 물어보고 내 컴퓨터(~/.auction_dashboard_vworld)에만 저장 — 결과 파일에는 들어가지 않는다
  · main.py 도 이 파일의 동·호 대조 규칙(pick_price_row)을 그대로 가져다 쓴다
"""
from __future__ import annotations

import json
import os
import re
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
REPO = "leejinseok9612/auction-dashboard"
RAW = f"https://raw.githubusercontent.com/{REPO}/main"
AUCTIONS_URL = os.environ.get("AUCTIONS_URL") or f"{RAW}/docs/data/auctions.json"
TABLE_URL = os.environ.get("PRICE_TABLE_URL") or f"{RAW}/docs/data/official_prices.json"
UPLOAD_PAGE = f"https://github.com/{REPO}/upload/main/docs/data"
SELF_URL = f"{RAW}/scraper/price_local.py"
API_BASE = (os.environ.get("VWORLD_API_BASE") or "https://api.vworld.kr/ned/data").rstrip("/")
APT_OP, HOUSE_OP = "getApartHousingPriceAttr", "getIndvdHousingPriceAttr"
DEFAULT_DOMAIN = "https://leejinseok9612.github.io/auction-dashboard"     # 인증키 발급 때 등록한 서비스 주소
KEY_FILE = Path.home() / ".auction_dashboard_vworld"
TABLE_NAME = "official_prices.json"

FOUND_TTL_DAYS = 120       # 공시가격은 연 1회 공시 → 찾은 값은 오래 유지
MISS_TTL_DAYS = 14         # 못 찾은 물건은 2주 뒤 다시 조회
PAGE_SIZE, MAX_PAGES = 1000, 12
WORKERS = 4
MIN_INTERVAL = 0.08       # 호출 간격(초)
RULE_VERSION = 2           # 동·호 대조 규칙 버전 — 올리면 값을 못 찾았던 물건을 다시 조회
PRICE_KEYS = ("pblntfPc", "housePc", "hsprc", "hsprcAmt")


# ════════════════════════════════════════════════════════════
# 공용 규칙 (main.py 에서도 사용)
# ════════════════════════════════════════════════════════════
def to_int(v):
    if v is None:
        return None
    try:
        s = str(v).replace(",", "").strip()
        return int(float(s)) if s else None
    except ValueError:
        return None


def to_float(v):
    try:
        s = str(v).replace(",", "").strip()
        return float(s) if s else None
    except (ValueError, TypeError):
        return None


def digits(s) -> str:
    return re.sub(r"\D", "", str(s or ""))


def price_kind(item: dict):
    """'apt'(공동주택가격) / 'house'(개별주택가격) / None(대상 아님 — 오피스텔·상가는 국세청 기준시가)"""
    ptype = item.get("property_type", "") or ""
    if any(k in ptype for k in ("아파트", "다세대", "연립", "빌라")):
        return "apt"
    if any(k in ptype for k in ("단독", "다가구")):
        return "house"
    return None


_ROAD_RE = re.compile(r"^(.*?\S+(?:로|길)\s+\d+(?:-\d+)?)(?=[\s,]|$)")
_JIBUN_RE = re.compile(r"^(.*?\S+(?:동|리|가)\d*\s+(?:산\s*)?\d+(?:-\d+)?)(?=[\s,]|$)")
_HO_RE = re.compile(r"제?\s*(지하|비|B)?\s*(\d+)\s*호")
_BLDG_DONG_RE = re.compile(r"(?:^|\s)제?\s*([0-9A-Za-z가-힣]{1,6}?)\s*동(?=\s|제|\d|$)")


def fill_unit(item: dict) -> None:
    """동·호가 아직 안 채워진 물건은 주소에서 뽑는다 (main.py 의 parse_address 와 같은 규칙)"""
    if item.get("unit_ho") is not None or item.get("building_dong") is not None:
        return
    s = " ".join(x for x in [item.get("jibun_address") or item.get("address") or "", item.get("unit") or ""] if x)
    s = re.sub(r"\([^)]*\)", " ", s)
    s = re.sub(r"외\s*\d*\s*필지", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    m = _ROAD_RE.match(s) or _JIBUN_RE.match(s)
    if not m:
        return
    rest = s[len(m.group(1)):]
    hos = list(_HO_RE.finditer(rest))
    ho = (("B" if hos[-1].group(1) else "") + hos[-1].group(2)) if hos else None
    dm = _BLDG_DONG_RE.search(rest)
    dong = dm.group(1) if dm else None
    dash = re.search(r"(?:^|\s)(\d{1,4})-(\d{1,5})\s*호", rest)      # '101-2002호' = 101동 2002호
    if dash:
        dong, ho = dong or dash.group(1), dash.group(2)
    item["building_dong"], item["unit_ho"] = dong, ho


def price_target(item: dict) -> bool:
    """
    공시가격을 붙일 수 있는 물건인지
      · 일괄매각(여러 호·여러 필지를 한 번에) → 한 채의 공시가격으로 대표할 수 없음
      · 지분매각 → 공시가격은 한 채 전체 값이라 최저가(지분 값)와 비교하면 틀린 계산이 됨
    """
    if not price_kind(item) or len(item.get("pnu") or "") != 19:
        return False
    return not item.get("is_bulk_sale") and not item.get("is_share_sale")


def price_plausible(price, item: dict) -> bool:
    """공시가격이 감정가의 20%~130% 범위인지 — 벗어나면 다른 건물·일부만 매각 등 대상이 어긋난 것"""
    ap = to_int(item.get("appraisal"))
    if not price or not ap:
        return bool(price)
    return 0.2 <= price / ap <= 1.3


def price_pnus(item: dict) -> list:
    """조회할 PNU 후보: 수집된 지번 → 건축물대장이 등재된 지번(다를 때)"""
    pnu = item.get("pnu") or ""
    out = [pnu] if len(pnu) == 19 else []
    m = re.search(r"건축물대장 지번 (\d+)-(\d+)", item.get("building_lot_note") or "")
    if m and out:
        alt = pnu[:11] + m.group(1).zfill(4) + m.group(2).zfill(4)
        if alt not in out:
            out.append(alt)
    return out


_KO_LETTER = {"에이": "A", "비": "B", "씨": "C", "시": "C", "디": "D", "이": "E", "에프": "F", "지": "G", "에이치": "H"}
_KO_ORDER = "가나다라마바사아자차카타파하"


def norm_dong(s) -> str:
    """
    동 이름 비교용 — 출처마다 표기가 달라 핵심만 남긴다
      '제101동' → '101' / '에이동'·'A동' → 'A' / '가동' → '가' / '수팰리스5동' → '5' / '두산빌리지'(건물명) → ''
    """
    raw = re.sub(r"\s+", "", str(s or ""))
    had_dong = raw.endswith("동")
    t = re.sub(r"동$", "", re.sub(r"^제", "", raw)).upper()
    if t in ("", "-", "0"):
        return ""
    t = _KO_LETTER.get(t, t)
    m = re.search(r"(\d+)$", t)
    if m:
        return m.group(1).lstrip("0") or "0"
    if re.search(r"(^|[^A-Z])[A-Z]$", t):
        return t[-1]
    if len(t) == 1:
        return t
    if had_dong and t[-1] in _KO_ORDER:
        return t[-1]
    return ""                                  # 건물 이름만 적힌 경우 — 동 구분 없음으로 취급


def norm_ho(s):
    """호 비교용: (지하 여부, 숫자) — '제비01호'·'B01'·'지하1' → (True, '1')"""
    t = re.sub(r"\s+", "", str(s or "")).upper()
    d = digits(t)
    if not d:
        return None
    return (bool(re.search(r"지하|^제?B|^제?비", t)), d.lstrip("0") or "0")


def row_price(r: dict):
    for k in PRICE_KEYS:
        v = to_int(r.get(k))
        if v:
            return v
    return None


def pick_price_row(rows: list, item: dict, unit_required: bool):
    """
    공시가격 레코드 중 이 물건(동·호)에 해당하는 값 → (가격, 연도) 또는 None
    동·호가 같은 후보가 여럿이고 가격이 서로 다르면 전용면적으로 가려내고,
    그래도 못 가리면 채택하지 않는다 (틀린 값보다 빈 값).
    """
    cands = []
    want_ho = norm_ho(item.get("unit_ho")) if item.get("unit_ho") else None
    want_dong = norm_dong(item.get("building_dong"))
    want_base = bool(want_ho and want_ho[0]) or "지하" in f"{item.get('address') or ''} {item.get('unit') or ''}"
    for r in rows:
        price = row_price(r)
        if not price:
            continue
        if unit_required:
            ho = norm_ho(r.get("hoNm"))
            if not want_ho or not ho or ho[1] != want_ho[1]:
                continue
            # 지하 여부: 표기가 출처마다 달라(호 이름 대신 층으로 표시) 주소·층까지 함께 본다
            fl = str(r.get("floorNm") or "").strip()
            row_base = ho[0] or "지" in fl or fl.upper().startswith(("-", "B"))
            if row_base != want_base:
                continue
            dong = norm_dong(r.get("dongNm"))
            if want_dong and dong and dong != want_dong:
                continue
        year = to_int(str(r.get("stdrYear") or r.get("crtnDay") or r.get("stdDay") or "")[:4]) or 0
        cands.append((price, year, to_float(r.get("prvuseAr")), norm_dong(r.get("dongNm"))))
    if not cands:
        return None
    top_year = max(c[1] for c in cands)
    cands = [c for c in cands if c[1] == top_year]
    if len({c[0] for c in cands}) > 1 and unit_required:
        if want_dong:                       # 동이 정확히 같은 것 우선
            exact = [c for c in cands if c[3] == want_dong]
            if exact:
                cands = exact
        area = to_float(item.get("exclusive_area")) or to_float(item.get("court_area"))
        if len({c[0] for c in cands}) > 1 and area:
            near = [c for c in cands if c[2] and abs(c[2] - area) <= max(0.5, area * 0.01)]
            if near:
                cands = near
        if len({c[0] for c in cands}) > 1:
            return None
    if not unit_required and len({c[0] for c in cands}) > 1:
        return None                         # 한 필지에 개별주택이 여러 채 → 어느 것인지 알 수 없음
    return cands[0][0], top_year


class VworldError(Exception):
    def __init__(self, msg: str, fatal: bool = False):
        super().__init__(msg)
        self.fatal = fatal


def parse_vworld(text: str):
    """브이월드 국가중점데이터 응답 → (레코드 목록, 전체 건수). 오류 응답이면 VworldError"""
    text = text.strip().lstrip("﻿")
    if not text.startswith("{"):
        raise VworldError(f"알 수 없는 응답: {text[:120]!r}")
    data = json.loads(text)
    resp_obj = data.get("response") if isinstance(data.get("response"), dict) else None
    body = resp_obj or next((v for v in data.values() if isinstance(v, dict)), {})
    err = body.get("error") if isinstance(body.get("error"), dict) else {}
    code = str(body.get("resultCode") or err.get("code") or "").strip()
    status = str(body.get("status") or "").upper()
    msg = str(body.get("resultMsg") or err.get("text") or "")[:150]
    rows = body.get("field")
    if isinstance(rows, dict):
        rows = [rows]
    ok_codes = ("", "0", "00", "OK", "NORMAL", "INFO-200", "INFO_200")
    if status == "ERROR" or (code and rows is None and code.upper() not in ok_codes):
        up = (code + " " + msg).upper()
        if "NOT_FOUND" in up or "NODATA" in up or "NO_DATA" in up or "INFO-200" in up or "없습니다" in msg and "키" not in msg:
            return [], 0
        if any(k in up for k in ("KEY", "DOMAIN", "인증", "LIMIT", "QUOTA", "초과", "권한", "UNREGIST", "도메인")):
            raise VworldError(f"{code} {msg}".strip(), fatal=True)
        raise VworldError(f"{code} {msg}".strip() or text[:150])
    rows = [r for r in (rows or []) if isinstance(r, dict)]
    return rows, (to_int(body.get("totalCount")) or len(rows))


# ════════════════════════════════════════════════════════════
# 여기부터는 내 컴퓨터에서 실행할 때만 쓰는 부분
# ════════════════════════════════════════════════════════════
def now_iso() -> str:
    return datetime.now(KST).isoformat(timespec="seconds")


def days_since(iso) -> float:
    try:
        return (datetime.now(KST) - datetime.fromisoformat(iso)).total_seconds() / 86400
    except (TypeError, ValueError):
        return 1e9


def _ssl_ctx():
    ctx = ssl.create_default_context()
    for ca in ("/etc/ssl/cert.pem", "/private/etc/ssl/cert.pem"):     # 맥 기본 인증서 묶음
        if os.path.exists(ca):
            try:
                ctx.load_verify_locations(ca)
            except Exception:
                pass
            break
    return ctx


SSL_CTX = _ssl_ctx()


def http_get(url: str, timeout: int = 25):
    """→ (HTTP 상태, 본문 글자). 주소가 파일 경로면 파일을 읽는다 (시험용)."""
    if not url.startswith("http"):
        return 200, Path(url).read_text(encoding="utf-8")
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (auction-dashboard price_local)"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=SSL_CTX if url.startswith("https") else None) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


class Fetcher:
    def __init__(self, key: str, domain: str):
        self.key, self.domain = key, domain
        self.lock = threading.Lock()
        self.next_slot = 0.0
        self.rows_cache: dict = {}
        self.calls = 0
        self.net_fail = 0                  # 연속 통신 실패
        self.abort = None                  # 중단 사유 (키 오류·접속 불가)
        self.samples: list = []            # 응답 모양 확인용 (인증키 제외)
        self.errors: dict = {}

    def scrub(self, s: str) -> str:
        return str(s).replace(self.key, "***") if self.key else str(s)

    def _throttle(self):
        with self.lock:
            now = time.monotonic()
            slot = max(now, self.next_slot)
            self.next_slot = slot + MIN_INTERVAL
        if slot > now:
            time.sleep(slot - now)

    def _call(self, op: str, pnu: str, year: int, page: int):
        q = {"key": self.key, "pnu": pnu, "stdrYear": str(year), "format": "json",
             "numOfRows": PAGE_SIZE, "pageNo": page}
        if self.domain:
            q["domain"] = self.domain
        url = f"{API_BASE}/{op}?{urllib.parse.urlencode(q)}"
        last = None
        for attempt in range(3):
            if self.abort:
                raise VworldError(self.abort, fatal=True)
            self._throttle()
            with self.lock:
                self.calls += 1
            try:
                status, text = http_get(url)
            except Exception as e:          # 통신 오류 (예외 원문에는 주소가 들어 있어 종류만 기록)
                last = VworldError(f"통신 오류: {type(e).__name__}")
                with self.lock:
                    self.net_fail += 1
                    if self.net_fail >= 8:
                        self.abort = "브이월드에 연결되지 않습니다 (인터넷 연결 확인)"
                time.sleep(1.5 * (attempt + 1))
                continue
            with self.lock:
                self.net_fail = 0
                if len(self.samples) < 3:
                    self.samples.append({"op": op, "pnu": pnu, "year": year, "http": status,
                                         "text": self.scrub(text[:400])})
            if status in (401, 403):
                self.abort = f"브이월드가 인증을 거부했습니다 (HTTP {status})"
                raise VworldError(self.abort, fatal=True)
            if status != 200:
                last = VworldError(f"HTTP {status}: {self.scrub(text[:100])}")
                time.sleep(1.5 * (attempt + 1))
                continue
            try:
                return parse_vworld(text)
            except VworldError as e:
                if e.fatal:
                    self.abort = f"브이월드 오류: {self.scrub(e)}"
                    raise
                last = e
                time.sleep(1.0 * (attempt + 1))
            except ValueError as e:
                last = VworldError(f"응답 해석 실패: {e}")
                time.sleep(1.0 * (attempt + 1))
        raise last or VworldError("알 수 없는 오류")

    def rows(self, op: str, pnu: str, year: int) -> list:
        ck = (op, pnu, year)
        with self.lock:
            if ck in self.rows_cache:
                return self.rows_cache[ck]
        out: list = []
        for page in range(1, MAX_PAGES + 1):
            rows, total = self._call(op, pnu, year, page)
            out.extend(rows)
            if not rows or len(out) >= total:
                break
        with self.lock:
            if len(self.rows_cache) > 300:
                self.rows_cache.clear()
            self.rows_cache[ck] = out
        return out

    def lookup(self, item: dict):
        """→ (가격, 연도, 조회한 PNU, 레코드를 봤는지) — 가격 없으면 (None, None, None, 레코드 유무)"""
        kind = price_kind(item)
        op = APT_OP if kind == "apt" else HOUSE_OP
        this_year = datetime.now(KST).year
        seen = []
        for pnu in price_pnus(item):
            for year in (this_year, this_year - 1):
                rows = self.rows(op, pnu, year)
                hit = pick_price_row(rows, item, unit_required=(kind == "apt"))
                if hit:
                    return hit[0], (hit[1] or year), pnu, rows
                if rows:
                    seen = rows
        return None, None, None, seen


def read_key() -> str:
    key = (os.environ.get("VWORLD_KEY") or "").strip()
    if key:
        return key
    if KEY_FILE.exists():
        key = KEY_FILE.read_text(encoding="utf-8").strip()
        if key:
            return key
    print()
    print("  브이월드 인증키를 붙여넣고 엔터를 누르세요. (처음 한 번만 물어봅니다)")
    print("  · 인증키 보는 곳: https://www.vworld.kr → 로그인 → 마이포털 → 인증키 관리")
    try:
        key = input("  인증키: ").strip()
    except EOFError:
        key = ""
    if not key:
        print("  인증키가 입력되지 않아 종료합니다.")
        sys.exit(1)
    try:
        KEY_FILE.write_text(key + "\n", encoding="utf-8")
        os.chmod(KEY_FILE, 0o600)          # 나만 읽을 수 있게
    except OSError:
        pass
    return key


def load_table() -> dict:
    try:
        status, text = http_get(TABLE_URL)
        if status == 200:
            t = json.loads(text)
            if isinstance(t.get("prices"), dict):
                return t["prices"]
    except Exception:
        pass
    return {}


def is_fresh(entry, item: dict) -> bool:
    if not entry or entry.get("pnu") != item.get("pnu"):
        return False
    age = days_since(entry.get("checked_at"))
    if entry.get("price"):
        return age <= FOUND_TTL_DAYS
    return entry.get("v") == RULE_VERSION and age <= MISS_TTL_DAYS


def make_shortcut() -> None:
    """맥 바탕화면에 다음부터 더블클릭으로 실행할 파일을 만든다 (내 컴퓨터에서 만든 파일이라 보안 경고가 뜨지 않음)"""
    if sys.platform != "darwin":
        return
    path = Path.home() / "Desktop" / "공시가격 채우기.command"
    if path.exists():
        return
    try:
        path.write_text("#!/bin/bash\n"
                        f"python3 <(curl -fsSL {SELF_URL})\n"
                        "echo\nread -n 1 -s -r -p '아무 키나 누르면 창이 닫힙니다.'\n", encoding="utf-8")
        os.chmod(path, 0o755)
        print("  📌 바탕화면에 '공시가격 채우기' 파일을 만들었습니다. 다음부터는 그 파일을 더블클릭하면 됩니다.")
    except OSError:
        pass


def main() -> int:
    print("═" * 54)
    print(f" 공시가격 채우기 — {datetime.now(KST):%Y-%m-%d %H:%M}")
    print("═" * 54)
    key = read_key()
    domain = (os.environ.get("VWORLD_DOMAIN") or DEFAULT_DOMAIN).strip()

    print("[1/3] 경매 물건 목록 내려받는 중…")
    try:
        status, text = http_get(AUCTIONS_URL, timeout=90)
        items = json.loads(text).get("auctions", []) if status == 200 else []
    except Exception as e:
        print(f"  ❌ 목록을 내려받지 못했습니다: {type(e).__name__} — 인터넷 연결을 확인하고 다시 실행해 주세요.")
        return 1
    if not items:
        print("  ❌ 물건 목록이 비어 있습니다. 잠시 뒤 다시 실행해 주세요.")
        return 1
    table = load_table()
    by_id = {i["id"]: i for i in items if i.get("id")}
    for i in by_id.values():
        fill_unit(i)
    table = {k: v for k, v in table.items() if k in by_id}          # 끝난 경매는 표에서 제거
    targets = [i for i in by_id.values()
               if price_target(i) and not is_fresh(table.get(i["id"]), i)]
    targets.sort(key=lambda x: x.get("auction_date") or "9999")
    limit = to_int(os.environ.get("PRICE_LIMIT"))
    if limit:
        targets = targets[:limit]
    print(f"  물건 {len(by_id):,}건 중 이번에 조회할 물건 {len(targets):,}건 (이미 확인된 {len(table):,}건은 건너뜀)")

    f = Fetcher(key, domain)
    found = miss = failed = 0
    unmatched: list = []
    if targets:
        print("[2/3] 브이월드에서 공시가격 조회 중… (몇 분 걸립니다. 창을 닫지 마세요)")
        stamp = now_iso()

        def one(it: dict):
            return it, f.lookup(it)

        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futs = [ex.submit(one, it) for it in targets]
            for n, fu in enumerate(as_completed(futs), 1):
                try:
                    it, (price, year, pnu, rows) = fu.result()
                except VworldError as e:
                    failed += 1
                    msg = f.scrub(e)[:120]
                    f.errors[msg] = f.errors.get(msg, 0) + 1
                else:
                    entry = {"pnu": it.get("pnu"), "checked_at": stamp, "v": RULE_VERSION}
                    if price:
                        entry.update({"price": price, "year": year})
                        if pnu != it.get("pnu"):
                            entry["found_pnu"] = pnu
                        found += 1
                    else:
                        miss += 1
                        if rows and len(unmatched) < 8:        # 레코드는 있는데 동·호가 안 맞은 사례 (규칙 개선용)
                            unmatched.append({"id": it["id"], "dong": it.get("building_dong"), "ho": it.get("unit_ho"),
                                              "rows": len(rows),
                                              "sample": [[r.get("dongNm"), r.get("hoNm"), r.get("floorNm")] for r in rows[:8]]})
                    table[it["id"]] = entry
                if n % 200 == 0 or n == len(targets):
                    print(f"  … {n:,}/{len(targets):,}건 — 공시가격 확인 {found:,}건")
        if f.abort and not found:
            print()
            print(f"  ❌ 중단: {f.abort}")
            if "인증" in f.abort or "KEY" in f.abort.upper():
                print("     인증키가 맞는지 확인해 주세요. 다시 입력하려면 아래 한 줄을 실행한 뒤 다시 시작하면 됩니다.")
                print(f"     rm {KEY_FILE}")
            if f.samples:
                print("     (받은 응답) " + f.samples[0]["text"][:200])
            return 1
    else:
        print("[2/3] 새로 조회할 물건이 없습니다.")

    print("[3/3] 결과 파일 저장 중…")
    out_dir = Path(os.environ.get("PRICE_OUT_DIR") or (Path.home() / "Downloads"))
    if not out_dir.is_dir():
        out_dir = Path.cwd()
    out = out_dir / TABLE_NAME
    doc = {
        "updated_at": now_iso(),
        "source": "브이월드 공동주택가격·개별주택가격 속성조회",
        "stats": {"items": len(by_id), "queried": len(targets), "found": found, "not_found": miss, "failed": failed,
                  "with_price": sum(1 for v in table.values() if v.get("price")), "api_calls": f.calls},
        "debug": {"samples": f.samples, "errors": f.errors, "unmatched": unmatched, "aborted": f.abort},
        "prices": table,
    }
    out.write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print()
    print(f"  ✅ 끝났습니다. 이번에 공시가격 확인 {found:,}건 · 값 없음 {miss:,}건 · 조회 실패 {failed:,}건")
    print(f"     지금까지 공시가격이 있는 물건: {doc['stats']['with_price']:,}건")
    print(f"     결과 파일: {out}")
    print()
    print("  ▶ 마지막 한 단계 — 결과 파일을 GitHub 에 올리기")
    print("     1) 방금 열린 GitHub 화면에 official_prices.json 파일을 끌어다 놓습니다.")
    print("     2) 아래쪽 초록색 'Commit changes' 버튼을 누릅니다.")
    print("     몇 분 뒤 사이트에 공시가격이 반영됩니다.")
    print(f"     (화면이 안 열렸으면: {UPLOAD_PAGE})")
    if sys.platform == "darwin" and not os.environ.get("PRICE_NO_OPEN"):
        make_shortcut()
        subprocess.run(["open", "-R", str(out)], check=False)
        subprocess.run(["open", UPLOAD_PAGE], check=False)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n  중단했습니다.")
        sys.exit(130)
