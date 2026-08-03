#!/usr/bin/env python3
"""
청약홈 스크래퍼 v2
────────────────────────────────────────────────
applyhome.co.kr 에서 수도권 청약 분양 정보를 수집합니다.
직접 fetch() 대신 페이지가 스스로 보내는 XHR을 후킹해서 캡처합니다.

실행: python3 scraper/scrape_subscriptions.py
출력: docs/data/cheongyak.json
"""

import json, os, sys, re
from datetime import date

BASE  = "https://www.applyhome.co.kr"
TODAY = date.today().isoformat()
OUT   = os.path.join(os.path.dirname(__file__), "..", "docs", "data", "cheongyak.json")

METRO = ["서울", "경기", "인천"]

# 모든 XHR/fetch 응답을 캡처하는 후크
INIT_SCRIPT = """
window.__cy_reqs  = [];   // 요청 목록 {url, method, body, headers}
window.__cy_resps = [];   // 응답 목록 {url, body}

(function() {
    // ── XMLHttpRequest 후크 ──────────────────────────────────
    var _open = XMLHttpRequest.prototype.open;
    var _send = XMLHttpRequest.prototype.send;
    var _setH = XMLHttpRequest.prototype.setRequestHeader;

    XMLHttpRequest.prototype.open = function(m, u) {
        this.__u = u; this.__m = m; this.__h = {};
        return _open.apply(this, arguments);
    };
    XMLHttpRequest.prototype.setRequestHeader = function(n,v) {
        this.__h[n]=v; return _setH.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function(body) {
        var self = this;
        var u = self.__u || '';
        window.__cy_reqs.push({url:u, method:self.__m, body:body||'', headers:self.__h||{}});
        self.addEventListener('loadend', function() {
            try {
                var text = self.responseText;
                if (text && text.trim().startsWith('{')) {
                    window.__cy_resps.push({url:u, body:text});
                }
            } catch(e){}
        });
        return _send.apply(this, arguments);
    };

    // ── fetch 후크 ───────────────────────────────────────────
    var _fetch = window.fetch;
    window.fetch = function(input, init) {
        var u = (typeof input === 'string') ? input : (input.url || '');
        var p = _fetch.apply(this, arguments);
        p.then(function(resp) {
            resp.clone().text().then(function(text) {
                if (text && text.trim().startsWith('{')) {
                    window.__cy_resps.push({url:u, body:text});
                }
            });
        }).catch(function(){});
        return p;
    };
})();
"""

def parse_date(s):
    if not s: return None
    s = re.sub(r'[-./]', '', str(s).strip())
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return None

def get_status(start, end):
    t = TODAY
    if not start or not end: return "미정"
    if t < start:  return "청약예정"
    if t <= end:   return "청약중"
    return "청약마감"

def extract_rows(data):
    """응답 JSON에서 리스트 추출 (다양한 구조 대응)"""
    if isinstance(data, list): return data
    if not isinstance(data, dict): return []
    for key in ["data","list","dataList","items","item","result","body",
                "APTLttotPblancList","lttotPblancList","houseList","aptList"]:
        v = data.get(key)
        if isinstance(v, list) and v: return v
        if isinstance(v, dict):
            for k2 in ["item","items","list","data"]:
                v2 = v.get(k2)
                if isinstance(v2, list) and v2: return v2
    return []

def parse_item(row):
    name = (row.get("houseNm") or row.get("houseName") or row.get("lttotNm") or
            row.get("aptNm") or "").strip()
    if not name: return None

    # 지역 필터
    addr_raw = (row.get("hssplyAdres") or row.get("address") or
                row.get("sido","")+" "+row.get("sgungu","") or "").strip()
    if not any(m in addr_raw for m in METRO): return None

    if   "서울" in addr_raw: region = "서울특별시"
    elif "경기" in addr_raw: region = "경기도"
    elif "인천" in addr_raw: region = "인천광역시"
    else: return None

    district = (row.get("sgungu") or row.get("sigungu") or "").strip()
    address  = addr_raw or f"{region} {district}"

    start_dt = parse_date(row.get("subscrptRceptBgnde") or row.get("rceptBgnde") or row.get("startDate"))
    end_dt   = parse_date(row.get("subscrptRceptEndde") or row.get("rceptEndde") or row.get("endDate"))
    status   = get_status(start_dt, end_dt)
    if status == "청약마감": return None

    announce = parse_date(row.get("rcritPblancDe") or row.get("pblancDe") or row.get("announceDate"))
    win_dt   = parse_date(row.get("przwnerPresnatnDe") or row.get("presnatnDe") or row.get("winDate"))
    move_raw = str(row.get("mvnPrearngeYm") or row.get("moveIn") or "").strip()
    move_in  = move_raw[:7] if len(move_raw) >= 6 else move_raw

    try:    supply = int(row.get("totSuplyHshldco") or row.get("supplyCnt") or 0)
    except: supply = 0

    def to_man(v):
        try: return int(str(v).replace(",","")) // 10000
        except: return None

    p_min = to_man(row.get("lllc") or row.get("priceMin"))
    p_max = to_man(row.get("parcprc") or row.get("priceMax"))

    htype = str(row.get("houseSecd") or row.get("type") or "APT").upper()
    if "오피" in htype or "오피스텔" in name: htype = "OFT"
    else: htype = "APT"

    builder = (row.get("cnstrctEntrpsNm") or row.get("bsnsMbyNm") or row.get("builder") or "").strip()
    url_p   = row.get("pblancUrl") or row.get("url") or ""
    full_url = (BASE + url_p) if url_p.startswith("/") else (url_p or BASE)

    item_id = str(row.get("pblancNo") or row.get("houseManageNo") or "")
    if not item_id:
        item_id = re.sub(r'\W','', name)[:12] + (start_dt or "").replace("-","")[:6]

    return dict(
        id=item_id, name=name, type=htype, builder=builder,
        region=region, district=district, address=address,
        supply_count=supply, price_min=p_min, price_max=p_max,
        announce_date=announce, start_date=start_dt, end_date=end_dt,
        win_date=win_dt, move_in=move_in, status=status,
        url=full_url, scraped_date=TODAY,
    )

def main():
    print(f"=== 청약홈 스크래퍼 v2 시작: {TODAY} ===\n")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("오류: pip3 install playwright && python3 -m playwright install chromium")
        sys.exit(1)

    all_rows = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=False,   # 디버그용: 브라우저 직접 확인
            args=["--no-sandbox"]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1440, "height": 900},
        )
        context.add_init_script(INIT_SCRIPT)
        page = context.new_page()

        # ── 1. 청약홈 APT 목록 페이지 ──────────────────────────
        list_urls = [
            BASE + "/ai/aia/selectAPTLttotPblancListView.do",
            BASE + "/apt/applyHome.do",
            BASE + "/ai/aia/selectAPTRemndrLttotPblancListView.do",  # 무순위
        ]

        for url in list_urls:
            print(f"\n[로드] {url}")
            try:
                page.goto(url, wait_until="networkidle", timeout=30000)
                page.wait_for_timeout(5000)

                resps = page.evaluate("() => window.__cy_resps || []")
                reqs  = page.evaluate("() => window.__cy_reqs  || []")
                print(f"  XHR 응답 캡처: {len(resps)}개, 요청: {len(reqs)}개")

                # 유효한 응답 파싱
                for resp in resps:
                    try:
                        d = json.loads(resp['body'])
                        rows = extract_rows(d)
                        if rows:
                            print(f"  ✓ {resp['url'][-50:]}: {len(rows)}건 발견!")
                            all_rows.extend(rows)
                    except: pass

                # 요청 URL 출력 (디버그)
                if not all_rows:
                    print("  데이터 없음. 캡처된 XHR URL:")
                    for req in reqs[-10:]:
                        print(f"    [{req.get('method','?')}] {req.get('url','')[-70:]}")

            except Exception as e:
                print(f"  오류: {e}")

        # ── 2. 페이지 조작으로 추가 수집 ───────────────────────
        if not all_rows:
            print("\n[2단계] 페이지 조작으로 수집 시도...")

            # 청약홈 메인에서 청약중/예정 탭 클릭 시도
            page.goto(BASE, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(5000)
            page.evaluate("() => { window.__cy_resps = []; window.__cy_reqs = []; }")

            # 청약 관련 링크/버튼 클릭 시도
            for selector in [
                "a:has-text('아파트')", "a:has-text('청약정보')",
                "a:has-text('분양정보')", "button:has-text('청약')",
                "[href*='APT']", "[href*='apt']", "[onclick*='apt']",
            ]:
                try:
                    el = page.locator(selector).first
                    if el.is_visible():
                        print(f"  클릭: {selector}")
                        el.click()
                        page.wait_for_timeout(5000)
                        resps = page.evaluate("() => window.__cy_resps || []")
                        for resp in resps:
                            try:
                                d = json.loads(resp['body'])
                                rows = extract_rows(d)
                                if rows:
                                    print(f"  ✓ {len(rows)}건 발견!")
                                    all_rows.extend(rows)
                            except: pass
                        if all_rows: break
                except: pass

        # ── 3. 전체 XHR 요청 재현 ──────────────────────────────
        if not all_rows:
            print("\n[3단계] 캡처된 XHR 요청 재현...")
            reqs = page.evaluate("() => window.__cy_reqs || []")
            for req in reqs:
                url = req.get('url','')
                if not url or 'applyhome' not in url: continue
                try:
                    body_param = json.dumps(req.get('body',''))
                    result = page.evaluate(f"""async () => {{
                        var resp = await fetch({json.dumps(url)}, {{
                            method: {json.dumps(req.get('method','GET'))},
                            headers: {json.dumps(req.get('headers',{}))},
                            body: {body_param} || undefined,
                            credentials: 'include'
                        }});
                        var text = await resp.text();
                        return {{status: resp.status, body: text.slice(0,2000)}};
                    }}""")
                    if result and result.get('body','').strip().startswith('{'):
                        d = json.loads(result['body'])
                        rows = extract_rows(d)
                        if rows:
                            print(f"  ✓ {url[-50:]}: {len(rows)}건")
                            all_rows.extend(rows)
                except Exception as e:
                    pass

        # 마지막 캡처 상태 출력
        final_reqs  = page.evaluate("() => window.__cy_reqs  || []")
        final_resps = page.evaluate("() => window.__cy_resps || []")
        print(f"\n최종 캡처: 요청 {len(final_reqs)}개, 응답 {len(final_resps)}개")
        if not all_rows:
            print("\n캡처된 전체 XHR URL 목록:")
            for req in final_reqs:
                print(f"  {req.get('method','?')} {req.get('url','')}")

        context.close()
        browser.close()

    # ── 데이터 처리 ──────────────────────────────────────────
    print(f"\n[처리] 수집된 raw: {len(all_rows)}건")

    if not all_rows:
        print("\n⚠️  XHR 캡처 실패 - 기존 cheongyak.json 유지")
        return

    seen = set()
    items = []
    for row in all_rows:
        item = parse_item(row)
        if item and item["id"] not in seen:
            seen.add(item["id"])
            items.append(item)
            print(f"  ✓ {item['name'][:28]} [{item['region'][:2]}] {item['start_date']}~{item['end_date']} {item['status']}")

    items.sort(key=lambda x: (x.get('start_date') or '9999', x.get('name','')))
    print(f"\n[결과] 유효 청약 {len(items)}건")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"updated": TODAY, "subscriptions": items}, f,
                  ensure_ascii=False, indent=2)
    print(f"[저장] {OUT}")
    print("=== 완료 ===")

if __name__ == "__main__":
    main()
