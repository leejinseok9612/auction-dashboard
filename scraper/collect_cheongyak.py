#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
청약 분양정보 수집기 — 한국부동산원 청약홈 분양정보 조회 서비스 (공공데이터포털 odcloud API)

  출처   https://api.odcloud.kr/api/ApplyhomeInfoDetailSvc/v1  (인증키: 환경변수 CHEONGYAK_API_KEY)
  대상   APT / 오피스텔·도시형·민간임대 / APT 무순위·잔여세대 / 공공지원 민간임대 / 임의공급
  출력   docs/data/cheongyak.json  → {"updated", "updated_at", "report", "subscriptions": [...]}

  · 청약 접수가 진행 중이거나 예정이거나, 당첨자 발표를 기다리는 공고만 남긴다
  · 주택형별 분양가(최고가 기준)·공급세대수, 경쟁률(조회 권한이 있을 때)도 함께 수집
  · 수집에 실패하면 기존 파일을 그대로 둔다 (빈 파일로 덮어쓰지 않음)
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
ROOT = Path(__file__).resolve().parent.parent
OUT_PATH = ROOT / "docs" / "data" / "cheongyak.json"
API_KEY = (os.environ.get("CHEONGYAK_API_KEY") or "").strip()
BASE = "https://api.odcloud.kr/api/ApplyhomeInfoDetailSvc/v1"
CMPET_BASE = "https://api.odcloud.kr/api/ApplyhomeInfoCmpetRtSvc/v1"
LOOKBACK_DAYS = 150          # 모집공고일 기준 이만큼 전까지 조회 (발표 대기 공고 포함)
PER_PAGE = 500
MAX_PAGES = 20
APPLYHOME = "https://www.applyhome.co.kr"

# (종류, 공고 조회, 주택형 조회, 경쟁률 조회, 기본 유형명)
KINDS = [
    ("apt",    "getAPTLttotPblancDetail",        "getAPTLttotPblancMdl",        "getAPTLttotPblancCmpet",        "APT"),
    ("offi",   "getUrbtyOfctlLttotPblancDetail", "getUrbtyOfctlLttotPblancMdl", "getUrbtyOfctlLttotPblancCmpet", "오피스텔"),
    ("remndr", "getRemndrLttotPblancDetail",     "getRemndrLttotPblancMdl",     "getRemndrLttotPblancCmpet",     "APT 무순위·잔여세대"),
    ("rent",   "getPblPvtRentLttotPblancDetail", "getPblPvtRentLttotPblancMdl", "getPblPvtRentLttotPblancCmpet", "공공지원 민간임대"),
    ("opt",    "getOPTLttotPblancDetail",        "getOPTLttotPblancMdl",        None,                            "APT 임의공급"),
]

SIDO = {"서울": "서울특별시", "경기": "경기도", "인천": "인천광역시", "부산": "부산광역시", "대구": "대구광역시",
        "광주": "광주광역시", "대전": "대전광역시", "울산": "울산광역시", "세종": "세종특별자치시",
        "강원": "강원특별자치도", "충북": "충청북도", "충남": "충청남도", "전북": "전북특별자치도",
        "전남": "전라남도", "경북": "경상북도", "경남": "경상남도", "제주": "제주특별자치도"}


def log(msg: str) -> None:
    print(f"[{datetime.now(KST):%H:%M:%S}] {msg}", flush=True)


def scrub(s) -> str:
    s = str(s)
    for k in {API_KEY, urllib.parse.quote(API_KEY, safe="")}:
        if k:
            s = s.replace(k, "***")
    return s


class ApiDenied(Exception):
    """이 오퍼레이션을 쓸 권한이 없음 (활용신청 안 됨 등) — 재시도해도 소용없음"""


def call(base: str, op: str, params: dict) -> dict:
    q = {"page": 1, "perPage": PER_PAGE, "returnType": "JSON", **params, "serviceKey": API_KEY}
    url = f"{base}/{op}?{urllib.parse.urlencode(q)}"
    last = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Mozilla/5.0 (auction-dashboard)"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:200]
            if e.code in (401, 403, 404):
                raise ApiDenied(f"HTTP {e.code}: {scrub(body)}")
            last = f"HTTP {e.code}: {scrub(body)}"
        except Exception as e:            # 통신 오류 — 예외 원문에 주소(인증키)가 섞일 수 있어 종류만 기록
            last = f"통신 오류: {type(e).__name__}"
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(last or "알 수 없는 오류")


def fetch_all(base: str, op: str, cond: dict) -> list:
    rows: list = []
    for page in range(1, MAX_PAGES + 1):
        d = call(base, op, {**cond, "page": page})
        data = d.get("data") or []
        rows.extend(r for r in data if isinstance(r, dict))
        total = int(d.get("matchCount") or d.get("totalCount") or 0)
        if not data or len(data) < PER_PAGE or len(rows) >= total:
            break
        time.sleep(0.2)
    return rows


def g(raw: dict, *keys) -> str:
    for k in keys:
        v = raw.get(k)
        if v is not None and str(v).strip() not in ("", "null", "None"):
            return str(v).strip()
    return ""


def ymd(s) -> str:
    """'20261014' / '2026-10-14' / '2026.10.14' → '2026-10-14' (형식이 다르면 빈 값)"""
    t = re.sub(r"\D", "", str(s or ""))
    if len(t) >= 8:
        try:
            return datetime.strptime(t[:8], "%Y%m%d").strftime("%Y-%m-%d")
        except ValueError:
            return ""
    return ""


def ym(s) -> str:
    t = re.sub(r"\D", "", str(s or ""))
    return f"{t[:4]}-{t[4:6]}" if len(t) >= 6 else ""


def num(s):
    t = re.sub(r"[^\d.]", "", str(s or ""))
    if not t:
        return None
    try:
        v = float(t)
        return int(v) if v == int(v) else v
    except ValueError:
        return None


def status_of(start: str, end: str, win: str, today: str) -> str:
    if not start or not end:
        return "미정"
    if today < start:
        return "청약예정"
    if today <= end:
        return "청약중"
    if win and today <= win:
        return "발표대기"
    return "마감"


def build(raw: dict, kind: str, default_type: str, today: str) -> dict | None:
    name = g(raw, "HOUSE_NM")
    hid = g(raw, "HOUSE_MANAGE_NO", "PBLANC_NO")
    if not name or not hid:
        return None
    # 접수 기간: 종류마다 필드가 다름. 여러 접수일(특별공급·1순위·2순위) 중 가장 이른 날 ~ 가장 늦은 날
    starts = [ymd(g(raw, k)) for k in ("RCEPT_BGNDE", "SUBSCRPT_RCEPT_BGNDE", "SPSPLY_RCEPT_BGNDE", "GNRL_RCEPT_BGNDE",
                                       "GNRL_RNK1_CRSPAREA_RCPTDE")]
    ends = [ymd(g(raw, k)) for k in ("RCEPT_ENDDE", "SUBSCRPT_RCEPT_ENDDE", "SPSPLY_RCEPT_ENDDE", "GNRL_RCEPT_ENDDE",
                                     "GNRL_RNK2_ETC_AREA_ENDDE", "GNRL_RNK2_CRSPAREA_ENDDE", "GNRL_RNK1_ETC_AREA_ENDDE")]
    starts, ends = [s for s in starts if s], [e for e in ends if e]
    start, end = (min(starts) if starts else ""), (max(ends) if ends else "")
    win = ymd(g(raw, "PRZWNER_PRESNATN_DE"))
    st = status_of(start, end, win, today)
    if st in ("마감", "미정"):
        return None

    addr = g(raw, "HSSPLY_ADRES")
    parts = addr.split()
    area_nm = g(raw, "SUBSCRPT_AREA_CODE_NM")
    first = parts[0] if parts else ""
    region = first if first in SIDO.values() else (SIDO.get(first[:2]) or SIDO.get(area_nm) or first or area_nm)
    district = ""
    if len(parts) > 1:
        district = parts[1]
        if len(parts) > 2 and parts[1].endswith("시") and parts[2].endswith("구"):
            district += " " + parts[2]

    secd, dtl, rent = g(raw, "HOUSE_SECD_NM"), g(raw, "HOUSE_DTL_SECD_NM"), g(raw, "RENT_SECD_NM")
    if kind == "apt":
        typ = "APT" + (f" · {dtl}" if dtl else "") + (f" · {rent}" if rent and rent != "분양주택" else "")
    elif kind == "offi":
        typ = dtl or secd or default_type
    else:
        typ = default_type
    pblanc = g(raw, "PBLANC_NO") or hid
    item = {
        "id": hid,
        "pblanc_no": pblanc,
        "kind": kind,
        "name": name,
        "type": typ,
        "builder": g(raw, "BSNS_MBY_NM"),
        "constructor": g(raw, "CNSTRCT_ENTRPS_NM"),
        "region": region,
        "district": district,
        "address": addr,
        "supply_count": num(g(raw, "TOT_SUPLY_HSHLDCO")) or 0,
        "price_min": None,
        "price_max": None,
        "announce_date": ymd(g(raw, "RCRIT_PBLANC_DE")),
        "start_date": start,
        "end_date": end,
        "special_date": ymd(g(raw, "SPSPLY_RCEPT_BGNDE")),
        "rank1_date": ymd(g(raw, "GNRL_RNK1_CRSPAREA_RCPTDE")),
        "rank2_date": ymd(g(raw, "GNRL_RNK2_CRSPAREA_RCPTDE")),
        "win_date": win,
        "contract_start": ymd(g(raw, "CNTRCT_CNCLS_BGNDE")),
        "contract_end": ymd(g(raw, "CNTRCT_CNCLS_ENDDE")),
        "move_in": ym(g(raw, "MVN_PREARNGE_YM")),
        "status": st,
        "url": g(raw, "PBLANC_URL") or APPLYHOME,
        "homepage": g(raw, "HMPG_ADRES"),
        "phone": g(raw, "MDHS_TELNO"),
        "price_cap": g(raw, "PARCPRC_ULS_AT") == "Y",           # 분양가상한제 적용
        "speculation_area": g(raw, "SPECLT_RDN_EARTH_AT") == "Y",  # 투기과열지구
        "public_land": g(raw, "PUBLIC_HOUSE_EARTH_AT") == "Y",   # 공공주택지구
        "units": [],
        "competition": {},
        "lat": None,
        "lng": None,
    }
    return item


def attach_units(items: dict, rows: list) -> None:
    """주택형별 공급세대·분양가(만원) → units, price_min/max"""
    for r in rows:
        it = items.get(g(r, "HOUSE_MANAGE_NO"))
        if not it:
            continue
        price = num(g(r, "LTTOT_TOP_AMOUNT", "SUPLY_AMOUNT"))
        unit = {
            "type": g(r, "HOUSE_TY", "TP").strip(),
            "area": num(g(r, "SUPLY_AR", "EXCLUSE_AR")),
            "supply": num(g(r, "SUPLY_HSHLDCO")) or 0,
            "special": num(g(r, "SPSPLY_HSHLDCO")) or 0,
            "price": int(price) if price else None,
        }
        it["units"].append(unit)
    for it in items.values():
        prices = [u["price"] for u in it["units"] if u["price"]]
        if prices:
            it["price_min"], it["price_max"] = min(prices), max(prices)
        if not it["supply_count"]:
            it["supply_count"] = sum((u["supply"] or 0) + (u["special"] or 0) for u in it["units"])
        it["units"].sort(key=lambda u: (u["area"] or 0, u["type"]))


def attach_competition(items: dict, rows: list) -> None:
    """주택형별 1순위 최고 경쟁률 → competition {'84A 1순위': 12.3}"""
    best: dict = {}
    for r in rows:
        hid = g(r, "HOUSE_MANAGE_NO")
        if hid not in items:
            continue
        rate = g(r, "CMPET_RATE")
        if not re.fullmatch(r"[\d,]+(\.\d+)?", rate):          # '-'·'(△5)'(미달) 등은 제외
            continue
        rank = g(r, "SUBSCRPT_RANK_CODE")
        label = g(r, "HOUSE_TY", "TP").strip()
        label = re.sub(r"^0+", "", label.split(".")[0]) + (re.sub(r"^[\d.]+", "", label) or "")
        key = (hid, f"{label}㎡" + (f" {rank}순위" if rank else ""))
        v = float(rate.replace(",", ""))
        best[key] = max(best.get(key, 0.0), v)
    for (hid, label), v in best.items():
        items[hid]["competition"][label] = round(v, 2)
    for it in items.values():
        it["competition"] = dict(sorted(it["competition"].items(), key=lambda kv: -kv[1])[:6])


def main() -> int:
    log("청약 분양정보 수집 시작")
    if not API_KEY:
        log("  ⚠ CHEONGYAK_API_KEY 없음 — 수집 생략 (기존 파일 유지)")
        return 0
    today = datetime.now(KST).date()
    since = (today - timedelta(days=LOOKBACK_DAYS)).isoformat()
    try:
        prev = json.loads(OUT_PATH.read_text(encoding="utf-8"))
    except Exception:
        prev = {}
    prev_by_id = {s.get("id"): s for s in prev.get("subscriptions", []) if isinstance(s, dict)}

    items: dict = {}
    report = {"since": since, "kinds": {}, "errors": []}
    ok_kinds = 0
    for kind, op_detail, op_mdl, op_cmpet, default_type in KINDS:
        rep = {"rows": 0, "kept": 0}
        try:
            rows = fetch_all(BASE, op_detail, {"cond[RCRIT_PBLANC_DE::GTE]": since})
        except Exception as e:
            report["errors"].append(f"{kind} 공고: {scrub(e)}")
            log(f"  ⚠ [{kind}] 공고 조회 실패: {scrub(e)}")
            report["kinds"][kind] = rep
            continue
        ok_kinds += 1
        rep["rows"] = len(rows)
        if rows and not report.get("sample_keys"):
            report["sample_keys"] = sorted(rows[0].keys())
        mine = {}
        for raw in rows:
            it = build(raw, kind, default_type, today.isoformat())
            if it and it["id"] not in items:
                mine[it["id"]] = it
        rep["kept"] = len(mine)
        if mine:
            lo = min(mine)
            try:
                attach_units(mine, fetch_all(BASE, op_mdl, {"cond[HOUSE_MANAGE_NO::GTE]": lo}))
            except Exception as e:
                report["errors"].append(f"{kind} 주택형: {scrub(e)}")
            closed = {k: v for k, v in mine.items() if v["status"] in ("청약중", "발표대기")}
            if op_cmpet and closed:
                try:
                    attach_competition(mine, fetch_all(CMPET_BASE, op_cmpet, {"cond[HOUSE_MANAGE_NO::GTE]": min(closed)}))
                except Exception as e:
                    report["errors"].append(f"{kind} 경쟁률: {scrub(e)}")
        items.update(mine)
        report["kinds"][kind] = rep
        log(f"  [{kind}] 공고 {rep['rows']}건 → 진행·예정·발표대기 {rep['kept']}건")

    if ok_kinds == 0:
        log("  ❌ 모든 조회 실패 — 기존 파일 유지")
        for e in report["errors"][:5]:
            log(f"     {e}")
        return 1
    if not items and len(prev_by_id) > 0 and ok_kinds < len(KINDS):
        log("  ❌ 일부 조회 실패 + 결과 0건 — 기존 파일 유지")
        return 1

    out = []
    for it in items.values():
        old = prev_by_id.get(it["id"]) or {}
        if old.get("lat") and old.get("lng"):
            it["lat"], it["lng"] = old["lat"], old["lng"]       # 좌표는 이어받기
        if not it["competition"] and old.get("competition"):
            it["competition"] = old["competition"]
        it["scraped_date"] = old.get("scraped_date") or today.isoformat()
        out.append({k: v for k, v in it.items() if v not in (None, "", [], {}) or k in ("price_min", "price_max", "competition")})
    order = {"청약중": 0, "청약예정": 1, "발표대기": 2}
    out.sort(key=lambda x: (order.get(x["status"], 9), x.get("start_date") or ""))
    report["counts"] = {s: sum(1 for x in out if x["status"] == s) for s in order}
    report["with_price"] = sum(1 for x in out if x.get("price_max"))
    report["with_competition"] = sum(1 for x in out if x.get("competition"))
    doc = {"updated": today.isoformat(), "updated_at": datetime.now(KST).isoformat(timespec="seconds"),
           "source": "한국부동산원 청약홈 분양정보 조회 서비스 (공공데이터포털)", "report": report, "subscriptions": out}
    if prev.get("subscriptions") == out and prev.get("updated") == doc["updated"]:
        log(f"  변경 없음 — {len(out)}건 ({report['counts']})")
        return 0
    OUT_PATH.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"  ✅ 저장 {len(out)}건 — {report['counts']}, 분양가 있음 {report['with_price']}건, 경쟁률 있음 {report['with_competition']}건")
    for e in report["errors"][:8]:
        log(f"  ⚠ {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
