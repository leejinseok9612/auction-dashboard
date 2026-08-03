#!/usr/bin/env python3
"""
청약홈 스크래퍼 v3
────────────────────────────────────────────────
Playwright 내장 response 인터셉터를 사용합니다.
넷퍼넬(대기열) 통과 후 실제 데이터 XHR을 자동 캡처합니다.

실행: python3 scraper/scrape_subscriptions.py
출력: docs/data/cheongyak.json
"""

import json, os, sys, re, time
from datetime import date

BASE  = "https://www.applyhome.co.kr"
TODAY = date.today().isoformat()
OUT   = os.path.join(os.path.dirname(__file__), "..", "docs", "data", "cheongyak.json")
METRO = ["서울", "경기", "인천"]

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
    if isinstance(data, list): return data
    if not isinstance(data, dict): return []
    for key in ["data","list","dataList","items","item","result","body",
                "APTLttotPblancList","lttotPblancList","houseList","aptList",
                "pblanc","pblancList"]:
        v = data.get(key)
        if isinstance(v, list) and v: return v
        if isinstance(v, dict):
            for k2 in ["item","items","list","data","pblanc"]:
                v2 = v.get(k2)
                if isinstance(v2, list) and v2: return v2
    return []

def parse_item(row):
    name = (row.get("houseNm") or row.get("houseName") or row.get("lttotNm") or
            row.get("aptNm") or row.get("HOUSE_NM") or "").strip()
    if not name: return None

    addr_raw = (row.get("hssplyAdres") or row.get("address") or
                (row.get("sido","") + " " + row.get("sgungu",""))).strip()
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
    htype = "OFT" if ("오피" in htype or "오피스텔" in name) else "APT"
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
    print(f"=== 청약홈 스크래퍼 v3 시작: {TODAY} ===\n")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("pip3 install playwright && python3 -m playwright install chromium")
        sys.exit(1)

    captured_responses = []
    all_rows = []

    def on_response(resp):
        url = resp.url
        ct  = resp.headers.get("content-type","")
        skip = ["netFunnel","google","analytics","kakao","naver","jquery",
                "font",".css",".png",".jpg",".gif",".woff",".svg",".ico"]
        if any(k in url.lower() for k in skip): return
        if "json" not in ct and "javascript" not in ct: return
        try:
            body = resp.body()
            text = body.decode("utf-8", errors="ignore")
            if not (text.strip().startswith("{") or text.strip().startswith("[")):
                return
            d = json.loads(text)
            captured_responses.append({"url": url, "data": d})
            print(f"  [캡처] {url[-65:]}")
        except:
            pass

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
            viewport={"width": 1440, "height": 900},
        )
        page = context.new_page()
        page.on("response", on_response)

        # ── 1. 넷퍼넬 통과 대기 ──────────────────────────────
        print("[1단계] 청약홈 로드 (넷퍼넬 통과 최대 90초)...")
        try:
            page.goto(BASE, wait_until="domcontentloaded", timeout=30000)
        except:
            pass

        deadline = time.time() + 90
        while time.time() < deadline:
            page.wait_for_timeout(3000)
            url_now = page.url
            title   = page.title()
            print(f"  [{int(deadline-time.time())}s] {title[:25]} | {url_now[-45:]}")
            if "netFunnel" not in url_now and ("청약" in title or "apply" in url_now.lower()):
                print("  → 넷퍼넬 통과 완료!")
                break
        page.wait_for_timeout(5000)

        # ── 2. 청약 목록 직접 이동 ───────────────────────────
        print("\n[2단계] 청약 목록 페이지 직접 이동...")
        paths = [
            "/ai/aia/selectAPTLttotPblancListView.do",
            "/ai/aib/selectAPTRemndrLttotPblancListView.do",
            "/ai/aia/selectULttotPblancListView.do",
        ]
        for path in paths:
            try:
                print(f"  → {path}")
                page.goto(BASE + path, wait_until="networkidle", timeout=30000)
                page.wait_for_timeout(8000)
                print(f"    캡처 누적: {len(captured_responses)}개")
            except Exception as e:
                print(f"    오류: {e}")

        # ── 3. 지역 필터 조작 ────────────────────────────────
        print("\n[3단계] 지역 필터 조작...")
        for sido_val, sido_name in [("11","서울"),("41","경기"),("28","인천")]:
            try:
                # select 옵션 설정
                changed = page.evaluate(f"""() => {{
                    var sels = document.querySelectorAll('select');
                    for (var s of sels) {{
                        for (var o of s.options) {{
                            if (o.value === '{sido_val}') {{
                                s.value = '{sido_val}';
                                s.dispatchEvent(new Event('change', {{bubbles:true}}));
                                return s.id || s.name || 'ok';
                            }}
                        }}
                    }}
                    return 'not_found';
                }}""")
                if changed != 'not_found':
                    print(f"  {sido_name} 선택: {changed}")
                    page.wait_for_timeout(1000)
                    # 검색 버튼 클릭
                    for sel in ["button:has-text('검색')", "input[value='검색']",
                                "#btnSearch", ".btn_search", "a:has-text('검색')"]:
                        try:
                            el = page.locator(sel).first
                            if el.is_visible():
                                el.click()
                                page.wait_for_timeout(5000)
                                print(f"    검색 클릭 완료. 캡처: {len(captured_responses)}개")
                                break
                        except: pass
            except Exception as e:
                print(f"  {sido_name} 오류: {e}")

        print(f"\n최종 캡처: {len(captured_responses)}개")
        for r in captured_responses:
            rows = extract_rows(r["data"])
            print(f"  {r['url'][-60:]} → {len(rows)}행")

        context.close()
        browser.close()

    # ── 추출 & 파싱 ─────────────────────────────────────────
    for resp in captured_responses:
        rows = extract_rows(resp["data"])
        if rows:
            all_rows.extend(rows)

    print(f"\n[처리] raw {len(all_rows)}건")

    if not all_rows:
        print("\n⚠️  데이터 수집 실패 - 기존 cheongyak.json 유지")
        print("  → 청약홈 구조 변경 가능성. 캡처된 URL을 확인해주세요.")
        return

    seen, items = set(), []
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
