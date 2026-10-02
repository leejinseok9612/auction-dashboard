#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""개발용 탐침: 법원경매 사이트의 실제 응답 구조를 조사해 scraper/dev_out/ 에 저장 (검증 끝나면 삭제 예정)"""
import json, os, sys, time, traceback
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scrape_auctions as SA

OUT = Path(__file__).resolve().parent / "dev_out"
OUT.mkdir(exist_ok=True)
R = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "steps": {}}

CANDIDATES = ["", "B000210", "B000211", "B000212", "B000213", "B000214", "B000215", "B000216",
              "B214807", "B214804", "B000240", "B000241", "B000250", "B000251", "B000252",
              "B000253", "B000254", "B250826", "B000242", "B000255"]

JS_FETCH = """async (arg) => {
    var req = window.__auction_last_req;
    if (!req || !req.body) return {ok:false, reason:'no_req'};
    var b = JSON.parse(req.body);
    var si = b.dma_srchGdsDtlSrchInfo || {};
    for (var k in (arg.si||{})) si[k] = arg.si[k];
    var pi = b.dma_pageInfo || {};
    for (var k in (arg.pi||{})) pi[k] = arg.pi[k];
    b.dma_pageInfo = pi;
    try {
        var resp = await fetch(req.url, {method: req.method||'POST', headers: Object.assign({}, req.headers),
                                         body: JSON.stringify(b), credentials:'include'});
        var text = await resp.text();
        var d = JSON.parse(text);
        var rows = (d.data && d.data.dlt_srchResult) || [];
        return {ok:true, status:resp.status, pageInfo:(d.data||{}).dma_pageInfo, n:rows.length,
                rows: arg.full ? rows : rows.slice(0, arg.keep||0), msg: d.message||d.errors||null,
                dataKeys: Object.keys(d.data||{})};
    } catch(e) { return {ok:false, reason:String(e)}; }
}"""

CLEAR_SIDO = {k: "" for k in ["rprsAdongSdCd", "rdnmSdCd", "rprsAdongSggCd", "rprsAdongEmdCd", "rdnmSggCd", "rdnmNo",
                              "mvprpDspslPlcAdongSdCd", "mvprpDspslPlcAdongSggCd", "mvprpDspslPlcAdongEmdCd",
                              "rdDspslPlcAdongSdCd", "rdDspslPlcAdongSggCd", "rdDspslPlcAdongEmdCd"]}


def net_check():
    import requests
    out = {}
    for name, url in [("data.go.kr", "https://apis.data.go.kr/1613000/BldRgstHubService/getBrTitleInfo"),
                      ("juso", "https://business.juso.go.kr/addrlink/addrLinkApi.do"),
                      ("vworld", "https://api.vworld.kr/ned/data/getApartHousingPriceAttr"),
                      ("court", "https://www.courtauction.go.kr/pgj/index.on")]:
        t = time.time()
        try:
            r = requests.get(url, timeout=(8, 15))
            out[name] = f"HTTP {r.status_code} {time.time()-t:.1f}s {r.text[:80]!r}"
        except Exception as e:
            out[name] = f"FAIL {type(e).__name__} {time.time()-t:.1f}s"
    try:
        out["runner_ip"] = requests.get("https://api.ipify.org", timeout=8).text
    except Exception as e:
        out["runner_ip"] = f"FAIL {type(e).__name__}"
    return out


def main():
    R["steps"]["net"] = net_check()
    print("NET", json.dumps(R["steps"]["net"], ensure_ascii=False))
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = browser.new_context(viewport={"width": 1920, "height": 1080}, locale="ko-KR",
                                  user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                                              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"))
        ctx.add_init_script(SA.INIT_SCRIPT)
        page = ctx.new_page()
        xhrs = []

        def on_resp(resp):
            try:
                if resp.request.resource_type in ("xhr", "fetch") and "courtauction" in resp.url:
                    body = resp.text()
                    rec = {"url": resp.url.split("courtauction.go.kr")[-1], "len": len(body),
                           "post": (resp.request.post_data or "")[:300]}
                    if "지방법원" in body and "B000" in body:
                        rec["court_body"] = body[:6000]
                    xhrs.append(rec)
            except Exception:
                pass
        page.on("response", on_resp)

        page.goto(SA.BASE + "/pgj/index.on", wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(8000)
        page.click("#mf_wfm_header_anc_auctnGdsMain")
        page.wait_for_timeout(15000)
        # 검색 폼의 입력 요소 목록 (날짜·법원 선택 요소 파악용)
        R["steps"]["form_elems"] = page.evaluate("""() => Array.from(document.querySelectorAll('[id^="mf_wfm_mainFrame"]'))
            .filter(e => /sbx|cal|ibx|rad|chk|btn/.test(e.id) && e.id.length < 70)
            .map(e => ({id:e.id, tag:e.tagName, val:(e.value||'').slice(0,30), txt:(e.innerText||'').slice(0,40).replace(/\\n/g,' ')})).slice(0,150)""")
        page.evaluate("() => { window.__auction_captured = []; window.__auction_last_req = null; }")
        SA.try_trigger_search(page, page)
        page.wait_for_timeout(6000)
        last_req = page.evaluate("() => window.__auction_last_req")
        R["steps"]["request"] = {"url": last_req and last_req["url"], "headers": last_req and last_req["headers"],
                                 "body": last_req and json.loads(last_req["body"])}
        print("REQ BODY", json.dumps(R["steps"]["request"]["body"], ensure_ascii=False))
        R["steps"]["xhrs"] = xhrs[:80]

        def fetch(si=None, pi=None, keep=0, full=False):
            r = page.evaluate(JS_FETCH, {"si": si or {}, "pi": pi or {}, "keep": keep, "full": full})
            page.wait_for_timeout(700)
            return r

        # C. 법원 코드 후보 확인
        codes = {}
        for c in CANDIDATES:
            r = fetch({**CLEAR_SIDO, "cortOfcCd": c}, {"pageNo": 1, "bfPageNo": "0", "totalCnt": ""}, keep=3)
            names = sorted({x.get("jiwonNm", "") for x in r.get("rows", [])}) if r.get("ok") else []
            codes[c] = {"ok": r.get("ok"), "total": (r.get("pageInfo") or {}).get("totalCnt"), "n": r.get("n"),
                        "names": names, "reason": r.get("reason")}
            print("CODE", c, codes[c])
        R["steps"]["codes"] = codes

        # D. 페이지 크기 시험
        ps = {}
        for size in (10, 40, 100, 500):
            r = fetch({**CLEAR_SIDO, "cortOfcCd": "B000210"}, {"pageNo": 1, "bfPageNo": "0", "totalCnt": "", "pageSize": size}, keep=0)
            ps[size] = {"n": r.get("n"), "pageInfo": r.get("pageInfo")}
            print("PAGESIZE", size, ps[size])
        R["steps"]["pagesize"] = ps

        # E. 원본 행 덤프 (서울중앙 1~3페이지 전체 필드)
        raw = []
        for pg in (1, 2, 3):
            r = fetch({**CLEAR_SIDO, "cortOfcCd": "B000210"}, {"pageNo": pg, "bfPageNo": str(pg - 1), "totalCnt": ""}, full=True)
            raw.extend(r.get("rows", []))
        json.dump(raw, open(OUT / "raw_rows.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        R["steps"]["raw_count"] = len(raw)

        # F. 날짜 범위 시험: 8자리 날짜처럼 보이는 검색 조건을 찾아 종료일을 +90일로 늘려 봄
        si = (R["steps"]["request"]["body"] or {}).get("dma_srchGdsDtlSrchInfo", {})
        date_keys = {k: v for k, v in si.items() if isinstance(v, str) and len(v) == 8 and v.isdigit() and v[:2] == "20"}
        R["steps"]["date_keys"] = date_keys
        print("DATE KEYS", date_keys)
        today = date.today()
        far = (today + timedelta(days=90)).strftime("%Y%m%d")
        dr = {}
        if date_keys:
            end_keys = [k for k, v in date_keys.items() if v > today.strftime("%Y%m%d")]
            for label, override in [("base", {}), ("end+90", {k: far for k in end_keys})]:
                r = fetch({**CLEAR_SIDO, "cortOfcCd": "B000210", **override}, {"pageNo": 1, "bfPageNo": "0", "totalCnt": ""}, keep=0)
                dr[label] = {"override": override, "total": (r.get("pageInfo") or {}).get("totalCnt"), "n": r.get("n")}
                print("DATERANGE", label, dr[label])
        R["steps"]["daterange"] = dr
        browser.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        R["error"] = traceback.format_exc()
        print(R["error"])
    R["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    json.dump(R, open(OUT / "probe.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
