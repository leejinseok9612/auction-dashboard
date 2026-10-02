#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
법원경매정보(courtauction.go.kr) 수도권 주거용 물건 수집기 (collect_court.py)
────────────────────────────────────────────────────────────
기존 scrape_auctions.py 를 대체한다. 실제 사이트 응답을 조사해 확인한 사실에 맞춰 다시 작성:

  · 법원 코드   사이트의 법원 목록 API(selectCortOfcLst) 값 사용 — 수도권 16개 법원 전부
  · 최저가      화면에 표시되는 값은 notifyMinmaePrice1 (minmaePrice 는 직전 회차 가격)
  · 물건 단위   한 사건에 물건이 여러 개 있고, 한 물건에 목록(토지·건물)이 여러 줄 → (법원, 사건, 물건번호)로 묶음
  · 고유 ID     사건번호는 법원마다 따로 매겨지므로 법원코드를 포함한 groupmaemulser 를 ID 로 사용
  · 기간        매각기일 오늘 ~ +60일 (사이트 기본값은 14일이라 그 뒤 물건이 빠짐)
  · 용도        건물 > 주거용건물 (아파트·연립·다세대·빌라·오피스텔·단독·다가구 등)
  · 주소 코드   응답의 법정동코드(srchHjguRdCd) + 지번(daepyoLotno) → PNU 를 직접 생성 (주소 API 불필요)
  · 특수조건    spJogCd(유치권·법정지상권·별도등기 등) + 물건비고(mulBigo) 저장
  · 검증        법원별로 "사이트가 알려준 총 건수 = 실제 수집 건수" 를 대조해 collect_report 에 기록

출력: docs/data/auctions.json  →  {"updated", "collected_at", "collect_report", "auctions": [...]}
법원 하나가 수집 실패하면 그 법원 물건은 이전 파일 내용을 유지한다 (부분 실패로 물건이 사라지지 않게).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
BASE = "https://www.courtauction.go.kr"
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "data" / "auctions.json"

DAYS_AHEAD = int(os.environ.get("COLLECT_DAYS_AHEAD", "60"))
PAGE_SIZE = 40                 # 사이트가 허용하는 최대 페이지 크기 (100 은 거부됨)
MAX_PAGES = 400                # 법원 1곳당 안전 상한 (16,000줄)
CALL_GAP_MS = 600              # 요청 간격
SEARCH_URL = "/pgj/pgjsearch/searchControllerMain.on"

# 사이트 법원 목록 API 로 확인한 수도권 법원 코드 (2026-10 기준)
METRO_COURTS = [
    ("B000210", "서울중앙지방법원"), ("B000211", "서울동부지방법원"), ("B000215", "서울서부지방법원"),
    ("B000212", "서울남부지방법원"), ("B000213", "서울북부지방법원"),
    ("B000214", "의정부지방법원"), ("B214807", "고양지원"), ("B214804", "남양주지원"),
    ("B000240", "인천지방법원"), ("B000241", "부천지원"),
    ("B000250", "수원지방법원"), ("B000251", "성남지원"), ("B000252", "여주지원"),
    ("B000253", "평택지원"), ("B250826", "안산지원"), ("B000254", "안양지원"),
]
METRO_SIDO = ("서울", "인천", "경기")

# 매각 특수조건 코드 (사이트 공통코드 RLET_DSPSL_SPC_COND_CD)
SPECIAL_CODES = {
    "0004301": "법정지상권", "0004302": "별도등기", "0004303": "유치권", "0004304": "분묘기지권",
    "0004305": "재매각", "0004306": "특별매각조건", "0004307": "농지취득", "0004308": "예고등기",
    "0004309": "선순위", "0004310": "우선매수신고", "0004311": "맹지",
}
LOT_KIND = {"01": "토지", "02": "건물", "03": "집합건물"}

# 검색 요청 기본 틀 (실제 요청을 가로채지 못했을 때 사용)
REQUEST_TEMPLATE = {
    "dma_pageInfo": {"pageNo": 1, "pageSize": PAGE_SIZE, "bfPageNo": "", "startRowNo": "", "totalCnt": "",
                     "totalYn": "Y", "groupTotalCount": ""},
    "dma_srchGdsDtlSrchInfo": {
        "rletDspslSpcCondCd": "", "bidDvsCd": "000331", "mvprpRletDvsCd": "00031R", "cortAuctnSrchCondCd": "0004601",
        "rprsAdongSdCd": "", "rprsAdongSggCd": "", "rprsAdongEmdCd": "", "rdnmSdCd": "", "rdnmSggCd": "", "rdnmNo": "",
        "mvprpDspslPlcAdongSdCd": "", "mvprpDspslPlcAdongSggCd": "", "mvprpDspslPlcAdongEmdCd": "",
        "rdDspslPlcAdongSdCd": "", "rdDspslPlcAdongSggCd": "", "rdDspslPlcAdongEmdCd": "",
        "cortOfcCd": "B000210", "jdbnCd": "", "execrOfcDvsCd": "", "lclDspslGdsLstUsgCd": "", "mclDspslGdsLstUsgCd": "",
        "sclDspslGdsLstUsgCd": "", "cortAuctnMbrsId": "", "aeeEvlAmtMin": "", "aeeEvlAmtMax": "",
        "lwsDspslPrcRateMin": "", "lwsDspslPrcRateMax": "", "flbdNcntMin": "", "flbdNcntMax": "",
        "objctArDtsMin": "", "objctArDtsMax": "", "mvprpArtclKndCd": "", "mvprpArtclNm": "", "mvprpAtchmPlcTypCd": "",
        "notifyLoc": "off", "lafjOrderBy": "", "pgmId": "PGJ151F01", "csNo": "", "cortStDvs": "1", "statNum": 1,
        "bidBgngYmd": "", "bidEndYmd": "", "dspslDxdyYmd": "", "fstDspslHm": "", "scndDspslHm": "", "thrdDspslHm": "",
        "fothDspslHm": "", "dspslPlcNm": "", "lwsDspslPrcMin": "", "lwsDspslPrcMax": "", "grbxTypCd": "",
        "gdsVendNm": "", "fuelKndCd": "", "carMdyrMax": "", "carMdyrMin": "", "carMdlNm": "", "sideDvsCd": "",
    },
}
REQUEST_HEADERS = {"Content-Type": "application/json;charset=UTF-8", "Accept": "application/json",
                   "submissionid": "mf_wfm_mainFrame_sbm_selectGdsDtlSrch", "SC-Userid": "SYSTEM"}

# 브라우저 안에서 검색 API 를 직접 호출 (사이트 세션·쿠키 그대로 사용)
JS_SEARCH = """async (arg) => {
    try {
        var resp = await fetch(arg.url, {method:'POST', headers: arg.headers, body: JSON.stringify(arg.body), credentials:'include'});
        var text = await resp.text();
        var d = JSON.parse(text);
        if (!d || !d.data) return {ok:false, reason:'no_data:' + text.slice(0,120), status:resp.status};
        return {ok:true, rows:d.data.dlt_srchResult || [], pageInfo:d.data.dma_pageInfo || {}};
    } catch(e) { return {ok:false, reason:String(e)}; }
}"""


def now_kst() -> datetime:
    return datetime.now(KST)


def log(msg: str) -> None:
    print(f"[{now_kst():%H:%M:%S}] {msg}", flush=True)


def to_int(v) -> int | None:
    try:
        s = str(v).replace(",", "").strip()
        return int(float(s)) if s else None
    except (TypeError, ValueError):
        return None


def ymd(s) -> str | None:
    s = re.sub(r"\D", "", str(s or ""))
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) >= 8 and s[:4] != "0000" else None


def hhmm(s) -> str | None:
    s = re.sub(r"\D", "", str(s or ""))
    return f"{s[:2]}:{s[2:4]}" if len(s) >= 4 else None


def clean(s) -> str:
    return re.sub(r"\s+", " ", str(s or "").replace("\r", " ").replace("\n", " ")).strip()


_LOT_RE = re.compile(r"^(산)?\s*(\d{1,4})(?:-(\d{1,4}))?$")


def make_pnu(bjdong: str, lotno: str) -> str | None:
    """법정동코드(10) + 지번 → PNU(19). 지번이 '508-123' / '산12-3' 형태가 아니면 None (블록·로트 표기 등)"""
    m = _LOT_RE.match(clean(lotno))
    if not (bjdong and len(bjdong) == 10 and bjdong.isdigit() and m):
        return None
    return f"{bjdong}{'2' if m.group(1) else '1'}{m.group(2).zfill(4)}{(m.group(3) or '0').zfill(4)}"


_BJDONG: dict[str, str] | None = None
SIGUNGU_ALIAS = {"인천광역시 남구": "인천광역시 미추홀구"}      # 옛 이름으로 적힌 주소


def std_bjdong(sido: str, sigu: str, dong: str, ri: str) -> str | None:
    """
    행정표준 법정동코드(10자리) 조회 — scraper/bjdong_codes.json (지역명 → 코드)
    법원 사이트의 지역 코드는 일부 시군구에서 표준 코드와 다르다 (예: 금천구 11540 ↔ 표준 11545).
    건축물대장·실거래가 API 는 표준 코드를 쓰므로 지역명으로 표준 코드를 찾는다.
    """
    global _BJDONG
    if _BJDONG is None:
        try:
            _BJDONG = json.load(open(Path(__file__).resolve().parent / "bjdong_codes.json", encoding="utf-8")).get("codes", {})
        except (OSError, ValueError):
            _BJDONG = {}
    head = SIGUNGU_ALIAS.get(f"{sido} {sigu}", f"{sido} {sigu}")
    for name in (" ".join(x for x in [head, dong, ri] if x), " ".join(x for x in [head, dong] if x)):
        code = _BJDONG.get(name)
        if code:
            return code
    return None


def parse_areas(text: str) -> list[float]:
    """'철근콘크리트조 67.87㎡' → [67.87]"""
    return [float(x.replace(",", "")) for x in re.findall(r"(\d[\d,]*(?:\.\d+)?)\s*㎡", text or "")]


# ════════════════════════════════════════════════════════════
# 원본 행 → 물건 단위 묶기
# ════════════════════════════════════════════════════════════
def build_item(rows: list[dict], today: str) -> dict | None:
    """같은 물건(groupmaemulser)의 목록 행들 → 물건 1건"""
    # 대표 행: 집합건물 > 건물 > 토지 순, 같은 종류면 목록번호 빠른 순
    order = {"03": 0, "02": 1, "01": 2}
    rows = sorted(rows, key=lambda r: (order.get(r.get("mokGbncd"), 9), to_int(r.get("mokmulSer")) or 0))
    r = rows[0]
    appraisal = to_int(r.get("gamevalAmt"))
    notify1 = to_int(r.get("notifyMinmaePrice1"))
    prev_min = to_int(r.get("minmaePrice"))
    min_bid = notify1 or prev_min          # 화면 표시값 = 이번 매각기일 최저가
    if not appraisal or not min_bid:
        return None
    sido = clean(r.get("hjguSido"))
    if not any(k in sido for k in METRO_SIDO):
        return None

    addresses, seen = [], set()
    for x in rows:
        a = clean(x.get("printSt"))
        if a and a not in seen:
            seen.add(a)
            addresses.append({"kind": LOT_KIND.get(x.get("mokGbncd"), "기타"), "address": a,
                              "detail": clean(x.get("pjbBuldList")) or None})
    lot = clean(r.get("daepyoLotno"))
    court_code = clean(r.get("srchHjguRdCd"))
    # 표준 법정동코드: 지역명으로 코드표 조회, 없으면(개편된 새 구 이름 등) 법원 코드 사용
    bjdong = std_bjdong(sido, clean(r.get("hjguSigu")), clean(r.get("hjguDong")), clean(r.get("hjguRd"))) or court_code
    jibun_addr = " ".join(x for x in [sido, clean(r.get("hjguSigu")), clean(r.get("hjguDong")), clean(r.get("hjguRd")), lot] if x)
    unit = clean(r.get("buldList")) or None
    bname = clean(r.get("buldNm")) or None

    # 면적: 대표 목록(집합건물 전유부분 또는 건물)에 적힌 면적 합계 (복층·여러 층이면 합산)
    areas = parse_areas(r.get("pjbBuldList"))
    court_area = round(sum(areas), 2) if areas else None

    specials = [SPECIAL_CODES.get(c, c) for c in str(r.get("spJogCd") or "").split(",") if c.strip()]
    remarks = clean(r.get("mulBigo")) or None
    next_bids = [v for v in (to_int(r.get(f"notifyMinmaePrice{i}")) for i in (2, 3, 4)) if v]
    kinds = sorted({LOT_KIND.get(x.get("mokGbncd"), "기타") for x in rows})

    return {
        "id": clean(r.get("groupmaemulser")) or f"{r.get('boCd')}{r.get('saNo')}{r.get('maemulSer')}",
        "case_no": clean(r.get("srnSaNo")),
        "item_no": to_int(r.get("maemulSer")) or 1,
        "court": clean(r.get("jiwonNm")),
        "court_code": clean(r.get("boCd")),
        "court_dept": clean(r.get("jpDeptNm")) or None,
        "address": addresses[0]["address"] if addresses else jibun_addr,
        "property_type": clean(r.get("dspslUsgNm")),
        "appraisal": appraisal,
        "min_bid": min_bid,
        "auction_date": ymd(r.get("maeGiil")),
        "failed_bids": to_int(r.get("yuchalCnt")) or 0,
        "bid_ratio": round(min_bid / appraisal * 100, 1),
        "scraped_date": today,                       # 최초 수집일 (저장 시 이전 값으로 보존)
        # ── 법원 원본에서 가져온 추가 정보 ──
        "prev_min_bid": prev_min if (prev_min and prev_min != min_bid) else None,
        "next_min_bids": next_bids,                  # 같은 공고에 잡힌 다음 기일 최저가
        "auction_time": hhmm(r.get("maeHh1")),
        "auction_place": clean(r.get("maePlace")) or None,
        "decision_date": ymd(r.get("maegyuljGiil")),
        "special_conditions": specials,              # 법원이 표시한 특수조건
        "remarks": remarks,                          # 물건비고
        "is_bulk_sale": bool(remarks and "일괄매각" in remarks),
        "is_share_sale": bool(remarks and re.search(r"지분\s*매각|지분\s*일괄", remarks)),
        "building_name": bname,
        "unit": unit,
        "lot_kinds": kinds,                          # 포함된 목록 종류 (토지/건물/집합건물)
        "lot_count": len(rows),
        "addresses": addresses if len(addresses) > 1 else [],
        "court_area": court_area,                    # 법원 목록상 건물(전유) 면적 ㎡
        "usage_code": clean(r.get("sclsUtilCd")) or None,
        "jibun_address": jibun_addr,
        "umd_name": " ".join(x for x in [clean(r.get("hjguDong")), clean(r.get("hjguRd"))] if x) or None,
        "bjdong_code": bjdong if len(bjdong) == 10 else None,
        "bjdong_code_court": court_code if (court_code and court_code != bjdong) else None,   # 법원 내부 코드 (표준과 다를 때만)
        "pnu": make_pnu(bjdong, lot),
        "view_count": to_int(r.get("inqCnt")),
        "related_case": clean(r.get("dupSaNo")) or None,
    }


def group_rows(rows: list[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for r in rows:
        key = clean(r.get("groupmaemulser")) or f"{r.get('boCd')}{r.get('saNo')}{r.get('maemulSer')}"
        groups.setdefault(key, []).append(r)
    return groups


# ════════════════════════════════════════════════════════════
# 수집
# ════════════════════════════════════════════════════════════
def open_search_page(p):
    """브라우저를 띄워 물건상세검색 화면까지 이동 (사이트 세션 확보)"""
    browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
    ctx = browser.new_context(
        viewport={"width": 1920, "height": 1080}, locale="ko-KR",
        user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"))
    page = ctx.new_page()
    last_err = None
    for attempt in range(1, 4):
        try:
            page.goto(BASE + "/pgj/index.on", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_selector("#mf_wfm_header_anc_auctnGdsMain", timeout=60000)
            page.wait_for_timeout(4000)
            page.click("#mf_wfm_header_anc_auctnGdsMain")
            page.wait_for_selector("#mf_wfm_mainFrame_btn_gdsDtlSrch", timeout=60000)
            page.wait_for_timeout(4000)
            return browser, page
        except Exception as e:      # 접속 불안정 대비 재시도
            last_err = e
            log(f"  사이트 접속 시도 {attempt}/3 실패: {type(e).__name__}")
            page.wait_for_timeout(5000 * attempt)
    browser.close()
    raise RuntimeError(f"법원경매 사이트 접속 실패: {last_err}")


def fetch_page(page, court_code: str, page_no: int, d_from: str, d_to: str, bid_dvs: str) -> dict:
    body = json.loads(json.dumps(REQUEST_TEMPLATE))
    si = body["dma_srchGdsDtlSrchInfo"]
    si.update({"cortOfcCd": court_code, "bidBgngYmd": d_from, "bidEndYmd": d_to, "bidDvsCd": bid_dvs,
               "lclDspslGdsLstUsgCd": "20000", "mclDspslGdsLstUsgCd": "20100"})   # 건물 > 주거용건물
    body["dma_pageInfo"].update({"pageNo": page_no, "bfPageNo": str(page_no - 1) if page_no > 1 else ""})
    last = {"ok": False, "reason": "not_called"}
    for attempt in range(1, 4):
        try:
            last = page.evaluate(JS_SEARCH, {"url": SEARCH_URL, "headers": REQUEST_HEADERS, "body": body})
        except Exception as e:
            last = {"ok": False, "reason": f"evaluate:{type(e).__name__}"}
        if last.get("ok"):
            page.wait_for_timeout(CALL_GAP_MS)
            return last
        page.wait_for_timeout(2000 * attempt)
    return last


def collect_court(page, code: str, name: str, d_from: str, d_to: str) -> tuple[list[dict], dict]:
    """법원 1곳 전체 수집 → (원본 행 목록, 검증 리포트)"""
    rows: list[dict] = []
    report = {"court": name, "code": code, "ok": True, "site_rows": 0, "site_items": 0, "rows": 0, "items": 0, "note": ""}
    for bid_dvs, label in (("000331", "기일입찰"), ("000332", "기간입찰")):
        first = fetch_page(page, code, 1, d_from, d_to, bid_dvs)
        if not first.get("ok"):
            report.update(ok=False, note=f"{label} 1페이지 실패: {first.get('reason')}")
            return [], report
        pi = first.get("pageInfo") or {}
        total = to_int(pi.get("totalCnt")) or 0
        report["site_rows"] += total
        report["site_items"] += to_int(pi.get("groupTotalCount")) or 0
        part = list(first.get("rows") or [])
        pages = min(MAX_PAGES, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        for pg in range(2, pages + 1):
            r = fetch_page(page, code, pg, d_from, d_to, bid_dvs)
            if not r.get("ok"):
                report.update(ok=False, note=f"{label} {pg}/{pages}페이지 실패: {r.get('reason')}")
                return [], report
            got = r.get("rows") or []
            if not got:
                break
            part.extend(got)
        rows.extend(part)
    # 중복 행 제거 (docid 기준 — 수집 도중 목록이 바뀌어 같은 줄이 두 번 올 수 있음)
    uniq = {}
    for r in rows:
        uniq[r.get("docid") or json.dumps(r, sort_keys=True)] = r
    rows = list(uniq.values())
    report["rows"] = len(rows)
    report["items"] = len(group_rows(rows))
    # 검증: 사이트가 알려준 건수와 일치하는가 (수집 도중 변동을 감안해 1% 또는 3건까지 허용)
    tol = max(3, int(report["site_rows"] * 0.01))
    if abs(report["rows"] - report["site_rows"]) > tol:
        report.update(ok=False, note=f"건수 불일치: 사이트 {report['site_rows']}줄 / 수집 {report['rows']}줄")
        return [], report
    return rows, report


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("오류: pip install playwright && python -m playwright install chromium")
        return 1
    started = time.time()
    today = now_kst().date()
    d_from, d_to = today.strftime("%Y%m%d"), (today + timedelta(days=DAYS_AHEAD)).strftime("%Y%m%d")
    log(f"=== 법원경매 수집 시작: 매각기일 {d_from} ~ {d_to}, 수도권 {len(METRO_COURTS)}개 법원, 주거용건물 ===")

    prev = {}
    try:
        prev = json.load(open(OUT, encoding="utf-8"))
    except (OSError, ValueError):
        pass
    prev_items = [i for i in prev.get("auctions", []) if isinstance(i, dict)]
    prev_by_id = {i.get("id"): i for i in prev_items}

    reports, items, failed_codes = [], {}, set()
    with sync_playwright() as p:
        try:
            browser, page = open_search_page(p)
        except RuntimeError as e:
            log(f"❌ {e}")
            return 1
        for code, name in METRO_COURTS:
            rows, rep = collect_court(page, code, name, d_from, d_to)
            reports.append(rep)
            if not rep["ok"]:
                failed_codes.add(code)
                log(f"  ❌ {name}: {rep['note']}")
                continue
            n_before = len(items)
            for key, grp in group_rows(rows).items():
                it = build_item(grp, today.isoformat())
                if it:
                    items[it["id"]] = it
            rep["kept"] = len(items) - n_before
            log(f"  ✅ {name}: 사이트 {rep['site_rows']}줄/{rep['site_items']}물건 → 수집 {rep['rows']}줄/{rep['items']}물건, 저장 {rep['kept']}건")
        browser.close()

    ok_count = sum(1 for r in reports if r["ok"])
    if ok_count == 0:
        log("❌ 모든 법원 수집 실패 — 기존 파일 유지")
        return 1

    # 실패한 법원의 물건은 이전 데이터 유지 (매각기일이 지나지 않은 것만)
    kept_old = 0
    for i in prev_items:
        if i.get("court_code") in failed_codes and (i.get("auction_date") or "") >= today.isoformat() and i.get("id") not in items:
            items[i["id"]] = i
            kept_old += 1
    # 최초 수집일 보존
    for k, it in items.items():
        old = prev_by_id.get(k)
        if old and old.get("scraped_date"):
            it["scraped_date"] = old["scraped_date"]

    final = sorted(items.values(), key=lambda x: (x.get("scraped_date") or "", x.get("auction_date") or ""), reverse=True)
    doc = {
        "updated": today.isoformat(),
        "collected_at": now_kst().isoformat(timespec="seconds"),
        "collect_report": {
            "period": [ymd(d_from), ymd(d_to)], "scope": "수도권 16개 법원 · 건물 > 주거용건물",
            "courts_ok": ok_count, "courts_failed": [r["court"] for r in reports if not r["ok"]],
            "kept_from_previous": kept_old, "courts": reports,
        },
        "auctions": final,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
        f.write("\n")
    os.replace(tmp, OUT)
    log(f"=== 완료: {len(final)}건 저장 (법원 {ok_count}/{len(METRO_COURTS)} 성공, 이전 유지 {kept_old}건) — {time.time() - started:.0f}초 ===")
    # 일부 법원 실패는 경고로만 처리 (다음 실행에서 다시 시도)
    return 0


if __name__ == "__main__":
    sys.exit(main())
