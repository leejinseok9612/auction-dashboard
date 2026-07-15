"""
청약홈 분양정보 스크래퍼 v4
- requests로 청약HOME API 직접 POST 호출
- API 키 있으면 공공데이터포털 API 사용
"""

import os, json, time, re
import urllib.request, urllib.parse
from datetime import datetime, date

TODAY_STR = date.today().isoformat()
OUT_PATH  = os.path.join(os.path.dirname(__file__), "..", "docs", "data", "cheongyak.json")

# API 키: 환경변수 → .local_config 파일 순서로 읽기
def _load_api_key():
    key = os.environ.get("CHEONGYAK_API_KEY", "")
    if key:
        return key
    config_path = os.path.join(os.path.dirname(__file__), "..", ".local_config")
    try:
        with open(config_path, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("CHEONGYAK_API_KEY="):
                    return line.split("=", 1)[1].strip()
    except:
        pass
    return ""

API_KEY = _load_api_key()

BASE_URL  = "https://www.applyhome.co.kr"

HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept":          "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Content-Type":    "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With":"XMLHttpRequest",
    "Referer":         "https://www.applyhome.co.kr/ai/aia/selectAPTLttotPblancListView.do",
    "Origin":          "https://www.applyhome.co.kr",
}

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

# ── POST 요청 헬퍼 ───────────────────────────────────────────
def post(path, body_dict):
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    body = urllib.parse.urlencode(body_dict).encode("utf-8")
    req  = urllib.request.Request(BASE_URL + path, data=body, headers=HEADERS, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  POST {path} 실패: {e}")
        return ""

# ── 범용 파싱 ────────────────────────────────────────────────
def parse_rows(rows, htype="APT"):
    items = []
    for raw in rows:
        if not isinstance(raw, dict): continue

        def g(*keys):
            for k in keys:
                v = raw.get(k)
                if v is not None and str(v).strip() not in ("", "null", "None"):
                    return str(v).strip()
            return ""

        name  = g("HOUSE_NM","housNm","houseNm","APT_NM","aptNm")
        start = fd(g("SUBSCRPT_RCEPT_BGNDE","sbscrptRceptBgnde","RCEPT_BGNDE","rceptBgnde"))
        end   = fd(g("SUBSCRPT_RCEPT_ENDDE","sbscrptRceptEndde","RCEPT_ENDDE","rceptEndde"))
        pbno  = g("PBLANC_NO","pblancNo","HOUSE_MANAGE_NO","houseManageNo")
        addr  = g("HSSPLY_ADRES","hssplyAdres","ADDRESS","address","공급위치")
        rgn   = g("SUBSCRPT_AREA_CODE_NM","subscrptAreaCodeNm","SIDO","sido","지역")
        bldr  = g("BSNS_MBY_NM","bsnsMbyNm","CMPNY_NM","cmpnyNm","시행사")

        if not name: continue
        st = calc_status(start, end)
        if st not in ("청약중","청약예정"): continue

        parts = addr.split()
        items.append({
            "id":            pbno or (name + start),
            "pblanc_no":     pbno,
            "name":          name,
            "type":          g("HOUSE_SECD_NM","houseSecdNm","HOUSE_DTL_SECD_NM") or htype,
            "builder":       bldr,
            "region":        rgn or (parts[0] if parts else ""),
            "district":      parts[1] if len(parts) > 1 else "",
            "address":       addr,
            "supply_count":  int(g("TOT_SUPLY_HSHLDCO","totSuplyHshldco","SUPLY_HSHLDCO","supplyCount") or 0),
            "announce_date": fd(g("RCRIT_PBLANC_DE","rcritPblancDe")),
            "start_date":    start,
            "end_date":      end,
            "win_date":      fd(g("PRZWNER_PRESNATN_DE","przwnerPresnatnDe")),
            "move_in":       g("MVIN_PREARNGE_YM","mvinPrearngeYm"),
            "status":        st,
            "url":           g("HMPG_ADRES","hmpgAdres") or BASE_URL,
            "competition":   {},
            "lat":           None,
            "lng":           None,
            "scraped_date":  TODAY_STR,
        })
    return items

# ══════════════════════════════════════════════════════════════
#  청약HOME 직접 POST
# ══════════════════════════════════════════════════════════════
def scrape_applyhome_direct():
    results = []

    # ── APT 분양정보 ──────────────────────────────────────────
    endpoints = [
        # (경로, body_params, 목록키들, 설명)
        (
            "/ai/aia/selectAPTLttotPblancListZ.do",
            {"sido":"","gugun":"","APT_NM":"","houseSecd":"","nowSuplyYmd":"",
             "sort":"3","startPage":"1","itemsPerPage":"100",
             "totalPages":"0","totalItems":"0"},
            ["dltAPTLttotPblancList","aptList","list","data"],
            "APT",
        ),
        (
            "/ai/aia/selectUrbtyOfctlLttotPblancListZ.do",
            {"sido":"","gugun":"","APT_NM":"","houseSecd":"",
             "sort":"3","startPage":"1","itemsPerPage":"100",
             "totalPages":"0","totalItems":"0"},
            ["dltUrbtyOfctlLttotPblancList","list","data"],
            "오피스텔/도시형",
        ),
        (
            "/ai/aia/selectRemndrLttotPblancListZ.do",
            {"sido":"","gugun":"","APT_NM":"","houseSecd":"",
             "sort":"3","startPage":"1","itemsPerPage":"100",
             "totalPages":"0","totalItems":"0"},
            ["dltRemndrLttotPblancList","list","data"],
            "무순위/잔여",
        ),
    ]

    for path, body, list_keys, htype in endpoints:
        print(f"  [{htype}] POST {path[-40:]}")
        raw_text = post(path, body)
        if not raw_text:
            continue

        try:
            data = json.loads(raw_text)
        except:
            print(f"    JSON 파싱 실패 (응답 길이: {len(raw_text)})")
            print(f"    응답 앞부분: {raw_text[:200]}")
            continue

        # 여러 가능한 키 시도
        rows = []
        for key in list_keys:
            val = data.get(key)
            if isinstance(val, list) and val:
                rows = val
                print(f"    키 '{key}' 에서 {len(rows)}건 발견")
                break

        # 중첩 구조 탐색
        if not rows:
            for k, v in data.items():
                if isinstance(v, list) and len(v) > 0 and isinstance(v[0], dict):
                    rows = v
                    print(f"    자동감지 키 '{k}' 에서 {len(rows)}건 발견")
                    break

        if not rows:
            print(f"    데이터 없음. 응답 키: {list(data.keys())[:10]}")
            continue

        parsed = parse_rows(rows, htype)
        print(f"    → 청약중/예정 {len(parsed)}건")
        results.extend(parsed)

    return results

# ══════════════════════════════════════════════════════════════
#  방법 2: 공공데이터포털 API
# ══════════════════════════════════════════════════════════════
def scrape_via_api():
    import ssl
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    BASE = "https://api.odcloud.kr/api/ApplyhomeInfoDetailSvc/v1"
    ENDPOINTS = [
        ("getAPTLttotPblancDetail",        "APT"),
        ("getUrbtyOfctlLttotPblancDetail", "오피스텔/도시형"),
        ("getRemndrLttotPblancDetail",     "무순위/잔여"),
    ]

    all_items = []
    for ep, htype in ENDPOINTS:
        print(f"  [{htype}]")
        pg = 1
        while True:
            url = f"{BASE}/{ep}?serviceKey={urllib.parse.quote(API_KEY)}&page={pg}&perPage=100"
            req = urllib.request.Request(url, headers={"Accept":"application/json","User-Agent":"Mozilla/5.0"})
            try:
                with urllib.request.urlopen(req, timeout=20, context=ctx) as resp:
                    d = json.loads(resp.read().decode())
                rows = d.get("data", [])
                total = d.get("totalCount", 0)
            except Exception as e:
                print(f"    page {pg} 실패: {e}")
                break

            parsed = parse_rows(rows, htype)
            all_items.extend(parsed)
            print(f"    page {pg}: {len(rows)}건 → 유효 {len(parsed)}건 (전체 {total}건)")
            if len(rows) < 100 or pg * 100 >= total: break
            pg += 1
            time.sleep(0.3)

    return all_items

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
        print("  기존 데이터 없음")

    # 수집
    items = []
    if API_KEY:
        print("\n[방법 1] 공공데이터포털 API 사용")
        try:
            items = scrape_via_api()
        except Exception as e:
            print(f"  API 실패: {e}")

    if not items:
        print("\n[방법 2] 청약HOME 직접 요청")
        items = scrape_applyhome_direct()

    # 기존 지오코딩 이어받기
    for item in items:
        old = existing_map.get(item["id"], {})
        if not item.get("lat") and old.get("lat"):
            item["lat"] = old["lat"]
            item["lng"] = old["lng"]

    # 마감 제거 + 정렬
    active = [i for i in items if i.get("status") in ("청약중","청약예정")]
    active.sort(key=lambda x: (x.get("status","")=="청약중", x.get("start_date","")), reverse=True)

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump({"updated": TODAY_STR, "subscriptions": active},
                  f, ensure_ascii=False, indent=2)

    now  = sum(1 for x in active if x.get("status") == "청약중")
    soon = sum(1 for x in active if x.get("status") == "청약예정")
    print(f"\n✅ 저장 완료 → {OUT_PATH}")
    print(f"   청약중: {now}건 / 청약예정: {soon}건 / 합계: {len(active)}건")
    for a in active[:5]:
        print(f"   [{a['status']}] {a['name']} ({a['start_date']}~{a['end_date']})")

if __name__ == "__main__":
    run()
