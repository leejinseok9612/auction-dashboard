#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""개발용 탐침 4: 건축물대장 API 응답 원본 확인 (검증 끝나면 삭제 예정). 인증키는 출력하지 않는다."""
import json, os, sys, time
from pathlib import Path
import requests
sys.path.insert(0, str(Path(__file__).resolve().parent))
import main as M

OUT = Path(__file__).resolve().parent / "dev_out"; OUT.mkdir(exist_ok=True)
KEY = M.DATA_GO_KR_KEY
BASE = "https://apis.data.go.kr/1613000/BldRgstHubService/"
OPS = ["getBrTitleInfo", "getBrRecapTitleInfo", "getBrBasisOulnInfo", "getBrExposInfo", "getBrExposPubuseAreaInfo", "getBrHsprcInfo", "getBrFlrOulnInfo"]
CASES = [  # (설명, PNU, 호)
    ("OK 관악 다세대", "1162010300110860012", "301"),
    ("FAIL 금천 오피스텔 골드마운틴", "1154010200110440003", "801"),
    ("FAIL 금천 다세대 금강파크빌", "1154010300107970017", "204"),
    ("FAIL 은평 연송에버빌", "1138010700100850017", "501"),
    ("FAIL 금천 아파트 가산양우내안애", "1154010100101410002", "1710"),
    ("FAIL 강서 마곡그린필", "1150010900103230003", "502"),
]
R = []
for label, pnu, ho in CASES:
    pp = M.pnu_parts(pnu)
    rec = {"case": label, "pnu": pnu, "params": pp, "ops": {}}
    for op in OPS:
        variants = {"기본": dict(pp)}
        if op in ("getBrExposPubuseAreaInfo", "getBrExposInfo", "getBrHsprcInfo"):
            variants["hoNm"] = {**pp, "hoNm": f"{ho}호"}
            variants["hoNm숫자"] = {**pp, "hoNm": ho}
        if op == "getBrTitleInfo":
            variants["ji없이"] = {k: v for k, v in pp.items() if k != "ji"}
            variants["platGb없이"] = {k: v for k, v in pp.items() if k != "platGbCd"}
        for vn, params in variants.items():
            t = time.time()
            try:
                r = requests.get(BASE + op, params={"serviceKey": KEY, "_type": "json", "numOfRows": 5, "pageNo": 1, **params}, timeout=(8, 25))
                txt = r.text
                try:
                    body = json.loads(txt)["response"]
                    items = (body.get("body") or {}).get("items") or {}
                    rows = items.get("item", []) if isinstance(items, dict) else []
                    rows = rows if isinstance(rows, list) else [rows]
                    rec["ops"][f"{op}/{vn}"] = {"http": r.status_code, "header": body.get("header"),
                                                "total": (body.get("body") or {}).get("totalCount"), "rows": rows[:2]}
                except Exception:
                    rec["ops"][f"{op}/{vn}"] = {"http": r.status_code, "raw": txt[:300]}
            except Exception as e:
                rec["ops"][f"{op}/{vn}"] = {"error": type(e).__name__, "sec": round(time.time() - t, 1)}
            time.sleep(0.15)
    R.append(rec)
    print(label, {k: (v.get("total"), (v.get("header") or {}).get("resultCode"), v.get("error"), v.get("http")) for k, v in rec["ops"].items()})
json.dump(R, open(OUT / "probe4.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
