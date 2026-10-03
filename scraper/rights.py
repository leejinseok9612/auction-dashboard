#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
권리분석 (rights.py)
────────────────────────────────────────────────────────────
법원경매정보 사이트가 "구조화된 데이터"로 제공하는 정보만 사용한다 (화면 자동 조작·PDF 판독 없음).

  1) 물건 목록에서 바로 얻는 정보 (모든 물건)
       · special_conditions  법원이 표시한 특수조건 (유치권·법정지상권·별도등기·재매각 …)
       · remarks             물건비고 (대항력 포기 확약, 위반건축물, 지분매각, 인수 조건 등)
  2) 물건 상세 API (매각기일이 가까운 물건부터, 1회 실행당 RIGHTS_MAX_PER_RUN 건)
       · 매각물건명세서 요약: 최선순위 설정(말소기준권리), 소멸되지 않는 권리, 지상권 개요, 비고
       · 기일 내역: 회차별 최저가와 결과 (실제 유찰 이력)
  3) 현황조사서 API
       · 임차인(전입세대) 목록과 전입일 → 최선순위 설정일보다 빠르면 '선순위 임차인(대항력)'

판정 (rights_risk)
  위험   인수 위험 키워드가 하나라도 있음 (유치권, 법정지상권, 인수되는 권리, 선순위 임차인 …)
  주의   주의 키워드만 있음 (토지별도등기, 재매각, 위반건축물 …)
  안전   매각물건명세서가 작성돼 있고, 위 키워드가 하나도 없고, 선순위 임차인이 없음
  미확인  명세서가 아직 없거나(통상 매각기일 7일 전 작성) 상세 조회를 아직 못 함

한계 — 반드시 알고 쓸 것
  · 임차인의 '배당요구 여부'는 명세서 PDF 에만 있어 판정에 쓰지 못한다.
    → 선순위 임차인이 있으면 배당요구를 했더라도 '위험'으로 분류한다 (보수적).
  · 현황조사서의 전입 정보는 조사 시점 기준이며 누락될 수 있다.
  · 자동 판정은 참고용이다. 입찰 전 등기부등본·매각물건명세서 원문 확인은 필수.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
VERSION = 2          # 판정 규칙 버전 — 올리면 이미 확인한 물건도 다시 조회·판정한다

# ════════════════════════════════════════════════════════════
# 키워드 규칙
# ════════════════════════════════════════════════════════════
# (위험도, 표시 키워드, 정규식)
RIGHTS_RULES = [
    ("위험", "유치권", r"유치권"),
    ("위험", "법정지상권", r"법정\s*지상권"),
    ("위험", "분묘기지권", r"분묘\s*기지권"),
    ("위험", "선순위 가처분", r"(?:선순위|최선순위)[^.\n]{0,10}가처분|가처분[^.\n]{0,10}(?:인수|말소되지)"),
    ("위험", "예고등기", r"예고\s*등기"),
    ("위험", "대지권 미등기", r"대지권\s*(?:미등기|없음|없는)"),
    ("위험", "보증금 인수", r"(?:보증금|임차권|전세권)[^.\n]{0,30}인수|인수[^.\n]{0,10}(?:보증금|임차)"),
    ("위험", "대항력 있는 임차인", r"대항력\s*(?:이\s*)?있는\s*임차인|대항력\s*있음|대항할\s*수\s*있는"),
    ("위험", "지분 매각", r"지분\s*매각|공유\s*지분|지분\s*일괄"),
    # 대항력 포기 확약 (HUG 등이 우선변제권만 행사) — 등급 판정은 filters.rights_grade 에서
    ("포기", "대항력 포기", r"대항력[^.\n]{0,15}포기|우선변제권만\s*(?:을\s*)?(?:주장|행사)|인수\s*조건\s*변경"
                          r"|보증금\s*반환\s*청구권[^.\n]{0,10}포기|잔액[^.\n]{0,40}포기[^.\n]{0,30}임차권\s*등기\s*(?:를\s*)?말소"
                          r"|말소\s*동의\s*(?:의\s*)?확약서"),
    ("주의", "토지별도등기", r"(?:토지\s*)?별도\s*등기"),
    ("주의", "위반건축물", r"위반\s*건축물"),
    ("주의", "선순위 전세권", r"(?:선순위|최선순위)\s*전세권"),
    ("주의", "대항력 여지", r"대항력\s*(?:여부|여지)|대항력[^.\n]{0,6}(?:불분명|알\s*수\s*없)"),
    ("주의", "HUG 관련 조건", r"주택도시보증공사|HUG"),
    ("주의", "농지취득자격증명", r"농지\s*취득\s*자격\s*증명"),
    ("주의", "제시외 건물 매각제외", r"(?:매각에서\s*)?제외[^.\n]{0,12}제시\s*외|제시\s*외[^.\n]{0,12}(?:매각\s*)?제외"),
]
_COMPILED = [(lv, kw, re.compile(rx)) for lv, kw, rx in RIGHTS_RULES]
LEVELS = {kw: lv for lv, kw, _ in RIGHTS_RULES}
LEVELS.update({"선순위 권리": "위험", "인수되는 권리": "위험", "재매각": "주의", "우선매수신고": "주의",
               "맹지": "주의", "특별매각조건": "주의", "임차인 전입일 미상": "주의"})

# 바로 뒤에 '없음' 류가 오면 해당 없음으로 간주
_NEGATION = re.compile(r"^\s*(?:신고|성립|주장|여지)?\s*(?:여부\s*)?[은는이가도]?\s*"
                       r"(?:없음|없다|없습니다|없는|해당\s*(?:사항)?\s*없음|부존재|불성립|미해당|성립하지\s*않)")
# 명세서 양식 문구 (공통 안내문 — 스캔 전에 제거)
_BOILERPLATE = [
    re.compile(r"※[^※\n]*"),
    re.compile(r"매각에\s*따라\s*설정된\s*것으로\s*보는\s*지상권의\s*개요"),
    re.compile(r"등기된\s*부동산에\s*관한\s*권리\s*또는\s*가처분으로\s*매각으로\s*그\s*효력이\s*소멸되지\s*아니하는\s*것"),
    re.compile(r"인수되는\s*경우가\s*발생\s*할\s*수\s*있[^.\n]*"),
]
# 법원 특수조건 코드명 → 키워드
SPECIAL_TO_KEYWORD = {
    "법정지상권": "법정지상권", "별도등기": "토지별도등기", "유치권": "유치권", "분묘기지권": "분묘기지권",
    "재매각": "재매각", "농지취득": "농지취득자격증명", "예고등기": "예고등기", "선순위": "선순위 권리",
    "우선매수신고": "우선매수신고", "맹지": "맹지",
}
_EMPTY_TEXT = re.compile(r"^\s*(?:해당\s*(?:사항)?\s*없음|없음|-|None)?\s*$")
_DATE_RE = re.compile(r"(\d{4})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})")


def scan_text(text: str | None) -> dict[str, str]:
    """텍스트에서 권리 키워드 탐지 → {키워드: 위험도}"""
    if not text or not str(text).strip():
        return {}
    t = str(text)
    for bp in _BOILERPLATE:
        t = bp.sub(" ", t)
    t = re.sub(r"[ \t]+", " ", t)
    found: dict[str, str] = {}
    for level, kw, rx in _COMPILED:
        for m in rx.finditer(t):
            if _NEGATION.match(t[m.end(): m.end() + 25]):
                continue
            found.setdefault(kw, level)
            break
    return found


def scan_rights_text(text: str | None) -> tuple[str, list[str]]:
    """(이전 버전 호환) 텍스트 → (rights_risk, rights_keywords)"""
    if not text or len(str(text).strip()) < 20:
        return "미확인", []
    found = scan_text(text)
    return _risk_of(found, checked=True), _sorted(found)


def _risk_of(found: dict[str, str], checked: bool) -> str:
    if any(lv == "위험" for lv in found.values()):
        return "위험"
    if any(lv == "주의" for lv in found.values()):
        return "주의"
    return "안전" if checked else "미확인"


def _sorted(found: dict[str, str]) -> list[str]:
    order = {"위험": 0, "포기": 1, "주의": 2}
    return sorted(found, key=lambda k: (order.get(found[k], 9), k))


def parse_dates(text: str | None) -> list[str]:
    """텍스트 안의 날짜들 → ['2019-03-26', ...]"""
    out = []
    for y, m, d in _DATE_RE.findall(str(text or "")):
        try:
            out.append(f"{int(y):04d}-{int(m):02d}-{int(d):02d}")
        except ValueError:
            pass
    return [x for x in out if "1950-01-01" <= x <= "2100-01-01"]


def _norm_unit(s) -> str:
    """'2층 202호 ' → '2층202호' (호실 비교용)"""
    return re.sub(r"[\s,()]", "", str(s or ""))


# ════════════════════════════════════════════════════════════
# 1) 목록 정보만으로 판정
# ════════════════════════════════════════════════════════════
def listing_flags(item: dict) -> dict[str, str]:
    """special_conditions + remarks → {키워드: 위험도}"""
    found = scan_text(item.get("remarks"))
    for name in item.get("special_conditions") or []:
        kw = SPECIAL_TO_KEYWORD.get(name)
        if kw:
            found.setdefault(kw, LEVELS.get(kw, "주의"))
        elif name == "특별매각조건":
            # 대항력 포기 조건·재매각 보증금 20% 는 권리상 위험이 아님 → 그 외의 특별매각조건만 표시
            rm = item.get("remarks") or ""
            if "대항력 포기" not in found and not re.search(r"보증금[^.]{0,30}(?:\d+\s*%|\d할|10분의\s*\d)", rm):
                found.setdefault("특별매각조건", "주의")
    if item.get("is_share_sale"):
        found.setdefault("지분 매각", "위험")
    return found


# ════════════════════════════════════════════════════════════
# 2·3) 상세 + 현황조사서 분석
# ════════════════════════════════════════════════════════════
BID_RESULT = {"002": "유찰"}      # 확인된 코드만 표기 (그 외는 코드 그대로 둠)


def analyze(item: dict, detail: dict | None = None, curst: dict | None = None) -> dict:
    """
    물건 1건 권리분석 → 저장할 필드 dict
      rights_risk, rights_keywords, rights_basis(판정 근거), rights_has_spec, bid_history
    detail: 상세 API 의 data.dma_result / curst: 현황조사서 API 의 data
    """
    found = listing_flags(item)
    basis: dict = {}
    has_spec = False
    history = None

    info = (detail or {}).get("dspslGdsDxdyInfo") or {}
    if info:
        spec_date = info.get("gdsSpcfcWrtYmd")
        base_text = (info.get("tprtyRnkHypthcStngDts") or "").strip()
        has_spec = bool(spec_date and base_text)
        if spec_date:
            s = re.sub(r"\D", "", str(spec_date))
            basis["spec_date"] = f"{s[:4]}-{s[4:6]}-{s[6:8]}" if len(s) >= 8 else None
        if base_text:
            basis["base_right"] = re.sub(r"\s+", " ", base_text)[:160]      # 최선순위 설정 (말소기준권리)
        assumed = info.get("ndstrcRghCtt")
        if assumed and not _EMPTY_TEXT.match(str(assumed)):
            basis["assumed_rights"] = re.sub(r"\s+", " ", str(assumed))[:400]   # 매각으로 소멸되지 않는 권리
            # 항목별로 나눠 판정: '임차권등기(다만 HUG 의 말소동의 확약서가 제출됨)' 은 대항력 포기 조건,
            # 그 밖의 항목(대항할 수 있는 임차권·전세권·가처분 등)은 낙찰자가 인수하는 권리
            parts = [x.strip() for x in re.split(r"\n+|(?:^|\s)-\s+|(?:^|\s)\d+\.\s+", str(assumed)) if x and x.strip()]
            for part in parts or [str(assumed)]:
                f = scan_text(part)
                if "대항력 포기" in f:
                    found.setdefault("대항력 포기", "포기")
                    f = {k: v for k, v in f.items() if k in ("대항력 포기", "HUG 관련 조건")}
                else:
                    found.setdefault("인수되는 권리", "위험")
                found.update({k: v for k, v in f.items() if k not in found})
        surface = info.get("sprfcExstcDts")
        if surface and not _EMPTY_TEXT.match(str(surface)):
            basis["surface_rights"] = re.sub(r"\s+", " ", str(surface))[:300]   # 지상권 개요
            found.setdefault("법정지상권", "위험")
        for key in ("gdsSpcfcRmk", "dspslGdsRmk"):
            txt = info.get(key)
            if txt and not _EMPTY_TEXT.match(str(txt)):
                basis.setdefault("spec_remarks", re.sub(r"\s+", " ", str(txt)).strip()[:500])
                found.update({k: v for k, v in scan_text(txt).items() if k not in found})
        # 기일 내역 (매각기일만)
        hist = []
        for h in (detail or {}).get("gdsDspslDxdyLst") or []:
            if not isinstance(h, dict) or h.get("auctnDxdyKndCd") != "01":
                continue
            d = re.sub(r"\D", "", str(h.get("dxdyYmd") or ""))
            code = h.get("auctnDxdyRsltCd")
            hist.append({"date": f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) >= 8 else None,
                         "min_bid": h.get("tsLwsDspslPrc") or None,
                         "result": BID_RESULT.get(code, code) if code else "예정"})
        history = hist or None

    # 임차인 (현황조사서)
    if curst is not None:
        tenants_all = [t for t in (curst.get("dlt_ordTsLserLtn") or []) if isinstance(t, dict)]
        unit = _norm_unit(item.get("unit"))
        if unit:
            tenants = [t for t in tenants_all if _norm_unit(t.get("bldDtlDts")) == unit
                       or (not _norm_unit(t.get("bldDtlDts")) and unit.endswith(_norm_unit(t.get("lesPartCtt")) or "\0"))]
        else:
            tenants = tenants_all            # 단독·다가구: 건물 전체 임차인
        base_dates = parse_dates(basis.get("base_right"))
        # 최선순위 설정일이 여러 개(토지/건물)면 가장 늦은 날짜 기준 → 임차인을 더 넓게 선순위로 본다 (보수적)
        base_date = max(base_dates) if base_dates else None
        senior = unknown = 0
        for t in tenants:
            mv = parse_dates(t.get("mvinDtlCtt"))
            if not mv:
                unknown += 1
            elif base_date and min(mv) <= base_date:
                senior += 1
        basis["tenant_count"] = len(tenants)
        basis["base_date"] = base_date
        if tenants:
            basis["tenant_move_in"] = sorted({d for t in tenants for d in parse_dates(t.get("mvinDtlCtt"))})[:10]
        if senior:
            basis["senior_tenant_count"] = senior
            found.setdefault("대항력 있는 임차인", "위험")
        if unknown:
            found.setdefault("임차인 전입일 미상", "주의")
        if tenants and not base_date:
            has_spec = False                 # 기준일이 없으면 선순위 여부를 판단할 수 없음

    checked = has_spec and curst is not None
    return {
        "rights_version": VERSION,
        "rights_risk": _risk_of(found, checked),
        "rights_keywords": _sorted(found),
        "rights_has_spec": has_spec,
        "rights_basis": basis or None,
        "bid_history": history,
    }


# ════════════════════════════════════════════════════════════
# 상세·현황조사서 조회 (브라우저 세션 안에서 API 호출)
# ════════════════════════════════════════════════════════════
_JS_POST = """async (arg) => {
    try {
        var resp = await fetch(arg.url, {method:'POST', headers: arg.headers, body: JSON.stringify(arg.body), credentials:'include'});
        var d = JSON.parse(await resp.text());
        // 사진(base64)은 버림 — 응답의 대부분을 차지
        (function strip(o){ if (o && typeof o === 'object') { for (var k in o) {
            if (k === 'picFile' || k === 'csPicLst') delete o[k]; else strip(o[k]); } } })(d);
        return {ok:true, status:resp.status, data:d.data || null};
    } catch(e) { return {ok:false, reason:String(e)}; }
}"""


def fetch_court_details(targets: list[dict], log=print, max_fail_streak: int = 5) -> dict[str, dict]:
    """
    targets: 물건 dict 목록 (case_no, court_code, item_no, id 필요)
    반환: {id: {"detail": dma_result|None, "curst": data|None}}  — 조회에 실패한 물건은 포함하지 않음
    """
    if not targets:
        return {}
    try:
        from playwright.sync_api import sync_playwright
        import collect_court as C
    except ImportError as e:
        log(f"  권리분석 조회 생략: {e}")
        return {}
    out: dict[str, dict] = {}
    curst_cache: dict[tuple, dict | None] = {}
    fails = 0
    with sync_playwright() as p:
        try:
            browser, page = C.open_search_page(p)
        except RuntimeError as e:
            log(f"  권리분석 조회 생략 (사이트 접속 실패): {e}")
            return {}
        si = dict(C.REQUEST_TEMPLATE["dma_srchGdsDtlSrchInfo"])
        hdr = dict(C.REQUEST_HEADERS)
        for n, it in enumerate(targets, 1):
            if fails >= max_fail_streak:
                log(f"  상세 조회 연속 {fails}회 실패 — 이번 실행 중단")
                break
            body = {"dma_srchGdsDtlSrch": {"csNo": it["case_no"], "cortOfcCd": it["court_code"],
                                           "dspslGdsSeq": str(it.get("item_no") or 1), "pgmId": "PGJ151F01", "srchInfo": si}}
            detail = None
            for attempt in (1, 2):
                try:
                    r = page.evaluate(_JS_POST, {"url": "/pgj/pgj15B/selectAuctnCsSrchRslt.on", "body": body,
                                                 "headers": {**hdr, "submissionid": "mf_wfm_mainFrame_sbm_selectGdsDtlSrchDtlInfo"}})
                except Exception as e:
                    r = {"ok": False, "reason": type(e).__name__}
                detail = ((r.get("data") or {}).get("dma_result") or None) if r.get("ok") else None
                page.wait_for_timeout(900)
                if detail and detail.get("dspslGdsDxdyInfo"):
                    break
                detail = None
                page.wait_for_timeout(2500)
            if detail is None:
                fails += 1
                continue
            fails = 0
            ck = (it["court_code"], it["case_no"])
            if ck not in curst_cache:
                try:
                    c = page.evaluate(_JS_POST, {"url": "/pgj/pgj15B/selectCurstExmndc.on",
                                                 "headers": {**hdr, "submissionid": "mf_wfm_mainFrame_sbm_selectCurstExmndc"},
                                                 "body": {"dma_srchCurstExmn": {"cortOfcCd": it["court_code"], "csNo": it["case_no"],
                                                                                "auctnInfOriginDvsCd": "2", "ordTsCnt": ""}}})
                except Exception as e:
                    c = {"ok": False, "reason": type(e).__name__}
                data = c.get("data") if c.get("ok") else None
                # 현황조사서가 비어 있으면(조사 전) None 으로 둠
                curst_cache[ck] = data if (data and data.get("dma_curstExmnMngInf")) else None
                page.wait_for_timeout(700)
            out[it["id"]] = {"detail": detail, "curst": curst_cache[ck]}
            if n % 25 == 0:
                log(f"  … 상세 조회 {n}/{len(targets)}건")
        browser.close()
    return out
