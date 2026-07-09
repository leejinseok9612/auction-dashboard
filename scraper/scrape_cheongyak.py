"""
청약홈 분양정보 + 경쟁률 스크래퍼
공공데이터포털 API: 한국부동산원_청약홈 분양정보 조회 서비스
Base URL: https://api.odcloud.kr/api
API 키 발급: https://www.data.go.kr/data/15098547/openapi.do
GitHub Secret 이름: CHEONGYAK_API_KEY
"""

import os, json, requests, time
from datetime import datetime, date

API_KEY   = os.environ.get("CHEONGYAK_API_KEY", "")
BASE      = "https://api.odcloud.kr/api/ApplyhomeInfoDetailSvc/v1"
TODAY_STR = date.today().isoformat()

ENDPOINTS = [
    ("getAPTLttotPblancDetail",        "APT"),
    ("getUrbtyOfctlLttotPblancDetail", "오피스텔/도시형"),
    ("getRemndrLttotPblancDetail",     "무순위/잔여"),
    ("getPblPvtRentLttotPblancDetail", "공공지원민간임대"),
]

# ── 페이지 요청 ─────────────────────────────────────────────
def fetch_page(endpoint, page=1, per_page=100):
    url = f"{BASE}/{endpoint}"
    params = {"serviceKey": API_KEY, "page": page, "perPage": per_page}
    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        return data.get("data", []), data.get("totalCount", 0)
    except Exception as e:
        print(f"  [{endpoint}] page {page} 실패: {e}")
        return [], 0

# ── 경쟁률 조회 ─────────────────────────────────────────────
def fetch_competition():
    """청약중 항목의 경쟁률 조회 (공고번호 → {일반, 특별})"""
    comp = {}
    # 두 가지 엔드포인트 시도 (API마다 이름이 다를 수 있음)
    for ep in ["getAPTRcritPblancMdl", "getAPTLttotPblancMdl"]:
        try:
            r = requests.get(
                f"{BASE}/{ep}",
                params={"serviceKey": API_KEY, "page": 1, "perPage": 100},
                timeout=15
            )
            data = r.json().get("data", [])
            if not data:
                continue
            for item in data:
                pno   = str(item.get("공고번호") or item.get("PBLANC_NO") or "")
                stype = str(item.get("공급유형코드명") or item.get("공급유형") or "일반")
                rate  = item.get("경쟁률") or item.get("COMPT_RATE") or 0
                try: rate = float(rate)
                except: rate = 0
                if pno:
                    if pno not in comp: comp[pno] = {}
                    comp[pno][stype] = rate
            if comp:
                print(f"  경쟁률 {len(comp)}건 수집 (endpoint: {ep})")
                return comp
        except Exception as e:
            print(f"  경쟁률 [{ep}] 실패: {e}")
    return comp

# ── 날짜 정규화 ─────────────────────────────────────────────
def fd(s):
    s = str(s or "").replace("-","").replace(".","").replace("/","")
    if len(s) == 8:
        try: return datetime.strptime(s, "%Y%m%d").strftime("%Y-%m-%d")
        except: pass
    return s

# ── 상태 계산 ───────────────────────────────────────────────
def calc_status(start, end):
    try:
        ed = date.fromisoformat(end)
        sd = date.fromisoformat(start)
        today = date.today()
        if ed < today:    return "마감"
        if sd <= today:   return "청약중"
        return "청약예정"
    except:
        return "미정"

# ── JSON 파싱 ───────────────────────────────────────────────
def parse_item(raw, house_type):
    def g(k): return str(raw.get(k) or "").strip()

    start   = fd(g("청약접수시작일") or g("SUBSCRPT_RCEPT_BGNDE"))
    end     = fd(g("청약접수종료일") or g("SUBSCRPT_RCEPT_ENDDE"))
    region  = g("공급지역명")  or g("SUBSCRPT_AREA_CODE_NM")
    address = g("공급위치")    or g("HSSPLY_ADRES")
    name    = g("주택명")      or g("HOUSE_NM")
    builder = g("사업주체명")  or g("BSNS_MBY_NM")
    htype   = g("주택구분")    or g("HOUSE_SECD_NM") or house_type
    pblanc  = g("공고번호")    or g("PBLANC_NO")

    def price_int(k):
        try: return int(str(raw.get(k,"0") or 0).replace(",",""))
        except: return 0

    parts    = address.split()
    district = parts[1] if len(parts) > 1 else ""

    return {
        "id":            pblanc or (name + start),
        "pblanc_no":     pblanc,
        "name":          name,
        "type":          htype,
        "builder":       builder,
        "region":        region,
        "district":      district,
        "address":       address,
        "supply_count":  int(g("공급세대수") or g("TOT_SUPLY_HSHLDCO") or 0),
        "price_min":     price_int("최저분양가"),
        "price_max":     price_int("최고분양가") or price_int("분양가상한액"),
        "announce_date": fd(g("모집공고일")    or g("RCRIT_PBLANC_DE")),
        "start_date":    start,
        "end_date":      end,
        "win_date":      fd(g("당첨자발표일")  or g("PRZWNER_PRESNATN_DE")),
        "move_in":       g("입주예정월")        or g("MVIN_PREARNGE_YM"),
        "status":        calc_status(start, end),
        "url":           g("홈페이지주소")      or g("HMPG_ADRES") or "https://www.applyhome.co.kr",
        "competition":   {},          # 경쟁률: {일반: X.X, 특별: X.X}
        "lat":           None,        # Nominatim 지오코딩
        "lng":           None,
        "scraped_date":  TODAY_STR,
    }

# ── Nominatim 지오코딩 ───────────────────────────────────────
_geocode_cache = {}

def geocode(address):
    """주소 → (lat, lng) — Nominatim, 1.2초 딜레이"""
    if not address: return None, None
    if address in _geocode_cache:
        return _geocode_cache[address]
    time.sleep(1.2)
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": address, "format": "json", "limit": 1, "countrycodes": "kr"},
            headers={"User-Agent": "auction-dashboard/1.0"},
            timeout=8,
        )
        data = r.json()
        if data:
            lat = round(float(data[0]["lat"]), 6)
            lng = round(float(data[0]["lon"]), 6)
            _geocode_cache[address] = (lat, lng)
            return lat, lng
    except Exception as e:
        print(f"    geocode 실패 ({address[:20]}..): {e}")
    _geocode_cache[address] = (None, None)
    return None, None

# ── 메인 ────────────────────────────────────────────────────
def run():
    if not API_KEY:
        print("❌ CHEONGYAK_API_KEY 없음 — GitHub Secret 추가 필요")
        return

    print(f"▶ 청약 데이터 수집 시작 ({TODAY_STR})")

    # 기존 데이터 로드 (지오코딩 캐시 및 병합용)
    out_path = "data/cheongyak.json"
    existing_map = {}
    try:
        with open(out_path, "r", encoding="utf-8") as f:
            for e in json.load(f).get("subscriptions", []):
                if e.get("id"):
                    existing_map[e["id"]] = e
                    # 기존 지오코딩 캐시 복원
                    if e.get("address") and e.get("lat"):
                        _geocode_cache[e["address"]] = (e["lat"], e["lng"])
    except:
        pass
    print(f"  기존 데이터: {len(existing_map)}건 로드")

    # ── 분양정보 수집 ──────────────────────────────────────
    all_items = []
    for endpoint, htype in ENDPOINTS:
        print(f"\n  [{htype}]")
        pg = 1
        while True:
            items_raw, total = fetch_page(endpoint, page=pg)
            if not items_raw: break
            parsed = [parse_item(r, htype) for r in items_raw]
            active = [p for p in parsed if p["status"] != "마감"]
            all_items.extend(active)
            print(f"    page {pg}: {len(items_raw)}건 → 유효 {len(active)}건 / 전체 {total}건")
            if len(items_raw) < 100 or pg * 100 >= total: break
            pg += 1
            time.sleep(0.3)

    # ── 경쟁률 수집 ────────────────────────────────────────
    print("\n  [경쟁률 조회]")
    comp_map = fetch_competition()

    # ── 지오코딩 (새 항목만) ────────────────────────────────
    new_items = [i for i in all_items if i["id"] not in existing_map or
                 not existing_map[i["id"]].get("lat")]
    print(f"\n  [지오코딩] 새 항목 {len(new_items)}건 (약 {len(new_items)*1.2:.0f}초 소요)")
    for i, item in enumerate(new_items):
        lat, lng = geocode(item["address"])
        item["lat"] = lat
        item["lng"] = lng
        if lat: print(f"    {i+1}/{len(new_items)} {item['name'][:20]} → ({lat}, {lng})")

    # ── 병합 ───────────────────────────────────────────────
    for item in all_items:
        # 경쟁률 붙이기
        if item["pblanc_no"] in comp_map:
            item["competition"] = comp_map[item["pblanc_no"]]
        # 기존 지오코딩 유지
        old = existing_map.get(item["id"], {})
        if not item.get("lat") and old.get("lat"):
            item["lat"]  = old["lat"]
            item["lng"]  = old["lng"]
        existing_map[item["id"]] = item

    merged = sorted(existing_map.values(),
                    key=lambda x: x.get("start_date", ""), reverse=True)

    os.makedirs("data", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"updated": TODAY_STR, "subscriptions": merged},
                  f, ensure_ascii=False, indent=2)

    open_count = sum(1 for x in merged if x.get("status") in ["청약중","청약예정"])
    print(f"\n✅ 저장 완료: {out_path} (전체 {len(merged)}건, 유효 {open_count}건)")

if __name__ == "__main__":
    run()
