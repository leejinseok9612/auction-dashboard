#!/usr/bin/env python3
"""
청약홈 스크래퍼 v6  (전국 + 공공지원 민간임대 추가)
────────────────────────────────────────────────
변경사항 v6:
  - 지역 필터 제거 → 전국 전체 수집 (지역 옵션 선택 없이 조회)
  - LIST_TYPES에 공공지원 민간임대 추가
  - 지역 정규화 테이블 전국 17개 시도로 확장

실행: python3 scraper/scrape_subscriptions.py
출력: docs/data/cheongyak.json
"""

import json, os, re, sys, time
from datetime import date

try:
    from bs4 import BeautifulSoup
except ImportError:
    print("pip3 install beautifulsoup4 를 먼저 실행하세요")
    sys.exit(1)

BASE  = "https://www.applyhome.co.kr"
TODAY = date.today().isoformat()
OUT   = os.path.join(os.path.dirname(__file__), "..", "docs", "data", "cheongyak.json")

# 전국 17개 시도 정규화 테이블
REGION_NORM = {
    "서울": "서울특별시",
    "경기": "경기도",
    "인천": "인천광역시",
    "부산": "부산광역시",
    "대구": "대구광역시",
    "광주": "광주광역시",
    "대전": "대전광역시",
    "울산": "울산광역시",
    "세종": "세종특별자치시",
    "강원": "강원특별자치도",
    "충북": "충청북도",
    "충남": "충청남도",
    "전북": "전북특별자치도",
    "전남": "전라남도",
    "경북": "경상북도",
    "경남": "경상남도",
    "제주": "제주특별자치도",
}

# ── 날짜/상태 유틸 ────────────────────────────────────────────────────────────

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

def normalize_region(region_raw):
    """지역명 → 정규화 (예: '경기 김포' → '경기도')"""
    for key, full in REGION_NORM.items():
        if key in region_raw:
            return full
    return region_raw.strip() or "기타"

# ── HTML 테이블에서 청약 데이터 추출 ─────────────────────────────────────────

def parse_apt_table(html: str) -> list:
    """
    HTML에서 <tr data-pbno="..."> 청약 목록을 파싱합니다.
    전국 전체 수집 (지역 필터 없음)

    테이블 컬럼 순서:
      td[0]: 지역
      td[1]: 구분 (민영/국민)
      td[2]: 주택유형
      td[3]: 주택명 (<a><b>명칭</b></a>)
      td[4]: 시공사
      td[5]: 문의처
      td[6]: 모집공고일
      td[7]: 청약기간
      td[8]: 당첨자발표일
    """
    soup  = BeautifulSoup(html, "html.parser")
    items = []

    for tr in soup.select("tr[data-pbno]"):
        pbno = tr.get("data-pbno","").strip()
        hmno = tr.get("data-hmno","").strip()
        honm = tr.get("data-honm","").strip()
        if not pbno: continue

        tds = tr.find_all("td")
        if len(tds) < 8: continue

        def td(i): return tds[i].get_text(" ", strip=True) if i < len(tds) else ""

        region_raw = td(0)
        region = normalize_region(region_raw)

        # 주택명 (data-honm 우선)
        name = honm
        if not name:
            b = tds[3].find("b")
            name = b.get_text(strip=True) if b else td(3)
        name = name.strip()
        if not name: continue

        builder = td(4)

        announce = parse_date(td(6))

        # 청약기간: "2026-07-27 ~ 2026-07-30"
        period = td(7)
        m = re.search(r'(\d{4}[-./]\d{2}[-./]\d{2})\s*[~∼]\s*(\d{4}[-./]\d{2}[-./]\d{2})', period)
        start_dt = parse_date(m.group(1)) if m else None
        end_dt   = parse_date(m.group(2)) if m else None

        win_dt = parse_date(td(8))
        status = get_status(start_dt, end_dt)

        # 청약마감: 당첨자 발표 전이면 발표대기로 포함
        if status == "청약마감":
            if win_dt and win_dt >= TODAY:
                status = "발표대기"
            else:
                continue  # 완전히 종료된 건 제외

        htype   = "OFT" if "오피스텔" in name else "APT"
        item_id = pbno or (re.sub(r'\W','', name)[:12] + (start_dt or "").replace("-","")[:6])
        detail_url = BASE  # 넷퍼넬 때문에 메인 페이지 링크 사용

        items.append(dict(
            id=item_id, name=name, type=htype, builder=builder,
            region=region, district="", address=region,
            supply_count=0, price_min=None, price_max=None,
            announce_date=announce, start_date=start_dt, end_date=end_dt,
            win_date=win_dt, move_in=None, status=status,
            url=detail_url, scraped_date=TODAY,
        ))

    return items

def get_total_pages(html: str) -> int:
    """페이지 수 파악: pagination 링크에서 최대 pageIndex 추출"""
    m = re.findall(r'fn_link_page\((\d+)\)', html)
    if m:
        return max(int(x) for x in m)
    return 1

# ── Playwright 스크래핑 ──────────────────────────────────────────────────────

def scrape_with_playwright():
    from playwright.sync_api import sync_playwright

    all_items = []

    # 청약홈 목록 유형 (APT 분양 + 잔여세대 + 오피스텔/도시형 + 공공지원 민간임대)
    LIST_TYPES = [
        ("/ai/aia/selectAPTLttotPblancListView.do",        "APT 분양"),
        ("/ai/aib/selectAPTRemndrLttotPblancListView.do",  "잔여세대"),
        ("/ai/aia/selectULttotPblancListView.do",          "오피/도시형"),
        ("/ai/aib/selectPublicRentHouseListView.do",       "공공지원 민간임대"),
    ]

    def click_search(page):
        """조회 버튼 클릭 (지역 필터 없이 전국 조회)"""
        clicked = False
        for sel in ["button.search_btn", "button:has-text('조회')",
                    "button:has-text('검색')", "#btnSearch"]:
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=1500):
                    btn.click(); clicked = True
                    print(f"    조회 클릭: {sel}")
                    break
            except: pass
        if not clicked:
            try:
                page.evaluate("""() => {
                    if (typeof search !== 'undefined') search.submit([], 1);
                    else if (typeof $net !== 'undefined')
                        $net.submit('pbSearchForm', location.pathname);
                }""")
            except: pass
        page.wait_for_timeout(6000)

    def scrape_all_pages(page, list_name):
        """현재 페이지부터 모든 페이지 순회하며 파싱"""
        html        = page.content()
        total       = get_total_pages(html)
        items_p1    = parse_apt_table(html)
        print(f"    페이지 1/{total}: {len(items_p1)}건")
        collected   = list(items_p1)

        empty_streak = 0
        for pg in range(2, min(total + 1, 51)):
            try:
                page.evaluate(f"fn_link_page({pg})")
                page.wait_for_timeout(5000)
                items = parse_apt_table(page.content())
                print(f"    페이지 {pg}/{total}: {len(items)}건  (누적 {len(collected)+len(items)}건)")
                collected.extend(items)
                empty_streak = 0 if items else empty_streak + 1
                if empty_streak >= 5:
                    print(f"    5페이지 연속 0건 → 종료")
                    break
            except Exception as e:
                print(f"    페이지 {pg} 오류: {e}"); break
        return collected

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox","--disable-dev-shm-usage",
                  "--disable-blink-features=AutomationControlled"]
        )
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"),
            viewport={"width": 1440, "height": 900},
        )
        page = ctx.new_page()

        # ── 1단계: 넷퍼넬 통과 ────────────────────────────────────────────
        print("[1단계] 청약홈 로드 + 넷퍼넬 통과 (최대 120초)...")
        try:
            page.goto(BASE, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            print(f"  초기 goto: {e}")

        deadline = time.time() + 120
        passed   = False
        while time.time() < deadline:
            page.wait_for_timeout(3000)
            url_now = page.url
            title   = page.title()
            print(f"  [{int(deadline-time.time()):3}s] {title[:30]:30s} | {url_now[-50:]}")
            if "netFunnel" not in url_now and ("청약" in title or "apply" in url_now.lower()):
                print("  → 넷퍼넬 통과!")
                passed = True
                break
            if "chrome-error" in url_now:
                print("  ✗ 네트워크 오류 - 종료")
                ctx.close(); browser.close()
                return []

        if not passed:
            print("  ⚠️  시간 초과 (계속 진행)")
        page.wait_for_timeout(3000)

        # ── 2단계: 목록 유형별 전국 스크래핑 ──────────────────────────────
        for list_path, list_name in LIST_TYPES:
            print(f"\n=== [{list_name}] 전국 ===")
            try:
                page.goto(BASE + list_path, wait_until="networkidle", timeout=40000)
                page.wait_for_timeout(3000)
            except Exception as e:
                print(f"  페이지 이동 실패: {e}"); continue

            try:
                click_search(page)
                items = scrape_all_pages(page, list_name)
                all_items.extend(items)
                print(f"  [{list_name}] 전국 합계: {len(items)}건")
            except Exception as e:
                print(f"  오류: {e}")

        ctx.close()
        browser.close()

    return all_items

# ── 메인 ─────────────────────────────────────────────────────────────────────

def main():
    print(f"=== 청약홈 스크래퍼 v6 (전국) 시작: {TODAY} ===\n")

    try:
        from playwright.sync_api import sync_playwright  # noqa
        from bs4 import BeautifulSoup                    # noqa
    except ImportError as e:
        print(f"❌ 패키지 미설치: {e}")
        print("   pip3 install playwright beautifulsoup4")
        print("   python3 -m playwright install chromium")
        sys.exit(1)

    raw = scrape_with_playwright()
    print(f"\n[처리] raw {len(raw)}건")

    if not raw:
        print("\n⚠️  데이터 수집 실패 - 기존 cheongyak.json 유지")
        return

    # 기존 JSON 로드 (scraped_date 보존용)
    existing = {}
    if os.path.exists(OUT):
        try:
            with open(OUT) as f:
                for s in json.load(f).get("subscriptions", []):
                    existing[s["id"]] = s
        except: pass

    seen, items = set(), []
    for item in raw:
        if item["id"] in seen: continue
        seen.add(item["id"])
        if item["id"] in existing:
            item["scraped_date"] = existing[item["id"]]["scraped_date"]
        items.append(item)
        print(f"  ✓ {item['name'][:30]} [{item['region'][:2]}] "
              f"{item['start_date']}~{item['end_date']} [{item['status']}]")

    items.sort(key=lambda x: (x.get("start_date") or "9999", x.get("name","")))
    print(f"\n[결과] 유효 청약 {len(items)}건")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump({"updated": TODAY, "subscriptions": items}, f,
                  ensure_ascii=False, indent=2)
    print(f"[저장] {OUT}")
    print("=== 완료 ===")

if __name__ == "__main__":
    main()
