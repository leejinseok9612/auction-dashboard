"""
청약홈 분양정보 스크래퍼
공공데이터포털 API: 한국부동산원_청약홈 분양정보 조회 서비스
API Key 발급: https://www.data.go.kr/data/15098547/openapi.do
GitHub Secret 이름: CHEONGYAK_API_KEY
"""

import os, json, requests, time
from datetime import datetime, date
from xml.etree import ElementTree as ET

API_KEY   = os.environ.get("CHEONGYAK_API_KEY", "")
BASE_URL  = "http://openapi.reb.or.kr/OpenAPI_ToolInstallPackage/service/rest/ApplyhomeInfoDetailSvc"
TODAY_STR = date.today().isoformat()

# 주택 구분 코드 → 모든 타입
HOUSE_TYPES = {
    "01": "APT",
    "03": "오피스텔",
    "04": "도시형",
    "05": "민간임대",
    "06": "공공임대",
}

ENDPOINTS = {
    "APT":    "getAPTLttotPblancDetail",
    "오피스텔":  "getULttotPblancDetail",
    "도시형":   "getULttotPblancDetail",
    "무순위":   "getNHULttotPblancDetail",
}

def fetch_apt(page=1, rows=100):
    """아파트 분양정보 조회"""
    url = f"{BASE_URL}/getAPTLttotPblancDetail"
    params = {
        "serviceKey": API_KEY,
        "pageNo":     page,
        "numOfRows":  rows,
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        return parse_xml(r.text, "APT")
    except Exception as e:
        print(f"[APT] 조회 실패: {e}")
        return []

def fetch_etc(page=1, rows=100):
    """오피스텔·도시형 분양정보 조회"""
    url = f"{BASE_URL}/getULttotPblancDetail"
    params = {
        "serviceKey": API_KEY,
        "pageNo":     page,
        "numOfRows":  rows,
    }
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        return parse_xml(r.text, "오피스텔/도시형")
    except Exception as e:
        print(f"[ETC] 조회 실패: {e}")
        return []

def parse_xml(xml_text, house_type):
    """XML 파싱 → 공통 dict 변환"""
    items = []
    try:
        root = ET.fromstring(xml_text)
        for item in root.iter("item"):
            def g(tag):
                el = item.find(tag)
                return el.text.strip() if el is not None and el.text else ""

            # 분양가 (만원 단위로 저장)
            price_max_raw = g("LTTOT_TOP_AMOUNT") or g("HOUSE_SUPLY_AMOUNT")
            try:
                price_max = int(price_max_raw.replace(",", ""))
            except:
                price_max = 0

            # 접수일자
            start_raw = g("SUBSCRPT_RCEPT_BGNDE")
            end_raw   = g("SUBSCRPT_RCEPT_ENDDE")

            # 마감 여부 판단
            try:
                end_date = datetime.strptime(end_raw, "%Y%m%d").date()
                status = "마감" if end_date < date.today() else (
                    "청약중" if datetime.strptime(start_raw, "%Y%m%d").date() <= date.today() else "청약예정"
                )
            except:
                status = "미정"

            def fmt_date(raw):
                try:
                    return datetime.strptime(raw, "%Y%m%d").strftime("%Y-%m-%d")
                except:
                    return raw

            # 지역 파싱
            region    = g("SUBSCRPT_AREA_CODE_NM") or g("SIDO")
            address   = g("HSSPLY_ADRES")
            district  = ""
            if address:
                parts = address.split()
                district = parts[1] if len(parts) > 1 else ""

            items.append({
                "id":            g("PBLANC_NO") or g("HOUSE_MANAGE_NO"),
                "name":          g("HOUSE_NM"),
                "type":          g("HOUSE_SECD_NM") or house_type,
                "builder":       g("BSNS_MBY_NM"),
                "region":        region,
                "district":      district,
                "address":       address,
                "supply_count":  int(g("TOT_SUPLY_HSHLDCO") or 0),
                "price_min":     0,
                "price_max":     price_max,
                "announce_date": fmt_date(g("RCRIT_PBLANC_DE")),
                "start_date":    fmt_date(start_raw),
                "end_date":      fmt_date(end_raw),
                "win_date":      fmt_date(g("PRZWNER_PRESNATN_DE")),
                "move_in":       g("MVIN_PREARNGE_YM"),
                "status":        status,
                "url":           g("HMPG_ADRES") or "https://www.applyhome.co.kr",
                "scraped_date":  TODAY_STR,
            })
    except Exception as e:
        print(f"XML 파싱 오류: {e}")
    return items

def run():
    if not API_KEY:
        print("❌ CHEONGYAK_API_KEY 환경변수가 없습니다.")
        print("   → data.go.kr에서 API 키를 발급받고 GitHub Secret에 추가하세요.")
        print("   → https://www.data.go.kr/data/15098547/openapi.do")
        return

    print("▶ 청약 데이터 수집 시작...")
    all_items = []

    # APT
    print("  아파트 분양정보 수집 중...")
    pg = 1
    while True:
        batch = fetch_apt(page=pg, rows=100)
        if not batch:
            break
        all_items.extend(batch)
        print(f"    page {pg}: {len(batch)}건")
        if len(batch) < 100:
            break
        pg += 1
        time.sleep(0.5)

    # 오피스텔·도시형
    print("  오피스텔/도시형 분양정보 수집 중...")
    pg = 1
    while True:
        batch = fetch_etc(page=pg, rows=100)
        if not batch:
            break
        all_items.extend(batch)
        print(f"    page {pg}: {len(batch)}건")
        if len(batch) < 100:
            break
        pg += 1
        time.sleep(0.5)

    # 중복 제거
    seen = set()
    unique = []
    for item in all_items:
        key = item["id"] or item["name"]
        if key and key not in seen:
            seen.add(key)
            unique.append(item)

    # 마감 제외 (최근 30일 마감 포함)
    active = [i for i in unique if i["status"] != "마감"]

    print(f"\n✅ 총 {len(all_items)}건 수집 → 중복 제거 후 {len(unique)}건 → 유효 {len(active)}건")

    # 기존 데이터 병합 (누적)
    out_path = "data/cheongyak.json"
    existing = []
    try:
        with open(out_path, "r", encoding="utf-8") as f:
            existing = json.load(f).get("subscriptions", [])
    except:
        pass

    # 기존 중 최근 7일 내 scraped_date만 유지 + 오늘 데이터로 업데이트
    existing_map = {e["id"]: e for e in existing if e.get("id")}
    for item in active:
        existing_map[item["id"]] = item

    merged = sorted(existing_map.values(),
                    key=lambda x: x.get("start_date",""), reverse=True)

    result = {
        "updated": TODAY_STR,
        "subscriptions": merged
    }

    os.makedirs("data", exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(f"💾 {out_path} 저장 완료 ({len(merged)}건)")

if __name__ == "__main__":
    run()
