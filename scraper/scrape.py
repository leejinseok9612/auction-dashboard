#!/usr/bin/env python3
import requests
from bs4 import BeautifulSoup
import json, os, re, time, logging
from datetime import datetime, timezone, timedelta

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

BASE_URL = "https://www.courtauction.go.kr"
DATA_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "auctions.json")
MIN_BID_AMOUNT = 400_000_000
KST = timezone(timedelta(hours=9))

SEOUL_COURTS = {
    "서울중앙지방법원": "B000201",
    "서울동부지방법원": "B000205",
    "서울남부지방법원": "B000206",
    "서울북부지방법원": "B000204",
    "서울서부지방법원": "B000207",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8",
    "Referer": BASE_URL,
}

def parse_amount(text):
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else 0

def to_kst_str():
    return datetime.now(KST).strftime("%Y-%m-%d")

def load_existing_data():
    if os.path.exists(DATA_PATH):
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"last_updated": "", "total_count": 0, "auctions": []}

def save_data(data):
    os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)
    with open(DATA_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"저장 완료: {DATA_PATH}")

def get_session():
    sess = requests.Session()
    sess.headers.update(HEADERS)
    try:
        sess.get(BASE_URL, timeout=20)
        logger.info("세션 초기화 완료")
    except Exception as e:
        logger.warning(f"세션 초기화 실패: {e}")
    return sess

def search_court_auctions(sess, court_name, court_code):
    results = []
    search_url = f"{BASE_URL}/pgj/pgj002.on"
    payload = {
        "admCd": court_code,
        "realEstateSe": "1",
        "srchLwstPrc": str(MIN_BID_AMOUNT),
        "srchUprPrc": "",
        "curPage": "1",
        "pgSz": "100",
        "srchWrd": "서울",
    }
    try:
        resp = sess.post(search_url, data=payload, timeout=30)
        resp.raise_for_status()
        items = parse_search_results(resp.text, court_name)
        logger.info(f"  {court_name}: {len(items)}건")
        results.extend(items)
    except requests.RequestException as e:
        logger.error(f"  {court_name} 실패: {e}")
    return results

def parse_search_results(html, court_name):
    soup = BeautifulSoup(html, "html.parser")
    items = []
    today = to_kst_str()
    rows = soup.select("table.tbl_list tbody tr, table tbody tr")
    for row in rows:
        cols = row.find_all("td")
        if len(cols) < 6:
            continue
        try:
            case_id   = cols[0].get_text(strip=True)
            obj_type  = cols[1].get_text(strip=True)
            address   = cols[2].get_text(strip=True)
            appraisal = parse_amount(cols[3].get_text())
            min_bid   = parse_amount(cols[4].get_text())
            auction_dt= cols[5].get_text(strip=True) if len(cols) > 5 else ""
            failed    = parse_amount(cols[6].get_text()) if len(cols) > 6 else 0
            if min_bid < MIN_BID_AMOUNT:
                continue
            if "아파트" not in obj_type:
                continue
            items.append({
                "id": case_id, "court": court_name, "type": obj_type,
                "address": address, "appraisal": appraisal, "min_bid": min_bid,
                "auction_date": auction_dt, "failed_bids": failed,
                "scraped_date": today,
                "bid_ratio": round(min_bid / appraisal * 100, 1) if appraisal else None,
            })
        except Exception as e:
            logger.debug(f"파싱 오류: {e}")
    return items

def merge_auctions(existing, new_items):
    existing_ids = {item["id"] for item in existing}
    added = 0
    for item in new_items:
        if item["id"] not in existing_ids:
            existing.append(item)
            existing_ids.add(item["id"])
            added += 1
    logger.info(f"신규: {added}건 추가")
    return existing

def main():
    logger.info(f"스크래핑 시작: {to_kst_str()}")
    sess = get_session()
    all_new = []
    for court_name, court_code in SEOUL_COURTS.items():
        logger.info(f"[{court_name}] 검색 중...")
        items = search_court_auctions(sess, court_name, court_code)
        all_new.extend(items)
        time.sleep(1.5)
    data = load_existing_data()
    data["auctions"] = merge_auctions(data.get("auctions", []), all_new)
    data["last_updated"] = to_kst_str()
    data["total_count"] = len(data["auctions"])
    save_data(data)
    logger.info(f"완료! 총 {data['total_count']}건")

if __name__ == "__main__":
    main()
