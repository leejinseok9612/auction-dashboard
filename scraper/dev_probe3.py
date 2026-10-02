#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""개발용 탐침 3: 물건 상세·현황조사서 API 응답(사진 제외) 저장 (검증 끝나면 삭제 예정)"""
import json, sys, time, traceback
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import collect_court as C

OUT = Path(__file__).resolve().parent / "dev_out"; OUT.mkdir(exist_ok=True)
R = {"targets": [], "errors": []}

JS_POST = """async (arg) => {
    try {
        var resp = await fetch(arg.url, {method:'POST', headers: arg.headers, body: JSON.stringify(arg.body), credentials:'include'});
        var text = await resp.text();
        var d = JSON.parse(text);
        var size = text.length, pics = 0;
        // 사진(base64) 제거
        (function strip(o){ if (o && typeof o === 'object') { for (var k in o) {
            if (k === 'picFile' && typeof o[k] === 'string') { pics++; o[k] = '<' + o[k].length + ' bytes>'; }
            else strip(o[k]); } } })(d);
        return {ok:true, status:resp.status, size:size, pics:pics, body:d};
    } catch(e) { return {ok:false, reason:String(e)}; }
}"""
H = dict(C.REQUEST_HEADERS)


def main():
    all_items = json.load(open(C.OUT, encoding="utf-8")).get("auctions", [])
    today = time.strftime("%Y-%m-%d")
    def pick(pred, n=1):
        return [i for i in all_items if pred(i)][:n]
    targets = []
    targets += pick(lambda i: i.get("remarks") and "대항력" in i["remarks"], 2)
    targets += pick(lambda i: "유치권" in (i.get("special_conditions") or []), 1)
    targets += pick(lambda i: not i.get("special_conditions") and not i.get("remarks") and i["property_type"] in ("다세대", "아파트") and i["auction_date"] <= time.strftime("%Y-%m-%d", time.localtime(time.time() + 6 * 86400)), 3)
    targets += pick(lambda i: not i.get("special_conditions") and i["auction_date"] >= time.strftime("%Y-%m-%d", time.localtime(time.time() + 20 * 86400)), 2)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser, page = C.open_search_page(p)
        si = dict(C.REQUEST_TEMPLATE["dma_srchGdsDtlSrchInfo"])
        for n, it in enumerate(targets):
            rec = {"id": it["id"], "case_no": it["case_no"], "court": it["court"], "date": it["auction_date"],
                   "type": it["property_type"], "special": it.get("special_conditions"), "remarks": it.get("remarks")}
            t = time.time()
            d = page.evaluate(JS_POST, {"url": "/pgj/pgj15B/selectAuctnCsSrchRslt.on",
                "headers": {**H, "submissionid": "mf_wfm_mainFrame_sbm_selectGdsDtlSrchDtlInfo"},
                "body": {"dma_srchGdsDtlSrch": {"csNo": it["case_no"], "cortOfcCd": it["court_code"],
                                                "dspslGdsSeq": str(it["item_no"]), "pgmId": "PGJ151F01", "srchInfo": si}}})
            rec["detail"] = {k: d.get(k) for k in ("ok", "status", "size", "pics", "reason")}
            rec["detail_sec"] = round(time.time() - t, 1)
            if d.get("ok"):
                json.dump(d["body"], open(OUT / f"detail_{n}.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            page.wait_for_timeout(1500)
            c = page.evaluate(JS_POST, {"url": "/pgj/pgj15B/selectCurstExmndc.on",
                "headers": {**H, "submissionid": "mf_wfm_mainFrame_sbm_selectCurstExmndc"},
                "body": {"dma_srchCurstExmn": {"cortOfcCd": it["court_code"], "csNo": it["case_no"],
                                               "auctnInfOriginDvsCd": "2", "ordTsCnt": ""}}})
            rec["curst"] = {k: c.get(k) for k in ("ok", "status", "size", "pics", "reason")}
            if c.get("ok"):
                json.dump(c["body"], open(OUT / f"curst_{n}.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
            page.wait_for_timeout(1500)
            R["targets"].append(rec)
            print("TARGET", json.dumps(rec, ensure_ascii=False))
        browser.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        R["errors"].append(traceback.format_exc()); print(R["errors"][-1])
    json.dump(R, open(OUT / "probe3.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
