"""
청약HOME 실제 API 엔드포인트 탐색 v2
GET/POST/fetch 모두 캡처
"""
from playwright.sync_api import sync_playwright
import json, time

def run():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124",
        )
        page = ctx.new_page()

        print("=== 청약HOME 네트워크 전체 캡처 ===\n")

        api_hits = []

        def on_request(req):
            url = req.url
            # analytics, fonts, css, image 제외
            if any(x in url for x in ["google","gstatic","font","cdn","jpg","png","css","woff"]):
                return
            if "applyhome" in url or "reb.or.kr" in url or "odcloud" in url:
                print(f"[{req.method}] {url}")

        def on_response(resp):
            url = resp.url
            if any(x in url for x in ["google","gstatic","font","cdn","jpg","png","css","woff"]):
                return
            if "applyhome" in url and resp.status == 200:
                try:
                    body = resp.text()
                    ct = resp.headers.get("content-type","")
                    size = len(body)
                    # JSON이거나 데이터가 있을 법한 응답
                    if "json" in ct or (size > 200 and size < 500000 and not body.strip().startswith("<")):
                        print(f"  ★ JSON응답 [{size}bytes] {url[-80:]}")
                        api_hits.append({"url": url, "body": body[:3000], "ct": ct})
                    elif size > 500:
                        print(f"  ▷ [{size}bytes] {url[-80:]}")
                except: pass

        page.on("request",  on_request)
        page.on("response", on_response)

        # XHR도 캡처하기 위해 init script 추가
        ctx.add_init_script("""
        window.__reqs = [];
        const origFetch = window.fetch;
        window.fetch = function(url, opts) {
            window.__reqs.push({url: String(url), method: (opts||{}).method||'GET', body: String((opts||{}).body||'')});
            return origFetch.apply(this, arguments).then(r => {
                const clone = r.clone();
                clone.text().then(t => {
                    if (t.length > 100 && !t.trim().startsWith('<')) {
                        window.__reqs.push({url: String(url), resp: t.substring(0, 1000)});
                    }
                }).catch(()=>{});
                return r;
            });
        };
        """)

        print("→ 청약HOME 접속 중...")
        page.goto("https://www.applyhome.co.kr/ai/aia/selectAPTLttotPblancListView.do",
                  wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(5000)

        # fetch로 캡처된 요청 확인
        reqs = page.evaluate("window.__reqs || []")
        if reqs:
            print("\n=== fetch() 요청 ===")
            for r in reqs:
                print(f"  {r}")

        # 페이지 소스에서 API URL 패턴 찾기
        src = page.content()
        import re
        # .do 패턴 찾기
        urls_in_src = set(re.findall(r'["\']([^"\']*\.do[^"\']*)["\']', src))
        print("\n=== 소스에서 발견된 .do URL ===")
        for u in sorted(urls_in_src)[:30]:
            if "select" in u.lower() or "list" in u.lower() or "search" in u.lower():
                print(f"  {u}")

        # 검색 버튼 클릭 후 다시 캡처
        print("\n→ 검색 버튼 클릭 시도...")
        for sel in ["button:has-text('검색')", "button:has-text('조회')", "input[value='검색']",
                    "#btnSearch", ".btn_search", "a.btn:has-text('검색')"]:
            try:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    print(f"  클릭: {sel}")
                    el.click()
                    page.wait_for_timeout(3000)
                    break
            except: pass

        # 다시 fetch 확인
        reqs2 = page.evaluate("window.__reqs || []")
        if len(reqs2) > len(reqs):
            print("\n=== 클릭 후 추가된 fetch 요청 ===")
            for r in reqs2[len(reqs):]:
                print(f"  {r}")

        print("\n=== 캡처된 JSON 응답 ===")
        if api_hits:
            for h in api_hits:
                print(f"\nURL: {h['url']}")
                try:
                    d = json.loads(h['body'])
                    print(f"JSON 키: {list(d.keys())}")
                    for k, v in d.items():
                        if isinstance(v, list) and v:
                            print(f"  리스트 '{k}': {len(v)}건")
                            if isinstance(v[0], dict):
                                print(f"    키: {list(v[0].keys())[:8]}")
                except:
                    print(f"  {h['body'][:200]}")
        else:
            print("(캡처된 JSON 없음)")

        browser.close()

if __name__ == "__main__":
    run()
