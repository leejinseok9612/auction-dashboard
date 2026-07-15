#!/usr/bin/env python3
"""
법원경매정보 스크래퍼 v7
────────────────────────────────────────────────────────────
핵심 발견:
  - 사이트는 index.on 에서 SPA로 동작
  - '물건상세검색' (id=mf_wfm_header_anc_auctnGdsMain) 클릭 →
    WebSquare 내부에서 경매 검색 폼 로드 → 자동검색 or 검색버튼

흐름:
  1. index.on 로드 + WebSquare 초기화 대기
  2. 물건상세검색 클릭
  3. 검색 폼 로드 대기 (20초)
  4. scwin 검색함수 or 검색버튼 클릭
  5. XHR 후크로 결과 캡처
"""

import json, os, time, sys
from datetime import date

BASE       = "https://www.courtauction.go.kr"
TODAY      = date.today().isoformat()
OUT        = os.path.join(os.path.dirname(__file__), "..", "docs", "data", "auctions.json")
MIN_BID    = 400_000_000
METRO_SIDO = {"11", "28", "41"}

# WebSquare 보다 먼저 주입 — XHR 후크
INIT_SCRIPT = """
window.__auction_captured = [];
(function() {
    var _origOpen = XMLHttpRequest.prototype.open;
    var _origSend = XMLHttpRequest.prototype.send;

    XMLHttpRequest.prototype.open = function(method, url, async) {
        this.__iurl = (typeof url === 'string') ? url : '';
        return _origOpen.apply(this, arguments);
    };

    XMLHttpRequest.prototype.send = function(body) {
        var self = this;
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

def try_trigger_search(page):
    """검색 트리거: scwin 함수 → DOM 버튼 순으로 시도"""
    # scwin 함수 목록
    fns = page.evaluate("""() => {
        if (typeof scwin === 'undefined') return [];
        return Object.getOwnPropertyNames(scwin)
            .filter(k => typeof scwin[k] === 'function');
    }""")
    print(f"  scwin 함수: {fns[:10]}")

    # 검색 관련 함수 호출
    search_keywords = ['search','srch','조회','srchGds','mulSrch','SelMul','selMul','list','List']
    for fn in fns:
        if any(kw.lower() in fn.lower() for kw in search_keywords):
            try:
                r = page.evaluate(f"() => {{ try {{ scwin['{fn}'](); return 'ok'; }} catch(e) {{ return String(e); }} }}")
                print(f"  → scwin.{fn}() = {r}")
                page.wait_for_timeout(3000)
                if get_captured(page):
                    return True
            except:
                pass

    # 함수 못찾으면 첫 5개 다 시도
    for fn in fns[:5]:
        try:
            page.evaluate(f"() => {{ try {{ scwin['{fn}'](); }} catch(e) {{}} }}")
            page.wait_for_timeout(2000)
            if get_captured(page):
                return True
        except:
            pass

    # DOM 버튼 클릭
    for selector in [
        "button:has-text('조회')",
        "button:has-text('검색')",
        "input[value='조회']",
        "input[value='검색']",
        "a:has-text('조회')",
        "[id*='btn'][id*='srch']",
        "[id*='btn'][id*='search']",
    ]:
        try:
            elem = page.locator(selector).first
            if elem.is_visible():
                elem.click()
                print(f"  → 클릭: {selector}")
                page.wait_for_timeout(5000)
                if get_captured(page):
                    return True
        except:
            pass

    return False

def main():
    print(f"=== 경매 스크래퍼 v7 시작: {TODAY} ===\n")

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

        # 자동검색 확인
        captured = get_captured(page)
        if captured:
            print(f"  ★ 자동검색 결과 {len(captured)}개 캡처!")
        else:
            print("  → 자동검색 없음, 검색 트리거...")

            # ── 4. 검색 트리거 ─────────────────────────────
            print("\n[4단계] 검색 실행...")
            try_trigger_search(page)

            page.wait_for_timeout(10000)
            captured = get_captured(page)

            if not captured:
                print("\n[디버그] 현재 화면 버튼:")
                btns = page.evaluate("""() =>
                    Array.from(document.querySelectorAll('button,input[type=button],input[type=submit],a'))
                    .filter(e => {
                        var txt = e.innerText||e.value||'';
                        var r = e.getBoundingClientRect();
                        return txt.trim() && r.width > 0 && r.height > 0;
                    })
                    .slice(0,20)
                    .map(e => ({tag:e.tagName, text:(e.innerText||e.value||'').trim().slice(0,20), id:e.id}))
                """)
                for b in btns:
                    print(f"  [{b['tag']}] '{b['text']}' id={b['id']}")
                page.screenshot(path="/tmp/auction_form.png")
                print("\n  스크린샷: /tmp/auction_form.png")
                context.close(); browser.close()
                print("\n[브라우저 종료]")
                return

        # ── 5. 다음 페이지 클릭으로 전체 수집 ────────────────────
        print("\n[5단계] 전체 데이터 수집 (페이지 클릭)...")

        first = captured[0]
        total_cnt = int(first.get("data",{}).get("dma_pageInfo",{}).get("totalCnt") or 0)
        page_size = int(first.get("data",{}).get("dma_pageInfo",{}).get("pageSize") or 10)
        total_pages = (total_cnt + page_size - 1) // page_size
        print(f"  총 {total_cnt}건 / {total_pages}페이지")

        all_xhr_rows = []
        first_rows = first.get("data",{}).get("dlt_srchResult") or []
        all_xhr_rows.extend(first_rows)
        print(f"  p=1 → {len(first_rows)}건")

        # 2페이지부터 다음 버튼 클릭으로 수집
        for pg in range(2, min(total_pages + 1, 100)):
            # 이전 캡처 클리어
            page.evaluate("() => { window.__auction_captured = []; }")

            # 다음 페이지 버튼 클릭
            clicked = page.evaluate(f"""() => {{
                // 숫자 페이지 버튼 먼저 시도
                var btns = document.querySelectorAll('a, button, span');
                for (var b of btns) {{
                    var txt = (b.innerText || b.textContent || '').trim();
                    if (txt === '{pg}') {{
                        b.click();
                        return 'page_num:{pg}';
                    }}
                }}
                // 다음(>) 버튼
                for (var b of btns) {{
                    var txt = (b.innerText || b.textContent || '').trim();
                    if (txt === '다음' || txt === '>' || txt === '▶' || txt === '→') {{
                        b.click();
                        return 'next_btn';
                    }}
                }}
                return 'not_found';
            }}""")

            if clicked == 'not_found':
                print(f"  p={pg} → 다음 버튼 없음, 종료")
                break

            page.wait_for_timeout(3000)
            new_cap = get_captured(page)
            if not new_cap:
                print(f"  p={pg} → 응답 없음")
                break

            rows = new_cap[-1].get("data",{}).get("dlt_srchResult") or []
            all_xhr_rows.extend(rows)
            print(f"  p={pg} → {len(rows)}건 [{clicked}] (누적 {len(all_xhr_rows)}건)")

            if len(all_xhr_rows) >= total_cnt:
                break

        print(f"\n  수집 완료: {len(all_xhr_rows)}건 / 총 {total_cnt}건")
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

    merged = {**existing, **new_items}
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
