"""
청약홈 분양정보 스크래퍼 v2
- 1순위: 공공데이터포털 API (CHEONGYAK_API_KEY 환경변수 설정 시)
- 2순위: Playwright로 청약HOME 직접 스크래핑 (API 키 없이도 작동)
출처: https://www.applyhome.co.kr
"""

import os, json, time, sys
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
#  방법 2: Playwright 직접 스크래핑
# ══════════════════════════════════════════════════════════════
def scrape_via_playwright():
    from playwright.sync_api import sync_playwright
    import re

    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124",
            viewport={"width": 1280, "height": 800},
        )
        page = ctx.new_page()

        captured = []

        def on_response(resp):
            url = resp.url
            if "applyhome.co.kr" in url and (
                "PblancList" in url or "pblancList" in url or
                "selectAPT" in url or "LttotPblanc" in url
            ):
                try:
                    body = resp.text()
                    data = json.loads(body)
                    captured.append(data)
                    print(f"  [네트워크 캡처] {url[-60:]}")
                except:
                    pass

        page.on("response", on_response)

        print("  브라우저 시작 → 청약HOME 접속 중...")
        page.goto("https://www.applyhome.co.kr/ai/aia/selectAPTLttotPblancListView.do",
                  wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(3000)

        # 전체 탭 선택 (청약중 + 청약예정 모두 보이도록)
        try:
            # "전체" 탭 또는 첫번째 탭 클릭
            all_tab = page.query_selector("a[href*='all'], button:has-text('전체')")
            if all_tab:
                all_tab.click()
                page.wait_for_timeout(2000)
        except: pass

        # 페이지 수 파악 후 전체 페이지 순회
        page_count = 1
        try:
            pager = page.query_selector_all(".pagination a, .paging a, li.page a")
            nums = []
            for el in pager:
                t = el.inner_text().strip()
                if t.isdigit():
                    nums.append(int(t))
            if nums:
                page_count = max(nums)
        except: pass

        print(f"  페이지 수: {page_count}")

        def parse_page_dom():
            """DOM에서 청약 카드 파싱"""
            items = []
            rows = page.query_selector_all(
                "ul.list_wrap > li, .apt_item, .house_item, tr.list_item, .result_list li"
            )
            for row in rows:
                try:
                    txt = row.inner_text()
                    lines = [l.strip() for l in txt.split('\n') if l.strip()]
                    if len(lines) < 3: continue

                    name    = lines[0]
                    address = ""
                    start   = ""
                    end     = ""

                    for line in lines:
                        if "청약" in line and ("~" in line or "-" in line):
                            parts = re.split(r"[~\-–]", line.replace("청약기간","").replace(":",""))
                            if len(parts) >= 2:
                                start = fd(parts[0].strip()[:8])
                                end   = fd(parts[1].strip()[:8])
                        if any(k in line for k in ["시 ","도 ","구 ","동 "]):
                            if len(line) > 5: address = line

                    items.append({
                        "id":            name + start,
                        "pblanc_no":     "",
                        "name":          name,
                        "type":          "APT",
                        "builder":       "",
                        "region":        address.split()[0] if address else "",
                        "district":      address.split()[1] if len(address.split()) > 1 else "",
                        "address":       address,
                        "supply_count":  0,
                        "announce_date": "",
                        "start_date":    start,
                        "end_date":      end,
                        "win_date":      "",
                        "move_in":       "",
                        "status":        calc_status(start, end),
                        "url":           "https://www.applyhome.co.kr",
                        "competition":   {},
                        "lat":           None,
                        "lng":           None,
                        "scraped_date":  TODAY_STR,
                    })
                except:
                    pass
            return items

        # 네트워크 캡처 우선, 실패시 DOM 파싱
        page_items = parse_page_dom()
        results.extend(page_items)
        print(f"  page 1 DOM 파싱: {len(page_items)}건")

        # 페이지 2부터 순회
        for pn in range(2, min(page_count+1, 10)):
            try:
                btn = page.query_selector(f"a:has-text('{pn}'), button:has-text('{pn}')")
                if btn:
                    btn.click()
                    page.wait_for_timeout(2000)
                    items = parse_page_dom()
                    results.extend(items)
                    print(f"  page {pn}: {len(items)}건")
            except Exception as e:
                print(f"  page {pn} 실패: {e}")
                break

        # 네트워크 캡처된 JSON 처리
        for d in captured:
            try:
                rows = d.get("data") or d.get("dataBody",{}).get("list",[]) or []
                for raw in rows:
                    def g(k): return str(raw.get(k) or "").strip()
                    start = fd(g("SBSCRPT_RCEPT_BGNDE") or g("청약접수시작일"))
                    end   = fd(g("SBSCRPT_RCEPT_ENDDE") or g("청약접수종료일"))
                    name  = g("HOUSE_NM") or g("주택명")
                    st    = calc_status(start, end)
                    if st not in ("청약중","청약예정"): continue
                    results.append({
                        "id":            g("PBLANC_NO") or name+start,
                        "pblanc_no":     g("PBLANC_NO"),
                        "name":          name,
                        "type":          g("HOUSE_SECD_NM") or "APT",
                        "builder":       g("BSNS_MBY_NM"),
                        "region":        g("SUBSCRPT_AREA_CODE_NM"),
                        "district":      "",
                        "address":       g("HSSPLY_ADRES"),
                        "supply_count":  int(g("TOT_SUPLY_HSHLDCO") or 0),
                        "announce_date": fd(g("RCRIT_PBLANC_DE")),
                        "start_date":    start,
                        "end_date":      end,
                        "win_date":      fd(g("PRZWNER_PRESNATN_DE")),
                        "move_in":       g("MVIN_PREARNGE_YM"),
                        "status":        st,
                        "url":           g("HMPG_ADRES") or "https://www.applyhome.co.kr",
                        "competition":   {},
                        "lat":           None,
                        "lng":           None,
                        "scraped_date":  TODAY_STR,
                    })
            except:
                pass

        browser.close()

    # 중복 제거
    seen, unique = set(), []
    for item in results:
        key = item.get("id","") or item.get("name","")
        if key and key not in seen:
            seen.add(key)
            unique.append(item)

    return [i for i in unique if i["status"] in ("청약중","청약예정")]

# ══════════════════════════════════════════════════════════════
#  메인
# ══════════════════════════════════════════════════════════════
def run():
    print(f"▶ 청약 데이터 수집 시작 ({TODAY_STR})")

    # 기존 데이터 로드 (지오코딩 캐시)
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
        print("\n[방법 2] Playwright 직접 스크래핑 (API 키 없음)")
        items = scrape_via_playwright()

    if not items:
        print("⚠️  수집된 데이터 없음. 기존 데이터 유지")
        # 기존 데이터를 상태 재계산해서 저장
        for k, v in existing_map.items():
            if v.get("start_date") and v.get("end_date"):
                v["status"] = calc_status(v["start_date"], v["end_date"])
        items = list(existing_map.values())

    # 기존 지오코딩 이어받기
    for item in items:
        old = existing_map.get(item["id"], {})
        if not item.get("lat") and old.get("lat"):
            item["lat"] = old["lat"]
            item["lng"] = old["lng"]

    # 마감된 것 제거 + 최신순 정렬
    active = [i for i in items if i.get("status") in ("청약중","청약예정")]
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
