#!/usr/bin/env python3
"""
대법원 법원경매정보 스크래퍼
Target : 수도권 (서울/경기/인천) 아파트, 최저입찰가 4억 이상
Site   : https://www.courtauction.go.kr
"""

import requests
from bs4 import BeautifulSoup
import json, re, os, time
from datetime import date, datetime

BASE   = "https://www.courtauction.go.kr"
TODAY  = date.today().isoformat()
OUT    = os.path.join(os.path.dirname(__file__), "..", "data", "auctions.json")
MIN_BID = 400_000_000  # 4억

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

# 수도권 법원 → (표시명, 대법원 관할코드)
# 코드는 courtauction.go.kr 검색폼 option value 기준
COURTS = [
    ("서울중앙지방법원", "B000201"),
    ("서울동부지방법원", "B000202"),
    ("서울남부지방법원", "B000203"),
    ("서울북부지방법원", "B000204"),
    ("서울서부지방법원", "B000205"),
    ("수원지방법원",     "B000301"),
    ("수원지방법원 성남지원", "B000302"),
    ("수원지방법원 안양지원", "B000303"),
    ("수원지방법원 안산지원", "B000304"),
    ("수원지방법원 평택지원", "B000305"),
    ("수원지방법원 여주지원", "B000306"),
    ("의정부지방법원",   "B000401"),
    ("의정부지방법원 고양지원",   "B000402"),
    ("의정부지방법원 남양주지원", "B000403"),
    ("인천지방법원",     "B000501"),
    ("인천지방법원 부천지원", "B000502"),
]

# ── 유틸 ────────────────────────────────────────────────
def parse_amount(txt: str) -> int | None:
    """'15억 5,000만원' → 1,550,000,000"""
    if not txt:
        return None
    txt = txt.strip().replace(",", "").replace(" ", "")
    val = 0
    m = re.search(r"(\d+(?:\.\d+)?)억", txt)
    if m:
        val += int(float(m.group(1)) * 1e8)
    m = re.search(r"(\d+)만", txt)
    if m:
        val += int(m.group(1)) * 10_000
    if not val:
        m = re.search(r"(\d+)", txt)
        if m:
            val = int(m.group(1))
    return val if val else None

def parse_date(txt: str) -> str | None:
    """'2026.07.22' or '2026-07-22' → '2026-07-22'"""
    if not txt:
        return None
    m = re.search(r"(\d{4})[.\-/](\d{1,2})[.\-/](\d{1,2})", txt)
    if m:
        return f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"
    return None

def clean(txt: str) -> str:
    return " ".join(txt.split()) if txt else ""

# ── 세션 초기화 ─────────────────────────────────────────
def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    try:
        r = s.get(BASE + "/", timeout=30)
        print(f"[init] 메인페이지 status={r.status_code}")
        # 검색 페이지로 이동해서 추가 쿠키 확보
        r2 = s.get(BASE + "/pgj/pgj1001SelMulList.do", timeout=30)
        print(f"[init] 검색페이지 status={r2.status_code}")
        time.sleep(1)
    except Exception as e:
        print(f"[init] 오류: {e}")
    return s

# ── 검색 폼 탐색 ────────────────────────────────────────
def explore_search_form(session: requests.Session):
    """첫 실행 시 폼 구조를 로그로 출력 (디버깅용)"""
    try:
        r = session.get(BASE + "/pgj/pgj1001SelMulList.do", timeout=30)
        soup = BeautifulSoup(r.text, "html.parser")

        print("\n=== FORMS ===")
        for form in soup.find_all("form"):
            print(f"  form id={form.get('id')} name={form.get('name')} action={form.get('action')}")
            for inp in form.find_all(["input", "select"])[:20]:
                name = inp.get("name", "")
                val  = inp.get("value", "")
                tag  = inp.name
                print(f"    [{tag}] name={name!r} value={val!r}")

        print("\n=== SELECTS (법원/물건종류 추정) ===")
        for sel in soup.find_all("select"):
            n = sel.get("name", "")
            opts = [(o.get("value",""), clean(o.text)) for o in sel.find_all("option")]
            print(f"  select name={n!r}")
            for v, t in opts[:10]:
                print(f"    value={v!r}  text={t!r}")

        print("\n=== LINKS (경매 관련) ===")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if any(k in href for k in ["pgj", "auction", "search", "jemul"]):
                print(f"  {href[:100]}")

    except Exception as e:
        print(f"[explore] 오류: {e}")

# ── 검색 실행 ───────────────────────────────────────────
def search_one_court(session: requests.Session, court_name: str, court_code: str,
                     page: int = 1) -> list[dict]:
    """법원 1개, 1페이지 검색"""
    # 검색 POST 파라미터 (첫 실행 로그로 실제 파라미터 확인 후 수정)
    data = {
        "cortOfcCd":   court_code,   # 법원 코드
        "jemulGbCd_1": "001",        # 물건종류: 아파트 (추정)
        "lowsBidAmt":  str(MIN_BID), # 최저입찰가 이상
        "pageIndex":   str(page),
        "sortKey":     "auct_plc_loc",
        "sortOrder":   "ASC",
    }

    try:
        r = session.post(
            BASE + "/pgj/pgj1001SelMulList.do",
            data=data,
            timeout=30,
        )
        print(f"[search] {court_name} p{page} → status={r.status_code} len={len(r.text)}")
        return parse_results(r.text, court_name)
    except Exception as e:
        print(f"[search] {court_name} 오류: {e}")
        return []

# ── 결과 파싱 ───────────────────────────────────────────
def parse_results(html: str, court_name: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    results = []

    # 공통 패턴: <table> 에서 tr 추출
    tables = soup.find_all("table")
    print(f"  → table 수: {len(tables)}")

    for table in tables:
        rows = table.find_all("tr")
        for row in rows:
            cells = [clean(td.get_text()) for td in row.find_all(["td", "th"])]
            if len(cells) < 4:
                continue

            # 사건번호 패턴 탐색 (숫자타경숫자)
            case_no = None
            for cell in cells:
                m = re.search(r"\d{4}타경\d+", cell)
                if m:
                    case_no = m.group(0)
                    break
            if not case_no:
                continue

            # 주소 (보통 2~3번째 셀에 존재)
            addr = ""
            for cell in cells:
                if any(k in cell for k in ["서울", "경기", "인천"]) and len(cell) > 10:
                    addr = cell
                    break

            # 금액 파싱
            amounts = []
            for cell in cells:
                if re.search(r"\d+억|\d{8,}", cell.replace(",", "")):
                    a = parse_amount(cell)
                    if a and a >= 1_000_000:
                        amounts.append(a)

            appraisal = max(amounts) if len(amounts) >= 2 else None
            min_bid   = min(amounts) if len(amounts) >= 2 else (amounts[0] if amounts else None)

            # 최저가 필터
            if min_bid and min_bid < MIN_BID:
                continue

            # 날짜
            auction_date = None
            for cell in cells:
                d = parse_date(cell)
                if d:
                    auction_date = d
                    break

            # 유찰 횟수 (숫자+회)
            failed = 0
            for cell in cells:
                m = re.search(r"(\d+)회", cell)
                if m:
                    failed = int(m.group(1))
                    break

            item = {
                "id":           case_no,
                "court":        court_name,
                "address":      addr,
                "appraisal":    appraisal,
                "min_bid":      min_bid,
                "auction_date": auction_date,
                "failed_bids":  failed,
                "bid_ratio":    round(min_bid / appraisal * 100, 1) if (min_bid and appraisal) else None,
                "scraped_date": TODAY,
            }
            results.append(item)
            print(f"  ✓ {case_no} {addr[:30]} 최저:{min_bid}")

    return results

# ── 메인 ─────────────────────────────────────────────────
def main():
    print(f"=== 경매 스크래퍼 시작: {TODAY} ===")
    session = make_session()

    # 첫 실행 시 폼 구조 탐색 (로그 확인용)
    explore_search_form(session)

    # 기존 데이터 로드
    existing = {}
    if os.path.exists(OUT):
        try:
            with open(OUT) as f:
                d = json.load(f)
            for item in d.get("auctions", []):
                existing[item["id"]] = item
            print(f"[load] 기존 데이터 {len(existing)}건")
        except Exception as e:
            print(f"[load] 오류: {e}")

    # 수도권 법원 순회
    new_items = {}
    for court_name, court_code in COURTS:
        items = search_one_court(session, court_name, court_code, page=1)
        # 여러 페이지가 있으면 2페이지까지 시도
        if len(items) >= 10:
            items += search_one_court(session, court_name, court_code, page=2)
        for item in items:
            if item["id"] not in new_items:
                new_items[item["id"]] = item
        time.sleep(1.5)

    print(f"\n[결과] 신규 수집 {len(new_items)}건")

    # 병합 (기존 + 신규, 신규 우선)
    merged = {**existing, **new_items}

    # 아파트·수도권 필터 (주소 기반 2차 필터)
    def is_metro_apt(item):
        addr = item.get("address", "")
        return any(k in addr for k in ["서울", "경기", "인천"])

    final = [v for v in merged.values() if is_metro_apt(v) or not v.get("address")]

    # 날짜순 정렬
    final.sort(key=lambda x: (x.get("scraped_date",""), x.get("auction_date","")), reverse=True)

    out_data = {"updated": TODAY, "auctions": final}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out_data, f, ensure_ascii=False, indent=2)

    print(f"[저장] {OUT} — 총 {len(final)}건")
    print("=== 완료 ===")

if __name__ == "__main__":
    main()
