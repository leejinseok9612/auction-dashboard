"""
청약홈 분양정보 스크래퍼 v3
- Playwright로 청약HOME XHR 응답 캡처
- API 키 있으면 공공데이터포털 API 사용 (더 안정적)
"""

import os, json, time, re
from datetime import datetime, date

TODAY_STR = date.today().isoformat()
OUT_PATH  = os.path.join(os.path.dirname(__file__), "..", "docs", "data", "cheongyak.json")
API_KEY   = os.environ.get("CHEONGYAK_API_KEY", "")

# ── 날짜 정규화 ──────────────────────────────────────────────
def fd(s):
    s = str(s or "").replace("-","").replace(".","").replace("/","").strip()
    if len(s) == 8:
        try: return datetime.strptime(s, "%Y%m%d").strftime("%Y-%m-%d")
        except: pass
    return s or ""

# ── 상태 계산 ────────────────────────────────────────────────
def calc_status(start, end):
    try:
        ed = date.fromisoformat(end)
        sd = date.fromisoformat(start)
        today = date.today()
        if ed < today:  return "마감"
        if sd <= today: return "청약중"
        return "청약예정"
    except:
        return "미정"

# ══════════════════════════════════════════════════════════════
#  방법 1: 공공데이터포털 API
# ══════════════════════════════════════════════════════════════
def scrape_via_api():
    import requests
    BASE = "https://api.odcloud.kr/api/ApplyhomeInfoDetailSvc/v1"
    ENDPOINTS = [
        ("getAPTLttotPblancDetail",        "APT"),
        ("getUrbtyOfctlLttotPblancDetail", "오피스텔/도시형"),
        ("getRemndrLttotPblancDetail",     "무순위/잔여"),
    ]

    def fetch_page(ep, page=1, per=100):
        try:
            r = requests.get(f"{BASE}/{ep}",
                params={"serviceKey": API_KEY, "page": page, "perPage": per},
                timeout=20)
            r.raise_for_status()
            d = r.json()
            return d.get("data", []), d.get("totalCount", 0)
        except Exception as e:
            print(f"  [{ep}] p{page} 실패: {e}")
            return [], 0

    def parse(raw, htype):
        def g(k): return str(raw.get(k) or "").strip()
        start = fd(g("청약접수시작일") or g("SUBSCRPT_RCEPT_BGNDE"))
        end   = fd(g("청약접수종료일") or g("SUBSCRPT_RCEPT_ENDDE"))
        name  = g("주택명")   or g("HOUSE_NM")
        pbno  = g("공고번호") or g("PBLANC_NO")
        addr  = g("공급위치") or g("HSSPLY_ADRES")
        parts = addr.split()
        return {
            "id":            pbno or (name + start),
            "pblanc_no":     pbno,
            "name":          name,
            "type":          g("주택구분") or g("HOUSE_SECD_NM") or htype,
            "builder":       g("사업주체명") or g("BSNS_MBY_NM"),
            "region":        g("공급지역명") or g("SUBSCRPT_AREA_CODE_NM"),
            "district":      parts[1] if len(parts) > 1 else "",
            "address":       addr,
            "supply_count":  int(g("공급세대수") or g("TOT_SUPLY_HSHLDCO") or 0),
            "announce_date": fd(g("모집공고일") or g("RCRIT_PBLANC_DE")),
            "start_date":    start,
            "end_date":      end,
            "win_date":      fd(g("당첨자발표일") or g("PRZWNER_PRESNATN_DE")),
            "move_in":       g("입주예정월") or g("MVIN_PREARNGE_YM"),
            "status":        calc_status(start, end),
            "url":           g("홈페이지주소") or g("HMPG_ADRES") or "https://www.applyhome.co.kr",
            "competition":   {},
            "lat":           None,
            "lng":           None,
            "scraped_date":  TODAY_STR,
        }

    all_items = []
    for ep, htype in ENDPOINTS:
        print(f"  [{htype}] 수집 중...")
        pg = 1
        while True:
            rows, total = fetch_page(ep, pg)
            if not rows: break
            parsed = [parse(r, htype) for r in rows]
            active = [p for p in parsed if p["status"] in ("청약중","청약예정")]
            all_items.extend(active)
            print(f"    page {pg}: {len(rows)}건 → 유효 {len(active)}건 (전체 {total}건)")
            if len(rows) < 100 or pg * 100 >= total: break
            pg += 1
            time.sleep(0.3)

    return all_items

# ══════════════════════════════════════════════════════════════
#  방법 2: Playwright — XHR 응답 캡처
# ══════════════════════════════════════════════════════════════
def scrape_via_playwright():
    from playwright.sync_api import sync_playwright

    captured_items = []

    def parse_xhr_body(body_text, url=""):
        """청약HOME XHR 응답 파싱"""
        items = []
        try:
            data = json.loads(body_text)
        except:
            return items

        # 가능한 리스트 키들
        rows = []
        for key in ["list", "data", "dataBody", "result", "aptList", "lttotPblancList"]:
            val = data.get(key)
            if isinstance(val, list) and val:
                rows = val
                break
            if isinstance(val, dict):
                for k2 in ["list", "data", "aptList"]:
                    if isinstance(val.get(k2), list) and val[k2]:
                        rows = val[k2]
                        break

        for raw in rows:
            if not isinstance(raw, dict):
                continue
            def g(*keys):
                for k in keys:
                    v = raw.get(k)
                    if v is not None and str(v).strip():
                        return str(v).strip()
                return ""

            name  = g("HOUSE_NM","houseName","주택명","houseNm")
            start = fd(g("SUBSCRPT_RCEPT_BGNDE","sbscrptRceptBgnde","청약접수시작일","startDate"))
            end   = fd(g("SUBSCRPT_RCEPT_ENDDE","sbscrptRceptEndde","청약접수종료일","endDate"))
            pbno  = g("PBLANC_NO","pblancNo","공고번호")
            addr  = g("HSSPLY_ADRES","hssplyAdres","공급위치","address")
            rgn   = g("SUBSCRPT_AREA_CODE_NM","subscrptAreaCodeNm","공급지역명","region")

            if not name:
                continue

            st = calc_status(start, end)
            if st not in ("청약중","청약예정"):
                continue

            parts = addr.split()
            items.append({
                "id":            pbno or (name + start),
                "pblanc_no":     pbno,
                "name":          name,
                "type":          g("HOUSE_SECD_NM","houseSecdNm","주택구분") or "APT",
                "builder":       g("BSNS_MBY_NM","bsnsMbyNm","사업주체명"),
                "region":        rgn or (parts[0] if parts else ""),
                "district":      parts[1] if len(parts) > 1 else "",
                "address":       addr,
                "supply_count":  int(g("TOT_SUPLY_HSHLDCO","totSuplyHshldco","공급세대수") or 0),
                "announce_date": fd(g("RCRIT_PBLANC_DE","rcritPblancDe","모집공고일")),
                "start_date":    start,
                "end_date":      end,
                "win_date":      fd(g("PRZWNER_PRESNATN_DE","przwnerPresnatnDe","당첨자발표일")),
                "move_in":       g("MVIN_PREARNGE_YM","mvinPrearngeYm","입주예정월"),
                "status":        st,
                "url":           g("HMPG_ADRES","hmpgAdres","홈페이지주소") or "https://www.applyhome.co.kr",
                "competition":   {},
                "lat":           None,
                "lng":           None,
                "scraped_date":  TODAY_STR,
            })
        return items

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.new_page()

        all_responses = []

        def on_response(resp):
            url = resp.url
            # 청약HOME의 데이터 API 응답 캡처
            if "applyhome.co.kr" in url and resp.status == 200:
                ct = resp.headers.get("content-type","")
                if "json" in ct or "javascript" in ct or ".do" in url:
                    try:
                        body = resp.text()
                        if len(body) > 100 and ('"HOUSE_NM"' in body or '"houseName"' in body
                                or '"list"' in body or '"aptList"' in body
                                or 'PBLANC_NO' in body or 'pblancNo' in body):
                            all_responses.append((url, body))
                            print(f"  [캡처] {url[-70:]}")
                    except:
                        pass

        page.on("response", on_response)

        print("  브라우저 시작 → 청약HOME 접속...")
        try:
            page.goto(
                "https://www.applyhome.co.kr/ai/aia/selectAPTLttotPblancListView.do",
                wait_until="networkidle", timeout=30000
            )
        except Exception as e:
            print(f"  페이지 로드 경고: {e}")

        page.wait_for_timeout(4000)

        # "청약중" 탭 클릭 시도
        for sel in ["a:has-text('청약중')", "button:has-text('청약중')", "li:has-text('청약중') a"]:
            try:
                el = page.query_selector(sel)
                if el:
                    el.click()
                    page.wait_for_timeout(2000)
                    print(f"  '청약중' 탭 클릭: {sel}")
                    break
            except: pass

        # "청약예정" 탭 클릭 시도
        for sel in ["a:has-text('청약예정')", "button:has-text('청약예정')", "li:has-text('청약예정') a"]:
            try:
                el = page.query_selector(sel)
                if el:
                    el.click()
                    page.wait_for_timeout(2000)
                    print(f"  '청약예정' 탭 클릭: {sel}")
                    break
            except: pass

        # 검색/조회 버튼 클릭
        for sel in ["button:has-text('조회')", "input[value='조회']", "button:has-text('검색')", "#btnSearch"]:
            try:
                el = page.query_selector(sel)
                if el:
                    el.click()
                    page.wait_for_timeout(3000)
                    print(f"  조회 버튼 클릭: {sel}")
                    break
            except: pass

        page.wait_for_timeout(3000)

        # 잡힌 응답 파싱
        seen_ids = set()
        for url, body in all_responses:
            items = parse_xhr_body(body, url)
            for item in items:
                key = item["id"]
                if key not in seen_ids:
                    seen_ids.add(key)
                    captured_items.append(item)
                    print(f"  ✓ {item['status']} | {item['name']} | {item['start_date']}~{item['end_date']}")

        # 응답이 없으면 직접 POST 요청 시도
        if not captured_items:
            print("  XHR 캡처 실패 → 직접 fetch 시도...")
            try:
                result = page.evaluate("""async () => {
                    const res = await fetch('/ai/aia/selectAPTLttotPblancListZ.do', {
                        method: 'POST',
                        headers: {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8', 'X-Requested-With': 'XMLHttpRequest'},
                        body: 'sido=&gugun=&APT_NM=&houseSecd=&nowSuplyYmd=&sort=3&startPage=1&itemsPerPage=100&totalPages=0&totalItems=0'
                    });
                    return await res.text();
                }""")
                items = parse_xhr_body(result)
                for item in items:
                    key = item["id"]
                    if key not in seen_ids:
                        seen_ids.add(key)
                        captured_items.append(item)
                        print(f"  ✓ {item['status']} | {item['name']}")
            except Exception as e:
                print(f"  직접 fetch 실패: {e}")

        # 모든 탭/페이지 시도해도 없으면 페이지 내 텍스트 확인
        if not captured_items:
            print("\n  [디버그] 현재 페이지 URL:", page.url)
            # 페이지에서 단지명 패턴 텍스트 추출 시도
            try:
                texts = page.evaluate("""() => {
                    const items = [];
                    // 테이블 행
                    document.querySelectorAll('table tr, .list_item, .apt_item, li').forEach(el => {
                        const t = el.innerText.trim();
                        if (t.length > 10 && t.length < 200) items.push(t);
                    });
                    return items.slice(0, 30);
                }""")
                print("  [페이지 텍스트 샘플]:")
                for t in texts[:10]:
                    print(f"    {t[:80]}")
            except: pass

        browser.close()

    return captured_items

# ══════════════════════════════════════════════════════════════
#  메인
# ══════════════════════════════════════════════════════════════
def run():
    print(f"▶ 청약 데이터 수집 시작 ({TODAY_STR})")

    # 기존 데이터 로드
    existing_map = {}
    try:
        with open(OUT_PATH, "r", encoding="utf-8") as f:
            for e in json.load(f).get("subscriptions", []):
                if e.get("id"):
                    existing_map[e["id"]] = e
        print(f"  기존 데이터: {len(existing_map)}건 로드")
    except:
        print("  기존 데이터 없음 (첫 실행)")

    # 수집
    items = []
    if API_KEY:
        print("\n[방법 1] 공공데이터포털 API 사용")
        try:
            items = scrape_via_api()
        except Exception as e:
            print(f"  API 실패: {e} → Playwright 전환")
            items = scrape_via_playwright()
    else:
        print("\n[방법 2] Playwright 직접 스크래핑")
        items = scrape_via_playwright()

    # 기존 지오코딩 이어받기
    for item in items:
        old = existing_map.get(item["id"], {})
        if not item.get("lat") and old.get("lat"):
            item["lat"] = old["lat"]
            item["lng"] = old["lng"]

    if items:
        # 마감 제거 + 최신순 정렬
        active = [i for i in items if i.get("status") in ("청약중","청약예정")]
        active.sort(key=lambda x: x.get("start_date",""), reverse=True)
    else:
        print("⚠️  새 데이터 없음. 기존 데이터 상태 재계산 후 유지")
        for k, v in existing_map.items():
            if v.get("start_date") and v.get("end_date"):
                v["status"] = calc_status(v["start_date"], v["end_date"])
        active = [v for v in existing_map.values() if v.get("status") in ("청약중","청약예정")]
        active.sort(key=lambda x: x.get("start_date",""), reverse=True)

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump({"updated": TODAY_STR, "subscriptions": active},
                  f, ensure_ascii=False, indent=2)

    open_now  = sum(1 for x in active if x.get("status") == "청약중")
    open_soon = sum(1 for x in active if x.get("status") == "청약예정")
    print(f"\n✅ 저장 완료 → {OUT_PATH}")
    print(f"   청약중: {open_now}건 / 청약예정: {open_soon}건 / 합계: {len(active)}건")

if __name__ == "__main__":
    run()
