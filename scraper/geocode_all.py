"""
경매 + 청약 전체 주소 지오코딩 스크립트
Nominatim (OpenStreetMap) 무료 API 사용
실행: python3 scraper/geocode_all.py
"""
import json, time, os, urllib.request, urllib.parse
from pathlib import Path

REPO = Path(__file__).parent.parent
AUCTION_PATH  = REPO / "docs/data/auctions.json"
CHEONGYAK_PATH = REPO / "docs/data/cheongyak.json"

_cache = {}

def geocode(address):
    """주소 → (lat, lng). Nominatim 1.2초 간격 준수."""
    if not address:
        return None, None
    address = address.split('(')[0].strip()   # 괄호 이후 제거
    key = address[:50]
    if key in _cache:
        return _cache[key]

    time.sleep(1.2)
    try:
        params = urllib.parse.urlencode({
            'q': address + ', 대한민국',
            'format': 'json',
            'limit': '1',
            'countrycodes': 'kr',
            'accept-language': 'ko',
        })
        url = 'https://nominatim.openstreetmap.org/search?' + params
        req = urllib.request.Request(url, headers={
            'User-Agent': 'auction-dashboard-geocoder/1.0 (personal project)'
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            results = json.loads(resp.read().decode())
            if results:
                lat = round(float(results[0]['lat']), 6)
                lng = round(float(results[0]['lon']), 6)
                _cache[key] = (lat, lng)
                return lat, lng
    except Exception as e:
        print(f"    오류: {e}")
    _cache[key] = (None, None)
    return None, None

def process_auctions():
    print("\n══ 경매 지오코딩 ══")
    with open(AUCTION_PATH, 'r', encoding='utf-8') as f:
        data = json.load(f)

    items = data.get('auctions', [])
    ok = skip = fail = 0

    for i, item in enumerate(items):
        if item.get('lat'):
            _cache[item['address'][:50]] = (item['lat'], item['lng'])
            skip += 1
            continue
        addr = item.get('address', '')
        lat, lng = geocode(addr)
        item['lat'] = lat
        item['lng'] = lng
        sym = '✓' if lat else '✗'
        print(f"  [{i+1}/{len(items)}] {sym} {addr[:35]} → ({lat}, {lng})")
        if lat: ok += 1
        else: fail += 1

    with open(AUCTION_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  → 완료: 성공 {ok}건 / 스킵 {skip}건 / 실패 {fail}건")

def process_cheongyak():
    print("\n══ 청약 지오코딩 ══")
    with open(CHEONGYAK_PATH, 'r', encoding='utf-8') as f:
        data = json.load(f)

    items = data.get('subscriptions', [])
    ok = skip = fail = 0

    for i, item in enumerate(items):
        if item.get('lat'):
            _cache[item.get('address','')[:50]] = (item['lat'], item['lng'])
            skip += 1
            continue
        addr = item.get('address', '')
        lat, lng = geocode(addr)
        item['lat'] = lat
        item['lng'] = lng
        sym = '✓' if lat else '✗'
        print(f"  [{i+1}/{len(items)}] {sym} {item.get('name','')[:20]} → ({lat}, {lng})")
        if lat: ok += 1
        else: fail += 1

    with open(CHEONGYAK_PATH, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  → 완료: 성공 {ok}건 / 스킵 {skip}건 / 실패 {fail}건")

if __name__ == '__main__':
    print(f"▶ 지오코딩 시작 (약 {(24+20)*1.2/60:.0f}~2분 소요)")
    process_auctions()
    process_cheongyak()
    print("\n✅ 완료! 이제 git push 해주세요:")
    print("  cd /Users/jinseok/Desktop/auction-dashboard")
    print("  git add docs/data/auctions.json docs/data/cheongyak.json")
    print('  git commit -m "data: 지오코딩 완료"')
    print("  git push")
