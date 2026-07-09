"""
청약홈 분양정보 스크래퍼
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

# 수집할 엔드포인트 목록
ENDPOINTS = [
    ("getAPTLttotPblancDetail",          "APT"),
    ("getUrbtyOfctlLttotPblancDetail",   "오피스텔/도시형"),
    ("getRemndrLttotPblancDetail",       "무순위/잔여"),
    ("getPblPvtRentLttotPblancDetail",   "공공지원민간임대"),
]

def fetch_page(endpoint, page=1, per_page=100):
    url = f"{BASE}/{endpoint}"
    params = {
        "serviceKey": API_KEY,
        "page":       page,
        "perPage":    per_page,
    }
    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        return data.get("data", []), data.get("totalCount", 0)
    except Exception as e:
        print(f"  [{endpoint}] 페이지 {page} 실패: {e}")
        return [], 0

def parse_item(raw, house_type):
    """공공데이터 JSON → 통일 dict"""
    def g(k): return str(raw.get(k) or "").strip()

    # 날짜 정규화 (YYYYMMDD → YYYY-MM-DD)
    def fd(s):
        s = s.replace("-","").replace(".","").replace("/","")
        if len(s)==8:
            try: return datetime.strptime(s,"%Y%m%d").strftime("%Y-%m-%d")
            except: pass
        return s

    start = fd(g("청약접수시작일") or g("SUBSCRPT_RCEPT_BGNDE"))
    end   = fd(g("청약접수종료일") or g("SUBSCRPT_RCEPT_ENDDE"))

    # 상태 계산
    try:
        ed = date.fromisoformat(end)
        sd = date.fromisoformat(start)
        today = date.today()
        if ed < today:   status = "마감"
        elif sd <= today: status = "청약중"
        else:             status = "청약예정"
    except:
        status = "미정"

    # 분양가 (만원 단위)
    def price_int(k):
        try: return int(str(raw.get(k,"0")).replace(",","").replace("만원","") or 0)
        except: return 0

    region  = g("공급지역명") or g("SUBSCRPT_AREA_CODE_NM")
    address = g("공급위치")   or g("HSSPLY_ADRES")
    name    = g("주택명")     or g("HOUSE_NM")
    builder = g("사업주체명") or g("BSNS_MBY_NM")
    htype   = g("주택구분")   or g("HOUSE_SECD_NM") or house_type

    # district: address 두 번째 단어
    parts    = address.split()
    district = parts[1] if len(parts) > 1 else ""

    return {
        "id":            g("공고번호") or g("PBLANC_NO") or name+start,
        "name":          name,
        "type":          htype,
        "builder":       builder,
        "region":        region,
        "district":      district,
        "address":       address,
        "supply_count":  int(g("공급세대수") or g("TOT_SUPLY_HSHLDCO") or 0),
        "price_min":     price_int("최저분양가"),
        "price_max":     price_int("최고분양가") or price_int("분양가상한액"),
        "announce_date": fd(g("모집공고일") or g("RCRIT_PBLANC_DE")),
        "start_date":    start,
        "end_date":      end,
        "win_date":      fd(g("당첨자발표일") or g("PRZWNER_PRESNATN_DE")),
        "move_in":       g("입주예정월") or g("MVIN_PREARNGE_YM"),
        "status":        status,
        "url":           g("홈페이지주소") or g("HMPG_ADRES") or "https://www.applyhome.co.kr",
        "scraped_date":  TODAY_STR,
    }

def run():
    if not API_KEY:
        print("❌ CHEONGYAK_API_KEY 환경변수 없음")
        print("   → GitHub Settings > Secrets 에 CHEONGYAK_API_KEY 추가 필요")
        return

    print(f"▶ 청약 데이터 수집 시작 ({TODAY_STR})")
    all_items = []

    for endpoint, htype in ENDPOINTS:
        print(f"\n  [{htype}] {endpoint}")
        pg = 1
        while True:
            items_raw, total = fetch_page(endpoint, page=pg)
            if not items_raw:
                break
            parsed = [parse_item(r, htype) for r in items_raw]
            # 마감 제외 (현재 + 예정만 수집)
            active = [p for p in parsed if p["status"] != "마감"]
            all_items.extend(active)
            print(f"    page {pg}: {len(items_raw)}건 수신, {len(active)}건 유효 (전체 {total}건)")
            if len(items_raw) < 100 or pg * 100 >= total:
                break
            pg += 1
            time.sleep(0.3)

    # 중복 제거 (id 기준)
    seen = {}
    for item in all_items:
        key = item["id"] or item["name"]
        if key not in seen:
            seen[key] = item
    unique = list(seen.values())

    # 기존 데이터와 병합
    out_path = "data/cheongyak.json"
    existing_map = {}
    try:
        with open(out_path, "r", encoding="utf-8") as f:
            for e in json.load(f).get("subscriptions", []):
                if e.get("id"):
                    existing_map[e["id"]] = e
    except:
        pass

    for item in unique:
        existing_map[item["id"]] = item

    merged = sorted(existing_map.values(),
                    key=lambda x: x.get("start_date",""), reverse=True)

    os.makedirs("data", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump({"updated": TODAY_STR, "subscriptions": merged},
                  f, ensure_ascii=False, indent=2)

    print(f"\n✅ 저장 완료: {out_path} ({len(merged)}건)")

if __name__ == "__main__":
    run()
