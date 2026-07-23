#!/usr/bin/env python3
"""
법원경매정보 스크래퍼 v8
────────────────────────────────────────────────────────────
핵심 발견:
  - 사이트는 index.on 에서 SPA로 동작
  - '물건상세검색' (id=mf_wfm_header_anc_auctnGdsMain) 클릭 →
    WebSquare 내부에서 경매 검색 폼 로드 → 자동검색 or 검색버튼
  - 시도 코드(rprsAdongSdCd 등)는 서버에서 실제 필터링 안 됨
  - cortOfcCd(법원코드)가 실제 필터 키 → 법원 코드별로 검색해야 함

흐름:
  1. index.on 로드 + WebSquare 초기화 대기
  2. 물건상세검색 클릭
  3. 검색 폼 로드 대기 (20초)
  4. 법원 드롭다운에서 수도권 법원 코드 추출
  5. 서울 법원 검색으로 XHR 캡처
  6. 수도권 각 법원코드로 fetch() 재실행
"""

import json, os, time, sys
from datetime import date

BASE       = "https://www.courtauction.go.kr"
TODAY      = date.today().isoformat()
OUT        = os.path.join(os.path.dirname(__file__), "..", "docs", "data", "auctions.json")
MIN_BID    = 50_000_000   # 5천만원 이상 (유찰로 낮아진 물건도 포함)
METRO_SIDO = {"11", "28", "41"}

# 수도권 법원 키워드 (이름으로 구분)
SEOUL_KW   = ["서울"]
GYEONGGI_KW = ["수원","의정부","성남","부천","안산","안양","평택","여주","인천"]
# 인천은 경기 광역권에 포함해서 처리

# WebSquare 보다 먼저 주입 — XHR 후크 (요청 본문도 캡처)
INIT_SCRIPT = """
window.__auction_captured = [];
window.__auction_last_req = null;
(function() {
    var _origOpen = XMLHttpRequest.prototype.open;
    var _origSend = XMLHttpRequest.prototype.send;
    var _origSetHdr = XMLHttpRequest.prototype.setRequestHeader;

    XMLHttpRequest.prototype.open = function(method, url, async) {
        this.__iurl = (typeof url === 'string') ? url : '';
        this.__method = method;
        this.__hdrs = {};
        return _origOpen.apply(this, arguments);
    };

    XMLHttpRequest.prototype.setRequestHeader = function(name, value) {
        this.__hdrs = this.__hdrs || {};
        this.__hdrs[name] = value;
        return _origSetHdr.apply(this, arguments);
    };

    XMLHttpRequest.prototype.send = function(body) {
        var self = this;
        if ((self.__iurl || '').includes('searchControllerMain')) {
            window.__auction_last_req = {
                url: self.__iurl,
                method: self.__method || 'POST',
                body: (typeof body === 'string') ? body : '',
                headers: self.__hdrs || {}
            };
        }
        self.addEventListener('loadend', function() {
            if ((self.__iurl || '').includes('searchControllerMain')) {
                try {
                    var d = JSON.parse(self.responseText);
                    if (d && d.data && d.data.dlt_srchResult) {
                        window.__auction_captured.push(d);
                        console.log('[HOOK] 캡처! ' + (d.data.dlt_srchResult.length) + '건');
                    }
                } catch(e) {}
            }
        });
        return _origSend.apply(this, arguments);
    };
})();
"""

def parse_row(row):
    case_no = (row.get("srnSaNo") or "").strip()
    if not case_no:
        return None
    addr = (row.get("printSt") or "").strip()
    if not addr:
        addr = " ".join(filter(None, [
            row.get("hjguSido",""), row.get("hjguSigu",""), row.get("hjguDong","")
        ]))
    try:    appraisal = int(row.get("gamevalAmt") or 0) or None
    except: appraisal = None
    try:    min_bid = int(row.get("minmaePrice") or 0) or None
    except: min_bid = None
    d = row.get("maeGiil", "")
    auction_date = f"{d[:4]}-{d[4:6]}-{d[6:8]}" \
                   if (d and len(d) == 8 and d.isdigit()) else None
    try:    failed = int(row.get("yuchalCnt") or 0)
    except: failed = 0
    return {
        "id":            case_no,
        "court":         row.get("jiwonNm", ""),
        "address":       addr,
        "property_type": row.get("dspslUsgNm", ""),
        "appraisal":     appraisal,
        "min_bid":       min_bid,
        "auction_date":  auction_date,
        "failed_bids":   failed,
        "bid_ratio":     round(min_bid / appraisal * 100, 1)
                         if (min_bid and appraisal) else None,
        "scraped_date":  TODAY,
    }

EXCL_LAND = ["대지", "임야", "공장", "창고", "주차장"]
RESID_KW  = ["아파트", "다가구", "다세대", "연립", "빌라", "오피스텔", "단독주택"]

def is_target(row):
    # 순수 상업용·토지만 제외, 다가구·빌라·오피스텔은 포함
    ptype = (row.get("dspslUsgNm") or "").strip()
    if ptype:
        has_res = any(k in ptype for k in RESID_KW)
        if not has_res:
            if any(k in ptype for k in EXCL_LAND):
                return False
            segs = [s.strip() for s in ptype.split(',')]
            if all(any(k in s for k in ["근린시설", "상가"]) for s in segs):
                return False
    sido = row.get("daepyoSidoCd") or row.get("srchHjguSidoCd") or ""
    if sido not in METRO_SIDO:
        addr = row.get("printSt","") or row.get("hjguSido","")
        if not any(k in addr for k in ["서울","인천","경기"]):
            return False
    try:
        return int(row.get("minmaePrice") or 0) >= MIN_BID
    except:
        return False

def get_captured(page):
    return page.evaluate("() => window.__auction_captured || []")

def get_all_courts_from_page(page, frame):
    """페이지에서 가능한 모든 방법으로 법원 코드 추출 (디버그용)"""
    result = frame.evaluate("""() => {
        var found = [];

        // 1. select DOM 탐색 — 모든 select의 id와 옵션 수 출력
        var sels = document.querySelectorAll('select');
        var selInfo = [];
        for (var sel of sels) {
            selInfo.push({id: sel.id, name: sel.name, optCount: sel.options.length,
                firstVals: Array.from(sel.options).slice(0,3).map(o=>o.value+'='+o.text)});
        }

        // 2. select에서 법원 관련 옵션 탐색
        for (var sel of sels) {
            var id = (sel.id || sel.name || '').toLowerCase();
            var opts = [];
            for (var opt of sel.options) {
                var v = opt.value.trim(), t = opt.text.trim();
                if (v && (v.startsWith('B') || /지방법원|지원/.test(t))) {
                    opts.push({code:v, name:t});
                }
            }
            if (opts.length >= 3) {
                return {method:'select', sel_id:sel.id, opts:opts, selInfo:selInfo};
            }
        }

        // 3. WebSquare 데이터셋 탐색 (scwin 내 DataList, ComboBox)
        if (typeof scwin !== 'undefined') {
            var keys = Object.getOwnPropertyNames(scwin);
            for (var k of keys) {
                try {
                    var obj = scwin[k];
                    if (obj && typeof obj.getJSON === 'function') {
                        var d = obj.getJSON();
                        if (d && JSON.stringify(d).includes('지방법원')) {
                            return {method:'scwin:'+k, raw: JSON.stringify(d).slice(0,500)};
                        }
                    }
                } catch(e) {}
            }
        }

        // 4. window 전역 변수 탐색
        var winKeys = Object.getOwnPropertyNames(window);
        for (var k of winKeys) {
            try {
                var v = window[k];
                if (v && typeof v === 'object' && !Array.isArray(v)) {
                    var s = JSON.stringify(v);
                    if (s && s.includes('지방법원') && s.includes('B000')) {
                        return {method:'window:'+k, raw:s.slice(0,800)};
                    }
                }
            } catch(e) {}
        }

        return {method:'none', selInfo:selInfo};
    }""")
    return result

def get_metro_courts(page, frame):
    """
    법원 드롭다운에서 수도권 법원 코드 목록 추출.
    반환: [{"code": "B000210", "name": "서울중앙지방법원", "region": "서울"}, ...]
    """
    # 전체 탐색 실행
    raw = get_all_courts_from_page(page, frame)
    method = raw.get('method','none') if raw else 'none'
    print(f"  [법원코드 탐색] method={method}")

    result = None
    if raw and raw.get('opts'):
        result = raw  # select에서 발견

    if not result:

        # select 옵션 정보 출력 (디버그)
        sel_info = raw.get('selInfo', []) if raw else []
        print(f"  [법원코드] 페이지 select 목록 ({len(sel_info)}개):")
        for s in sel_info:
            print(f"    id={s.get('id')} name={s.get('name')} opts={s.get('optCount')} sample={s.get('firstVals')}")
        if raw and raw.get('raw'):
            print(f"  [법원코드] 힌트 데이터: {raw.get('raw','')[:300]}")
        print("  [법원코드] 드롭다운 추출 실패 — 기본 코드 사용")
        return DEFAULT_METRO_COURTS

    print(f"  [법원코드] select id={result.get('sel_id')}, 총 {len(result['opts'])}개 발견")
    courts = []
    for opt in result["opts"]:
        nm = opt["name"]
        region = None
        if "서울" in nm:
            region = "서울"
        elif any(k in nm for k in ["수원","성남","부천","안산","안양","평택","여주"]):
            region = "경기"
        elif "의정부" in nm:
            region = "경기"
        elif "인천" in nm:
            region = "인천"
        if region:
            courts.append({"code": opt["code"], "name": nm, "region": region})
            print(f"    {opt['code']} = {nm} [{region}]")

    if not courts:
        print("  [법원코드] 수도권 법원 없음 — 기본 코드 사용")
        return DEFAULT_METRO_COURTS

    return courts

# ── 알려진 수도권 법원 코드 (드롭다운 추출 실패 시 fallback) ──────────
# courtauction.go.kr 법원 select에서 확인한 실제 코드
DEFAULT_METRO_COURTS = [
    # 서울
    {"code": "B000210", "name": "서울중앙지방법원",  "region": "서울"},
    {"code": "B000215", "name": "서울동부지방법원",  "region": "서울"},
    {"code": "B000220", "name": "서울남부지방법원",  "region": "서울"},
    {"code": "B000225", "name": "서울북부지방법원",  "region": "서울"},
    {"code": "B000230", "name": "서울서부지방법원",  "region": "서울"},
    # 경기
    {"code": "B000260", "name": "수원지방법원",      "region": "경기"},
    {"code": "B000261", "name": "수원지방법원 성남지원", "region": "경기"},
    {"code": "B000262", "name": "수원지방법원 여주지원", "region": "경기"},
    {"code": "B000263", "name": "수원지방법원 평택지원", "region": "경기"},
    {"code": "B000264", "name": "수원지방법원 안산지원", "region": "경기"},
    {"code": "B000265", "name": "수원지방법원 안양지원", "region": "경기"},
    {"code": "B000270", "name": "의정부지방법원",    "region": "경기"},
    {"code": "B000271", "name": "의정부지방법원 고양지원", "region": "경기"},
    # 인천
    {"code": "B000250", "name": "인천지방법원",      "region": "인천"},
    {"code": "B000251", "name": "인천지방법원 부천지원", "region": "인천"},
]

def replay_with_court(page, court_code, court_name, page_num=1):
    """
    캡처된 마지막 XHR 요청 body(JSON)에서 cortOfcCd(법원코드)/page만 교체 후 fetch() 재실행.
    시도 코드는 실제 서버 필터링에 영향 없음 → cortOfcCd로만 지역 필터링.
    """
    cc = court_code
    pg = page_num
    result = page.evaluate(f"""async () => {{
        var req = window.__auction_last_req;
        if (!req || !req.url || !req.body) return {{ok:false, reason:'no_req'}};

        var bodyObj;
        try {{ bodyObj = JSON.parse(req.body); }}
        catch(e) {{ return {{ok:false, reason:'json_parse:'+e}}; }}

        var si = bodyObj.dma_srchGdsDtlSrchInfo;
        if (si) {{
            si.cortOfcCd = '{cc}';
            // sido 필드 초기화 (법원 코드로만 필터링)
            si.rprsAdongSdCd           = '';
            si.rdnmSdCd                = '';
            si.rprsAdongSggCd          = '';
            si.rprsAdongEmdCd          = '';
            si.rdnmSggCd               = '';
            si.rdnmNo                  = '';
            si.mvprpDspslPlcAdongSdCd  = '';
            si.mvprpDspslPlcAdongSggCd = '';
            si.mvprpDspslPlcAdongEmdCd = '';
            si.rdDspslPlcAdongSdCd     = '';
            si.rdDspslPlcAdongSggCd    = '';
            si.rdDspslPlcAdongEmdCd    = '';
        }}
        if (bodyObj.dma_pageInfo) {{
            bodyObj.dma_pageInfo.pageNo   = {pg};
            bodyObj.dma_pageInfo.bfPageNo = String({pg} - 1);
            bodyObj.dma_pageInfo.totalCnt = '';
        }}

        var hdrs = Object.assign({{}}, req.headers);
        try {{
            var controller = new AbortController();
            var tid = setTimeout(function(){{ controller.abort(); }}, 20000);
            var resp = await fetch(req.url, {{
                method: req.method || 'POST',
                headers: hdrs,
                body: JSON.stringify(bodyObj),
                credentials: 'include',
                signal: controller.signal
            }});
            clearTimeout(tid);
            var text = await resp.text();
            var d = JSON.parse(text);
            if (d && d.data && d.data.dlt_srchResult) {{
                window.__auction_captured.push(d);
                return {{ok:true, count:d.data.dlt_srchResult.length,
                        total:(d.data.dma_pageInfo||{{}}).totalCnt||0}};
            }}
            return {{ok:false, reason:'no_data', keys:Object.keys(d.data||{{}})}};
        }} catch(e) {{
            return {{ok:false, reason:String(e)}};
        }}
    }}""")
    print(f"  [{court_name}] replay p={page_num}: {result}")
    return result

def get_search_frame(page):
    """검색 폼이 있는 frame 반환 (메인 또는 iframe)"""
    # 메인 페이지에서 scwin 확인
    has_scwin = page.evaluate("() => typeof scwin !== 'undefined' && Object.keys(scwin).length > 0")
    if has_scwin:
        return page

    # iframe 순회 — scwin 또는 검색 폼이 있는 frame 찾기
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        try:
            ok = frame.evaluate("""() => {
                if (typeof scwin !== 'undefined' && Object.keys(scwin).length > 0) return true;
                if (document.querySelector('select[id*="Sido"],select[id*="sido"],[id*="slcSido"]')) return true;
                if (document.querySelector('[id*="btn"][id*="srch"],[id*="btn"][id*="Srch"]')) return true;
                return false;
            }""")
            if ok:
                print(f"  → 검색 frame 발견: {frame.url[:60]}")
                return frame
        except:
            pass
    return page  # 못찾으면 메인 페이지

def set_sido_in_frame(frame, sido_code, sido_name):
    """frame 안에서 시도 드롭다운 설정"""
    result = frame.evaluate(f"""() => {{
        // 1. select 직접 탐색
        var sels = ['select[id*="Sido"]','select[id*="sido"]','[id*="slcSido"]',
                    '[id*="cmbSido"]','select[name*="sido"]'];
        for (var s of sels) {{
            var el = document.querySelector(s);
            if (el) {{
                el.value = '{sido_code}';
                el.dispatchEvent(new Event('change', {{bubbles:true}}));
                return 'select:' + (el.id || s);
            }}
        }}
        // 2. WebSquare scwin 함수 탐색
        if (typeof scwin !== 'undefined') {{
            var fns = Object.getOwnPropertyNames(scwin);
            for (var fn of fns) {{
                if (/sido/i.test(fn)) {{
                    try {{ scwin[fn]('{sido_code}'); return 'scwin:'+fn; }} catch(e) {{}}
                }}
            }}
        }}
        // 3. 모든 select에서 "{sido_code}" 옵션 찾기
        var allSel = document.querySelectorAll('select');
        for (var sel of allSel) {{
            for (var opt of sel.options) {{
                if (opt.value === '{sido_code}') {{
                    sel.value = '{sido_code}';
                    sel.dispatchEvent(new Event('change', {{bubbles:true}}));
                    return 'auto:' + (sel.id||'?');
                }}
            }}
        }}
        return 'not_found';
    }}""")
    print(f"  [{sido_name}] 시도 설정: {result}")
    return result != 'not_found'

def try_trigger_search(page, frame=None):
    """검색 트리거: frame 우선, 없으면 page"""
    f = frame or page

    # scwin 함수 시도
    fns = f.evaluate("""() => {
        if (typeof scwin === 'undefined') return [];
        return Object.getOwnPropertyNames(scwin)
            .filter(k => typeof scwin[k] === 'function');
    }""")
    print(f"  scwin 함수: {fns[:10]}")

    search_kw = ['search','srch','조회','srchGds','mulSrch','selMul','list','List']
    for fn in fns:
        if any(kw.lower() in fn.lower() for kw in search_kw):
            try:
                r = f.evaluate(f"() => {{ try {{ scwin['{fn}'](); return 'ok'; }} catch(e) {{ return String(e); }} }}")
                print(f"  → scwin.{fn}() = {r}")
                page.wait_for_timeout(3000)
                if get_captured(page): return True
            except: pass

    for fn in fns[:5]:
        try:
            f.evaluate(f"() => {{ try {{ scwin['{fn}'](); }} catch(e) {{}} }}")
            page.wait_for_timeout(2000)
            if get_captured(page): return True
        except: pass

    # DOM 버튼 클릭 (frame + page 모두 시도)
    for target in ([f, page] if f != page else [page]):
        for selector in [
            "button:has-text('조회')", "button:has-text('검색')",
            "input[value='조회']", "input[value='검색']",
            "a:has-text('조회')", "[id*='btn'][id*='srch']",
            "[id*='btn'][id*='Srch']", "[id*='btn'][id*='search']",
        ]:
            try:
                elem = target.locator(selector).first
                if elem.is_visible():
                    elem.click()
                    print(f"  → 클릭: {selector}")
                    page.wait_for_timeout(5000)
                    if get_captured(page): return True
            except: pass

    return False

def main():
    print(f"=== 경매 스크래퍼 v8 시작: {TODAY} ===\n")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("오류: pip3 install playwright && python3 -m playwright install chromium")
        sys.exit(1)

    all_raw_rows = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
        )

        # ★ WebSquare 보다 먼저 XHR 후크 주입
        context.add_init_script(INIT_SCRIPT)
        page = context.new_page()

        # ── 1. 메인 페이지 로드 ──────────────────────────────
        print("[1단계] 메인 페이지 로드 (10초)...")
        page.goto(BASE + "/pgj/index.on", wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(10000)
        print(f"  URL: {page.url}")
        print(f"  제목: {page.title()}")

        # ── 2. 물건상세검색 클릭 ─────────────────────────────
        print("\n[2단계] 물건상세검색 클릭...")
        try:
            page.click("#mf_wfm_header_anc_auctnGdsMain")
            print("  → 클릭 성공!")
        except Exception as e:
            print(f"  → ID 클릭 실패: {e}")
            # 텍스트로 재시도
            try:
                page.click("a:has-text('물건상세검색')")
                print("  → 텍스트 클릭 성공!")
            except Exception as e2:
                print(f"  → 텍스트 클릭도 실패: {e2}")

        # ── 3. 검색 폼 로드 대기 ─────────────────────────────
        print("\n[3단계] 검색 폼 로드 대기 (20초)...")
        page.wait_for_timeout(20000)

        # ── 4. 검색 frame 탐지 ───────────────────────────────
        print("\n[4단계] 검색 frame 탐지...")
        # iframe 목록 출력 (디버그)
        frame_urls = [f.url[:80] for f in page.frames if f != page.main_frame]
        print(f"  iframe 수: {len(frame_urls)}")
        for u in frame_urls:
            print(f"    {u}")
        search_frame = get_search_frame(page)

        # ── 5. 첫 번째 검색 (서울) — XHR 캡처용 ──────────────
        print("\n[5단계] 서울 최초 검색으로 XHR 요청 캡처...")
        page.evaluate("() => { window.__auction_captured = []; window.__auction_last_req = null; }")

        # 시도 드롭다운 설정 시도 (실패해도 계속)
        set_sido_in_frame(search_frame, "11", "서울")
        page.wait_for_timeout(1500)

        # 검색 트리거
        try_trigger_search(page, search_frame)
        page.wait_for_timeout(8000)

        # 첫 번째 XHR 캡처 확인
        first_captured = get_captured(page)
        last_req = page.evaluate("() => window.__auction_last_req")
        print(f"  캡처된 응답: {len(first_captured)}개")
        print(f"  캡처된 요청: {'있음 (' + last_req['url'][:60] + ')' if last_req else '없음'}")

        # ── 6. 법원 코드 목록 추출 ─────────────────────────────
        print("\n[6단계] 수도권 법원 코드 추출...")
        metro_courts = get_metro_courts(page, search_frame)
        print(f"  → 수도권 법원 {len(metro_courts)}개 (서울/경기/인천)")

        # ── 7. 법원별 fetch() 직접 재실행 ──────────────────────
        all_xhr_rows = []
        seen_ids = set()  # 중복 방지

        for court in metro_courts:
            court_code = court["code"]
            court_name = court["name"]
            region     = court["region"]

            print(f"\n[7단계] {court_name} ({region}) 데이터 수집...")
            page.evaluate("() => { window.__auction_captured = []; }")

            if last_req:
                try:
                    r = replay_with_court(page, court_code, court_name, page_num=1)
                except Exception as e:
                    print(f"  [{court_name}] p=1 오류: {e}, 스킵")
                    continue
                page.wait_for_timeout(2000)
            else:
                print(f"  [{court_name}] XHR 요청 없음, 스킵")
                continue

            captured_now = get_captured(page)
            if not captured_now:
                print(f"  [{court_name}] 캡처 없음, 스킵")
                continue

            first = captured_now[0]
            total_cnt = int((first.get("data",{}).get("dma_pageInfo") or {}).get("totalCnt") or 0)
            page_size = int((first.get("data",{}).get("dma_pageInfo") or {}).get("pageSize") or 10)

            if total_cnt == 0:
                # 이 법원에 물건 없음 (코드 오류 또는 진짜 없음)
                first_rows = first.get("data",{}).get("dlt_srchResult") or []
                if not first_rows:
                    print(f"  [{court_name}] 결과 없음 (totalCnt=0), 스킵")
                    continue
                total_cnt = len(first_rows)

            total_pages = max(1, (total_cnt + page_size - 1) // page_size) if total_cnt else 1
            print(f"  [{court_name}] 총 {total_cnt}건 / {total_pages}페이지")

            court_rows = []
            first_rows = first.get("data",{}).get("dlt_srchResult") or []
            for row in first_rows:
                rid = row.get("srnSaNo","")
                if rid and rid not in seen_ids:
                    seen_ids.add(rid)
                    court_rows.append(row)
            print(f"  p=1 → {len(first_rows)}건 (신규 {len(court_rows)}건)")

            # 2페이지~
            for pg in range(2, min(total_pages + 1, 100)):
                page.evaluate("() => { window.__auction_captured = []; }")
                try:
                    replay_with_court(page, court_code, court_name, page_num=pg)
                except Exception as e:
                    print(f"  p={pg} evaluate 오류: {e}, 종료")
                    break
                page.wait_for_timeout(2000)

                new_cap = get_captured(page)
                if not new_cap:
                    print(f"  p={pg} 응답 없음, 종료")
                    break
                rows = new_cap[-1].get("data",{}).get("dlt_srchResult") or []
                if not rows:
                    print(f"  p={pg} 결과 없음, 종료")
                    break
                new_count = 0
                for row in rows:
                    rid = row.get("srnSaNo","")
                    if rid and rid not in seen_ids:
                        seen_ids.add(rid)
                        court_rows.append(row)
                        new_count += 1
                print(f"  p={pg} → {len(rows)}건 (신규 {new_count}건, 누적 {len(court_rows)}건)")
                if len(court_rows) >= total_cnt:
                    break

            print(f"  [{court_name}] 수집 완료: {len(court_rows)}건")
            all_xhr_rows.extend(court_rows)

        # 법원별 요약
        from collections import Counter as _Counter
        court_summary = _Counter(r.get('jiwonNm','') for r in all_xhr_rows)
        print("\n  [법원별 수집 요약]")
        for cn, cv in court_summary.most_common():
            print(f"    {cn}: {cv}건")
        print(f"\n  [전체] 수집 완료: {len(all_xhr_rows)}건 (서울+경기+인천)")
        captured = all_xhr_rows

        context.close()
        browser.close()
        print("\n[브라우저 종료]")

    # ── 데이터 처리 ──────────────────────────────────────────
    # captured 는 with 블록 안에서 list(rows) 또는 list(cap_dict) 중 하나
    raw = captured if 'captured' in dir() else []
    if raw and isinstance(raw[0], dict) and "srnSaNo" in raw[0]:
        # XHR 페이지네이션 결과 (row 직접)
        all_raw_rows = raw
        print(f"\n[데이터] 총 {len(all_raw_rows)}건")
    else:
        for cap in raw:
            items = cap.get("data", {}).get("dlt_srchResult") or []
            total = cap.get("data", {}).get("dma_pageInfo", {}).get("totalCnt", 0)
            print(f"\n[데이터] {len(items)}건 / 총{total}건")
            all_raw_rows.extend(items)

    if not all_raw_rows:
        print("\n=== 데이터 없음 ===")
        return

    # ── 디버그: 첫 번째 row 필드 확인 ──
    sample = all_raw_rows[0]
    print(f"\n[디버그] 첫 번째 row 키: {list(sample.keys())[:20]}")
    print(f"  srnSaNo={sample.get('srnSaNo')} minmaePrice={sample.get('minmaePrice')} "
          f"dspslUsgNm={sample.get('dspslUsgNm')} daepyoSidoCd={sample.get('daepyoSidoCd')}")
    # 실패 원인 분석 (처음 5건)
    fail_stats = {"ptype":0, "sido":0, "price":0, "pass":0}
    for row in all_raw_rows[:200]:
        ptype = (row.get("dspslUsgNm") or "").strip()
        if ptype:
            has_res = any(k in ptype for k in RESID_KW)
            if not has_res:
                if any(k in ptype for k in EXCL_LAND) or all(any(k in s for k in ["근린시설","상가"]) for s in [s.strip() for s in ptype.split(',')]):
                    fail_stats["ptype"] += 1; continue
        sido = row.get("daepyoSidoCd") or row.get("srchHjguSidoCd") or ""
        if sido not in METRO_SIDO:
            addr = row.get("printSt","") or row.get("hjguSido","")
            if not any(k in addr for k in ["서울","인천","경기"]):
                fail_stats["sido"] += 1; continue
        try:
            bid = int(row.get("minmaePrice") or 0)
            if bid < MIN_BID: fail_stats["price"] += 1
            else: fail_stats["pass"] += 1
        except: fail_stats["price"] += 1
    print(f"  [첫 200건 분석] 업종필터={fail_stats['ptype']} 지역필터={fail_stats['sido']} "
          f"가격미달={fail_stats['price']} 통과={fail_stats['pass']}")

    new_items = {}
    for row in all_raw_rows:
        if is_target(row):
            item = parse_row(row)
            if item and item["id"] not in new_items:
                new_items[item["id"]] = item
                price = (item["min_bid"] or 0) // 10000
                print(f"  ✓ {item['id']} [{item['property_type']}] "
                      f"{item['address'][:25]} {price:,}만원")

    # 기존 데이터 로드 (오늘 이전 경매는 자동 제거)
    existing = {}
    if os.path.exists(OUT):
        try:
            with open(OUT) as f:
                for i in json.load(f).get("auctions", []):
                    if (i.get("auction_date") or "") >= TODAY:  # 오늘 포함 미래만 유지
                        existing[i["id"]] = i
            print(f"  [기존] {len(existing)}건 (지난 경매 자동 제거)")
        except:
            pass

    # 기존 매물은 최초 수집일(scraped_date) 유지 — 진짜 신규 매물만 오늘 날짜
    merged = {**existing}
    for k, v in new_items.items():
        if k in existing:
            v["scraped_date"] = existing[k]["scraped_date"]  # 원래 날짜 보존
        merged[k] = v
    final = sorted(merged.values(),
                   key=lambda x: (x.get("scraped_date",""), x.get("auction_date","")),
                   reverse=True)

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"updated": TODAY, "auctions": final}, f, ensure_ascii=False, indent=2)

    print(f"\n[결과] 신규 {len(new_items)}건")
    print(f"[저장] {OUT} — 총 {len(final)}건")
    print("=== 완료 ===")


if __name__ == "__main__":
    main()
