#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""개발용 탐침 2: 코드표·날짜범위·상세/명세서 API 조사 (검증 끝나면 삭제 예정)"""
import json, re, sys, time, traceback
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import scrape_auctions as SA
from dev_probe import JS_FETCH, CLEAR_SIDO

OUT = Path(__file__).resolve().parent / "dev_out"
OUT.mkdir(exist_ok=True)
R = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "steps": {}}


def main():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        ctx = browser.new_context(viewport={"width": 1920, "height": 1080}, locale="ko-KR",
                                  user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                                              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"))
        ctx.add_init_script(SA.INIT_SCRIPT)
        page = ctx.new_page()
        xhrs = []
        phase = {"name": "load"}
        seq = {"n": 0}

        def on_resp(resp):
            try:
                u = resp.url
                if "courtauction" not in u or resp.request.resource_type not in ("xhr", "fetch"):
                    return
                if "/websquare/" in u or u.split("?")[0].endswith(".js"):
                    return
                body = resp.text()
                short = u.split("courtauction.go.kr")[-1].split("?")[0]
                rec = {"phase": phase["name"], "url": short, "len": len(body), "post": (resp.request.post_data or "")[:600]}
                save = (short.endswith(".on") and phase["name"] != "load" and "searchControllerMain" not in short) \
                    or "sccd/list" in short or "selectLclLst" in short or "selectMclLst" in short or "selectSclLst" in short
                if short.endswith(".xml") and phase["name"] != "load":
                    save = True
                if save:
                    seq["n"] += 1
                    fn = f"x{seq['n']:03d}_{phase['name']}_{short.strip('/').replace('/', '_')}"[:110]
                    open(OUT / fn, "w", encoding="utf-8").write(body[:400000])
                    rec["saved"] = fn
                xhrs.append(rec)
            except Exception:
                pass
        ctx.on("response", on_resp)
        popups = []
        ctx.on("page", lambda pg: popups.append(pg))

        page.goto(SA.BASE + "/pgj/index.on", wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(10000)
        page.click("#mf_wfm_header_anc_auctnGdsMain")
        page.wait_for_timeout(20000)
        page.wait_for_selector("input[value='검색']", timeout=30000)

        # 용도 분류 코드표: 대분류 '건물' → 중분류 목록, 중분류 첫 항목 → 소분류 목록
        phase["name"] = "usg"
        try:
            page.select_option("#mf_wfm_mainFrame_sbx_rletLclLst", label="건물")
            page.wait_for_timeout(2500)
            mcl = page.locator("#mf_wfm_mainFrame_sbx_rletMclLst option").all_inner_texts()
            R["steps"]["mcl_options"] = mcl
            for lab in mcl[1:6]:
                page.select_option("#mf_wfm_mainFrame_sbx_rletMclLst", label=lab)
                page.wait_for_timeout(1500)
                R["steps"].setdefault("scl_options", {})[lab] = page.locator("#mf_wfm_mainFrame_sbx_rletSclLst option").all_inner_texts()
            page.click("#mf_wfm_mainFrame_btn_rletInit")
            page.wait_for_timeout(2500)
        except Exception as e:
            R["steps"]["usg_error"] = str(e)[:300]

        phase["name"] = "search"
        page.evaluate("() => { window.__auction_captured = []; window.__auction_last_req = null; }")
        page.click("#mf_wfm_mainFrame_btn_gdsDtlSrch")
        page.wait_for_timeout(8000)
        last_req = page.evaluate("() => window.__auction_last_req")
        R["steps"]["has_req"] = bool(last_req)

        def fetch(si=None, pi=None, keep=0, full=False):
            r = page.evaluate(JS_FETCH, {"si": si or {}, "pi": pi or {}, "keep": keep, "full": full})
            page.wait_for_timeout(700)
            return r

        def total(si):
            r = fetch({**CLEAR_SIDO, **si}, {"pageNo": 1, "bfPageNo": "0", "totalCnt": "", "pageSize": 10}, keep=0)
            pi = r.get("pageInfo") or {}
            return {"total": pi.get("totalCnt"), "group": pi.get("groupTotalCount"), "ok": r.get("ok"), "reason": r.get("reason")}

        today = date.today()
        d = lambda n: (today + timedelta(days=n)).strftime("%Y%m%d")
        tests = {}
        R['steps']['tests'] = tests

        # 상세 화면 열기: 결과 그리드에서 첫 사건번호 클릭 → 이후 호출되는 API 기록
        phase["name"] = "detail"
        try:
            first = page.evaluate("() => (window.__auction_captured[0]||{}).data.dlt_srchResult[0]")
            R["steps"]["detail_target"] = {k: first.get(k) for k in ("srnSaNo", "maemulSer", "docid", "printSt")}
            # 주거용 + 특수조건 있는 물건을 찾아 그 행으로 상세 이동 (명세서 내용이 풍부한 표본)
            r = fetch({**CLEAR_SIDO, "cortOfcCd": "B000210", "bidEndYmd": d(60), "lclDspslGdsLstUsgCd": "20000", "mclDspslGdsLstUsgCd": "20100"},
                      {"pageNo": 1, "bfPageNo": "0", "totalCnt": "", "pageSize": 40}, full=True)
            R["steps"]["res_rows"] = [{k: x.get(k) for k in ("srnSaNo", "maemulSer", "spJogCd", "mulBigo", "dspslUsgNm", "yuchalCnt")} for x in r.get("rows", [])]
            page.wait_for_timeout(1000)
            page.evaluate("() => moveDtlPage(0)")
            page.wait_for_timeout(15000)
            R["steps"]["detail_url"] = page.url
            R["steps"]["detail_text"] = page.locator("body").inner_text()[:12000]
            # 상세 화면의 버튼·탭 목록
            R["steps"]["detail_clickables"] = page.evaluate("""() => Array.from(document.querySelectorAll('a,button,input[type=button]'))
                .map(e => ({id:e.id, t:((e.innerText||e.value||'')+'').trim().slice(0,30)}))
                .filter(x => x.t).slice(0,150)""")
            for label in ["매각물건명세서", "현황조사서", "감정평가서", "사건상세조회", "기일내역", "문건/송달내역"]:
                phase["name"] = "btn_" + label.replace("/", "")
                try:
                    n0 = len(popups)
                    page.locator(f"a:has-text('{label}'), button:has-text('{label}'), input[value='{label}']").first.click(timeout=6000)
                    page.wait_for_timeout(7000)
                    info = {"clicked": True, "url": page.url, "popups": len(popups) - n0}
                    if len(popups) > n0:
                        pop = popups[-1]
                        try:
                            pop.wait_for_load_state("domcontentloaded", timeout=15000)
                            pop.wait_for_timeout(4000)
                            info["popup_url"] = pop.url
                            info["popup_text"] = pop.locator("body").inner_text()[:3000]
                        except Exception as e:
                            info["popup_err"] = str(e)[:200]
                    else:
                        info["text"] = page.locator("body").inner_text()[:2500]
                    R["steps"].setdefault("buttons", {})[label] = info
                except Exception as e:
                    R["steps"].setdefault("buttons", {})[label] = {"clicked": False, "err": str(e)[:200]}
        except Exception:
            R["steps"]["detail_error"] = traceback.format_exc()[-1500:]
        R["steps"]["xhrs"] = [x for x in xhrs if x["phase"] != "load"][:300]
        browser.close()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        R["error"] = traceback.format_exc()
        print(R["error"])
    R["finished"] = time.strftime("%Y-%m-%d %H:%M:%S")
    json.dump(R, open(OUT / "probe2.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
