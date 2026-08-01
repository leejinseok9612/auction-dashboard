#!/usr/bin/env python3
"""
청약홈 스크래퍼
────────────────────────────────────────────────
applyhome.co.kr 에서 수도권 청약 분양 정보를 수집합니다.
XHR 후크로 API 응답을 캡처 후 JSON 저장.

실행: python3 scraper/scrape_subscriptions.py
출력: docs/data/subscriptions.json
"""

import json, os, sys, re
from datetime import date, datetime, timedelta

BASE    = "https://www.applyhome.co.kr"
TODAY   = date.today().isoformat()
OUT     = os.path.join(os.path.dirname(__file__), "..", "docs", "data", "cheongyak.json")

# 수집 대상 지역 (시도명)
METRO_REGIONS = ["서울특별시", "경기도", "인천광역시"]

INIT_SCRIPT = """
window.__cy_captured = [];
window.__cy_last_req = null;
(function() {
    var _open = XMLHttpRequest.prototype.open;
    var _send = XMLHttpRequest.prototype.send;
    var _setHdr = XMLHttpRequest.prototype.setRequestHeader;

    XMLHttpRequest.prototype.open = function(method, url, async) {
        this.__url = (typeof url === 'string') ? url : '';
        this.__method = method;
        this.__hdrs = {};
        return _open.apply(this, arguments);
    };
    XMLHttpRequest.prototype.setRequestHeader = function(n, v) {
        this.__hdrs = this.__hdrs || {};
        this.__hdrs[n] = v;
        return _setHdr.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function(body) {
        var self = this;
        // 청약 목록 API 캡처
        var isCyApi = (self.__url||'').includes('APTLttotPblancList') ||
                      (self.__url||'').includes('lttotPblanc') ||
                      (self.__url||'').includes('selectAPT');
        if (isCyApi) {
            window.__cy_last_req = {
                url: self.__url,
                method: self.__method || 'POST',
                body: (typeof body === 'string') ? body : '',
                headers: self.__hdrs || {}
            };
        }
        self.addEventListener('loadend', function() {
            if (!isCyApi) return;
            try {
                var d = JSON.parse(self.responseText);
                window.__cy_captured.push({url: self.__url, data: d});
                console.log('[CY HOOK] 캡처: ' + self.__url.slice(-40));
            } catch(e) {}
        });
        return _send.apply(this, arguments);
    };
})();
"""

def parse_date(s):
    """YYYYMMDD → YYYY-MM-DD"""
    if not s: return None
    s = str(s).strip().replace("-","").replace(".","").replace("/","")
    if len(s) == 8 and s.isdigit():
        return f"{s[:4]}-{s[4:6]}-{s[6:]}"
    return None

def get_status(start, end):
    today = TODAY
    if not start or not end: return "미정"
    if today < start: return "청약예정"
    if start <= today <= end: return "청약중"
    return "청약마감"

def parse_item(row):
    """API 응답 row → 대시보드 아이템"""
    # 필드명은 청약홈 API 기준 (확인 후 조정)
    name = (row.get("houseNm") or row.get("houseName") or row.get("lttotNm") or "").strip()
    if not name:
        return None

    region_raw = (row.get("sido") or row.get("hssplyAdres") or "").strip()
    # 지역 필터: 수도권만
    if not any(r in region_raw for r in ["서울", "경기", "인천"]):
        return None

    # 시도 정규화
    if "서울" in region_raw: region = "서울특별시"
    elif "경기" in region_raw: region = "경기도"
    elif "인천" in region_raw: region = "인천광역시"
    else: region = region_raw

    district = (row.get("sgungu") or row.get("sigungu") or "").strip()
    address  = (row.get("hssplyAdres") or row.get("address") or f"{region} {district}").strip()

    announce  = parse_date(row.get("rcritPblancDe") or row.get("pblancDe") or row.get("announceDate"))
    start_dt  = parse_date(row.get("subscrptRceptBgnde") or row.get("startDate") or row.get("rceptBgnde"))
    end_dt    = parse_date(row.get("subscrptRceptEndde") or row.get("endDate")   or row.get("rceptEndde"))
    win_dt    = parse_date(row.get("przwnerPresnatnDe") or row.get("winDate")    or row.get("presnatnDe"))
    move_in_raw = str(row.get("mvnPrearngeYm") or row.get("moveIn") or "").strip()
    move_in = move_in_raw[:7] if len(move_in_raw) >= 6 else move_in_raw

    try: supply = int(row.get("totSuplyHshldco") or row.get("supplyCnt") or 0)
    except: supply = 0

    try: p_min = int(str(row.get("lllc") or row.get("priceMin") or "0").replace(",","")) // 10000
    except: p_min = None
    try: p_max = int(str(row.get("parcprc") or row.get("priceMax") or "0").replace(",","")) // 10000
    except: p_max = None

    house_type = (row.get("houseSecd") or row.get("type") or "APT").strip().upper()
    if "오피" in house_type or "오피스텔" in name: house_type = "OFT"
    elif "민간" in house_type or "APT" in house_type: house_type = "APT"
    builder = (row.get("cnstrctEntrpsNm") or row.get("builder") or "").strip()
    url_path = row.get("pblancUrl") or row.get("url") or ""
    full_url = (BASE + url_path) if url_path.startswith("/") else (url_path or BASE)

    # 고유 ID: 공고번호 또는 이름+날짜 해시
    item_id = str(row.get("pblancNo") or row.get("houseManageNo") or "")
    if not item_id:
        item_id = re.sub(r'\W', '', name)[:12] + (start_dt or "")[:7].replace("-","")

    status = get_status(start_dt, end_dt)
    # 마감된 건은 제외 (오늘 기준 end_date 지난 것)
    if status == "청약마감":
        return None

    return {
        "id":           item_id,
        "name":         name,
        "type":         house_type,
        "builder":      builder,
        "region":       region,
        "district":     district,
        "address":      address,
        "supply_count": supply,
        "price_min":    p_min,
        "price_max":    p_max,
        "announce_date":announce,
        "start_date":   start_dt,
        "end_date":     end_dt,
        "win_date":     win_dt,
        "move_in":      move_in,
        "status":       status,
        "url":          full_url,
        "scraped_date": TODAY,
    }

def try_fetch_pages(page, url, params_base, region_code, max_pages=20):
    """fetch()로 페이지네이션 수집"""
    rows = []
    for pg in range(1, max_pages + 1):
        params = {**params_base, "pageIndex": pg}
        result = page.evaluate(f"""async () => {{
            try {{
                var resp = await fetch('{url}', {{
                    method: 'POST',
                    headers: {{'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                               'X-Requested-With': 'XMLHttpRequest'}},
                    body: new URLSearchParams({json.dumps(params)}).toString()
                }});
                var text = await resp.text();
                var d = JSON.parse(text);
                return {{ok:true, data:d}};
            }} catch(e) {{
                return {{ok:false, reason:String(e)}};
            }}
        }}""")
        if not result or not result.get('ok'):
            print(f"  p={pg} 오류: {result}")
            break
        data = result.get('data', {})
        # 응답 구조 파악
        items = (data.get('data') or data.get('list') or data.get('dataList') or
                 data.get('APTLttotPblancList') or [])
        if isinstance(items, dict):
            items = items.get('item') or items.get('items') or []
        if not items:
            break
        rows.extend(items)
        print(f"  {region_code} p={pg}: {len(items)}건 (누적 {len(rows)}건)")
        total = int(data.get('totalCount') or data.get('total') or 0)
        if total and len(rows) >= total:
            break
        if len(items) < 10:
            break
    return rows

def main():
    print(f"=== 청약홈 스크래퍼 시작: {TODAY} ===\n")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("오류: pip3 install playwright && python3 -m playwright install chromium")
        sys.exit(1)

    all_rows = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
        )
        context.add_init_script(INIT_SCRIPT)
        page = context.new_page()

        # ── 1. 청약홈 메인 로드 ─────────────────────────────────
        print("[1단계] 청약홈 로드...")
        page.goto(BASE + "/ai/aia/selectAPTLttotPblancListView.do",
                  wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(5000)
        print(f"  URL: {page.url}")

        # ── 2. XHR 캡처 확인 ────────────────────────────────────
        print("\n[2단계] 자동 XHR 캡처 확인...")
        page.wait_for_timeout(3000)
        captured = page.evaluate("() => window.__cy_captured || []")
        last_req = page.evaluate("() => window.__cy_last_req")
        print(f"  자동 캡처: {len(captured)}개")

        # ── 3. 검색 폼 조작 ─────────────────────────────────────
        print("\n[3단계] 지역·날짜 검색 조작...")

        # 수도권 탐색
        REGION_CODES = [
            ("11", "서울"),
            ("41", "경기"),
            ("28", "인천"),
        ]

        # API 엔드포인트 후보들
        API_ENDPOINTS = [
            "/ai/aia/selectAPTLttotPblancList.do",
            "/ai/aib/selectAPTRemndrLttotPblancList.do",  # 무순위
        ]

        for sido_code, sido_name in REGION_CODES:
            print(f"\n  [{sido_name}] 검색 시작...")

            for endpoint in API_ENDPOINTS:
                params = {
                    "sido":         sido_code,
                    "sgungu":       "",
                    "houseSecd":    "",         # 전체 유형
                    "strtRecordNo": 1,
                    "pageIndex":    1,
                    "orderBy":      "3",        # 최신순
                    "searchCondition": "",
                    "searchKeyword":   "",
                }

                # 직접 fetch 시도
                result = page.evaluate(f"""async () => {{
                    try {{
                        var params = {json.dumps(params)};
                        var body = Object.keys(params).map(k=>
                            encodeURIComponent(k)+'='+encodeURIComponent(params[k])).join('&');
                        var resp = await fetch('{BASE + endpoint}', {{
                            method: 'POST',
                            headers: {{
                                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                                'X-Requested-With': 'XMLHttpRequest',
                                'Referer': '{BASE}/ai/aia/selectAPTLttotPblancListView.do'
                            }},
                            credentials: 'include',
                            body: body
                        }});
                        var text = await resp.text();
                        return {{ok:true, status:resp.status, text:text.slice(0,1000), len:text.length}};
                    }} catch(e) {{
                        return {{ok:false, reason:String(e)}};
                    }}
                }}""")

                if not result or not result.get('ok'):
                    print(f"    {endpoint}: 실패 ({result})")
                    continue

                status_code = result.get('status')
                text_preview = result.get('text','')
                print(f"    {endpoint}: HTTP {status_code}, {result.get('len',0)}바이트")

                # JSON 파싱 시도
                try:
                    full_result = page.evaluate(f"""async () => {{
                        var params = {json.dumps(params)};
                        var body = Object.keys(params).map(k=>
                            encodeURIComponent(k)+'='+encodeURIComponent(params[k])).join('&');
                        var resp = await fetch('{BASE + endpoint}', {{
                            method: 'POST',
                            headers: {{
                                'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                                'X-Requested-With': 'XMLHttpRequest',
                                'Referer': '{BASE}/ai/aia/selectAPTLttotPblancListView.do'
                            }},
                            credentials: 'include',
                            body: body
                        }});
                        var d = await resp.json();
                        return d;
                    }}""")

                    if full_result:
                        # 데이터 목록 추출
                        items = (full_result.get('data') or full_result.get('list') or
                                 full_result.get('dataList') or [])
                        if isinstance(items, dict):
                            items = items.get('item') or items.get('items') or []
                        total = full_result.get('totalCount') or full_result.get('total') or 0
                        print(f"    ✓ {len(items)}건 (총 {total}건) 발견!")
                        if items:
                            all_rows.extend(items)
                            # 2페이지~
                            page_size = len(items)
                            total_pages = (int(total) + page_size - 1) // page_size if total else 1
                            for pg in range(2, min(total_pages + 1, 20)):
                                params["pageIndex"] = pg
                                page_result = page.evaluate(f"""async () => {{
                                    var params = {json.dumps(params)};
                                    var body = Object.keys(params).map(k=>
                                        encodeURIComponent(k)+'='+encodeURIComponent(params[k])).join('&');
                                    var resp = await fetch('{BASE + endpoint}', {{
                                        method: 'POST',
                                        headers: {{
                                            'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                                            'X-Requested-With': 'XMLHttpRequest'
                                        }},
                                        credentials: 'include',
                                        body: body
                                    }});
                                    var d = await resp.json();
                                    return d;
                                }}""")
                                page_items = (page_result.get('data') or page_result.get('list') or
                                              page_result.get('dataList') or []) if page_result else []
                                if not page_items: break
                                all_rows.extend(page_items)
                                print(f"    p={pg}: {len(page_items)}건 (누적 {len(all_rows)}건)")
                            break  # 이 endpoint 성공 → 다음 endpoint 스킵
                except Exception as e:
                    print(f"    JSON 파싱 오류: {e}")
                    print(f"    응답 미리보기: {text_preview[:200]}")

        # ── 4. XHR 자동 캡처 결과도 합산 ────────────────────────
        auto_captured = page.evaluate("() => window.__cy_captured || []")
        for cap in auto_captured:
            data = cap.get('data', {})
            items = (data.get('data') or data.get('list') or data.get('dataList') or [])
            if items:
                all_rows.extend(items)
                print(f"  [자동캡처] {cap.get('url','')[-40:]}: {len(items)}건")

        context.close()
        browser.close()
        print("\n[브라우저 종료]")

    # ── 데이터 처리 ──────────────────────────────────────────────
    print(f"\n[처리] 수집된 raw 데이터: {len(all_rows)}건")

    if not all_rows:
        print("\n⚠️  데이터 수집 실패 — 응답 구조가 예상과 다를 수 있습니다.")
        print("   청약홈 API 구조를 재확인 후 재시도하세요.")
        # 기존 파일 유지
        if os.path.exists(OUT):
            print(f"   기존 {OUT} 유지")
        return

    # 파싱 & 필터
    seen = set()
    items = []
    for row in all_rows:
        item = parse_item(row)
        if item and item["id"] not in seen:
            seen.add(item["id"])
            items.append(item)
            print(f"  ✓ {item['name'][:30]} [{item['region'][:2]}] {item['start_date']}~{item['end_date']} {item['status']}")

    print(f"\n[결과] 유효 청약 {len(items)}건 (수도권 + 진행중/예정)")

    # 날짜순 정렬
    items.sort(key=lambda x: (x.get('start_date') or '9999', x.get('name','')))

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"updated": TODAY, "subscriptions": items}, f,
                  ensure_ascii=False, indent=2)

    print(f"[저장] {OUT} — 총 {len(items)}건")
    print("=== 완료 ===")


if __name__ == "__main__":
    main()
