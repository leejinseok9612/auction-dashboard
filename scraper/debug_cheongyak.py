"""
청약HOME 실제 API 엔드포인트 탐색 디버그 스크립트
실행: python3 scraper/debug_cheongyak.py
"""
from playwright.sync_api import sync_playwright
import json, time

def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)  # 눈으로 확인 가능하게 헤드풀
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124",
        )
        page = ctx.new_page()

        print("=== 청약HOME 네트워크 요청 캡처 ===\n")

        captured = []

        def on_request(req):
            if "applyhome" in req.url and req.method == "POST":
                print(f"[POST] {req.url}")
                try:
                    body = req.post_data
                    if body:
                        print(f"  body: {body[:200]}")
                except: pass

        def on_response(resp):
            url = resp.url
            if "applyhome" in url and resp.status == 200:
                ct = resp.headers.get("content-type","")
                if "json" in ct or ".do" in url:
                    try:
                        body = resp.text()
                        if len(body) > 50:
                            print(f"[응답 {resp.status}] {url}")
                            print(f"  길이: {len(body)}  앞100자: {body[:100]}")
                            captured.append({"url": url, "body": body[:2000]})
                    except: pass

        page.on("request",  on_request)
        page.on("response", on_response)

        print("→ 청약HOME 접속 중...")
        page.goto("https://www.applyhome.co.kr/ai/aia/selectAPTLttotPblancListView.do",
                  timeout=30000)
        print("→ 페이지 로드 완료. 5초 대기...")
        page.wait_for_timeout(5000)

        print("\n→ 조회 버튼 탐색 중...")
        # 가능한 버튼들 출력
        btns = page.evaluate("""() => {
            const els = document.querySelectorAll('button, input[type=button], input[type=submit], a');
            return Array.from(els).map(e => ({
                tag: e.tagName, text: e.innerText||e.value||'', id: e.id, cls: e.className
            })).filter(e => e.text.trim()).slice(0, 30);
        }""")
        print("버튼 목록:")
        for b in btns:
            print(f"  [{b['tag']}] text='{b['text'][:30]}' id='{b['id']}' class='{b['cls'][:30]}'")

        # 조회 버튼 클릭 시도
        for sel in ["button:has-text('조회')", "input[value='조회']", "#btnSearch", "button.btn_search",
                    "a:has-text('조회')", "button:has-text('검색')"]:
            try:
                el = page.query_selector(sel)
                if el:
                    print(f"\n→ 클릭: {sel}")
                    el.click()
                    page.wait_for_timeout(3000)
                    break
            except: pass

        print("\n=== 캡처된 응답 ===")
        for c in captured:
            print(f"\nURL: {c['url']}")
            try:
                d = json.loads(c['body'])
                print(f"JSON 키: {list(d.keys())}")
                # 리스트 찾기
                for k, v in d.items():
                    if isinstance(v, list) and v:
                        print(f"  '{k}' 리스트: {len(v)}건")
                        if isinstance(v[0], dict):
                            print(f"    첫번째 항목 키: {list(v[0].keys())[:10]}")
            except:
                print(f"  (JSON 아님) {c['body'][:150]}")

        print("\n→ 10초 후 종료 (화면 확인하세요)")
        time.sleep(10)
        browser.close()

if __name__ == "__main__":
    run()
