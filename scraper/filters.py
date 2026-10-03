#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
경매 매물 검색·필터 로직 (filters.py)
────────────────────────────────────────────────────────────
대시보드 화면(docs/index.html)의 검색 조건과 "같은 정의"를 파이썬으로 구현한 모듈.

  derive(item)            물건 1건에서 검색용 파생 필드 계산 (main.py 가 저장 시 호출)
  matches(item, criteria) 물건이 조건에 맞는지 판정
  filter_items(items, criteria)  조건에 맞는 물건만 반환
  DEFAULT_CRITERIA        첫 화면 기본값 = '클린 매물'

터미널에서 리포트 출력:
  python3 scraper/filters.py                      # 기본(클린 매물) 조건
  python3 scraper/filters.py --all                # 조건 없이 전체
  python3 scraper/filters.py --sido 서울 --sigungu 마포구 --margin 20 --under-100m
  python3 scraper/filters.py --types apt villa --price-max 3 --gap-max 3000 --json

판정 원칙
  · "제외" 필터(위반건축물·근생·특수물건)는 '확인된' 물건만 숨긴다. (자료가 아직 없는 물건은 통과)
  · "값 조건" 필터(안전마진·공시가·갭·사용승인일)는 값이 없는 물건을 결과에서 뺀다. (모르는 값을 충족으로 치지 않음)
  · 권리 등급은 '미확인'을 안전으로 취급하지 않는다.
  · 안전마진 조건은 비교 거래 신뢰도가 '보통' 이상인 물건만 충족으로 본다.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
AUCTIONS_PATH = ROOT / "docs" / "data" / "auctions.json"

SIDO_SHORT = {"서울특별시": "서울", "서울시": "서울", "경기도": "경기", "인천광역시": "인천", "인천시": "인천"}

# 물건 종류 분류 (화면의 다중 선택 칩과 동일)
TYPE_LABELS = {"apt": "아파트", "villa": "연립/다세대(빌라)", "offi": "오피스텔", "house": "단독/다가구", "other": "기타"}

# 권리 등급
GRADE_LABELS = {"safe": "🟢 안전", "waiver": "🟡 대항력 포기", "caution": "🟠 주의", "danger": "🔴 위험", "unknown": "⚪ 미확인"}

# '특수물건'으로 보는 키워드 (법정지상권/유치권 등 — 권리관계가 복잡한 물건)
#   특별매각조건은 대부분 '대항력 포기 조건'·'재매각 보증금 20%' 라서 특수물건으로 치지 않는다.
SPECIAL_KEYWORDS = {"유치권", "법정지상권", "분묘기지권", "지분 매각", "선순위 가처분", "선순위 권리", "예고등기",
                    "대지권 미등기", "제시외 건물 매각제외", "농지취득자격증명", "맹지"}
# 대항력 포기 확약이 있으면 해소되는(임차인 관련) 키워드
TENANT_KEYWORDS = {"대항력 있는 임차인", "보증금 인수", "대항력 여지", "HUG 관련 조건", "대항력 포기",
                   "임차인 전입일 미상", "특별매각조건"}
DANGER_KEYWORDS = {"유치권", "법정지상권", "분묘기지권", "선순위 가처분", "선순위 권리", "예고등기", "대지권 미등기",
                   "보증금 인수", "대항력 있는 임차인", "지분 매각", "인수되는 권리"}

# 최저매각가격 구간 (억 원) — (최소 이상, 최대 미만)
PRICE_PRESETS = {"~1": (None, 1), "1~3": (1, 3), "3~5": (3, 5), "5~7": (5, 7), "7~10": (7, 10), "10~": (10, None)}


# ════════════════════════════════════════════════════════════
# 파생 필드
# ════════════════════════════════════════════════════════════
def split_region(address: str) -> tuple[str | None, str | None]:
    """주소 → (시/도 약칭, 구/군). 예: '경기도 성남시 분당구 …' → ('경기', '성남시 분당구')"""
    tokens = re.sub(r"^\s*사용본거지\s*:\s*", "", address or "").split()
    if len(tokens) < 2:
        return None, None
    sido = SIDO_SHORT.get(tokens[0])
    if not sido:
        return None, None
    sgg = tokens[1]
    if not re.search(r"[시군구]$", sgg):
        return sido, None
    # 일반구가 있는 시: '성남시 분당구', '수원시 장안구'
    if sgg.endswith("시") and len(tokens) > 2 and tokens[2].endswith("구"):
        sgg = f"{sgg} {tokens[2]}"
    return sido, sgg


def type_category(property_type: str) -> str:
    """법원 물건 유형 → apt / villa / offi / house / other"""
    pt = property_type or ""
    if "아파트" in pt:
        return "apt"
    if any(k in pt for k in ("다세대", "연립", "빌라")):
        return "villa"
    if "오피스텔" in pt:
        return "offi"
    if any(k in pt for k in ("단독", "다가구")):
        return "house"
    return "other"


def rights_grade(rights_risk: str | None, keywords: list[str] | None) -> str:
    """
    권리 등급
      waiver  법원이 '대항력 포기(우선변제권만 행사)' 조건을 공고함 (HUG 등) — 임차인 관련 외 다른 위험이 없을 때만.
              법원 공고(물건비고)에 근거하므로 명세서 확인 전이라도 부여한다.
      safe    매각물건명세서가 작성돼 있고 위험·주의 키워드와 선순위 임차인이 없음
      caution / danger / unknown(명세서 미작성 또는 아직 조회 전)
    """
    kws = set(keywords or [])
    if "대항력 포기" in kws:
        others = (kws - TENANT_KEYWORDS) & (DANGER_KEYWORDS | SPECIAL_KEYWORDS)
        return "danger" if others else "waiver"
    if rights_risk == "위험" or (kws & DANGER_KEYWORDS):
        return "danger"
    if rights_risk == "주의":
        return "caution"
    if rights_risk == "안전":
        return "safe"
    return "unknown"


def derive(item: dict) -> dict:
    """검색용 파생 필드 계산 → dict 반환 (item 을 직접 수정하지 않음)"""
    sido, sgg = split_region(item.get("address", ""))
    use = item.get("actual_use")
    op, mb = item.get("official_price"), item.get("min_bid")
    kws = item.get("rights_keywords") or []
    grade = rights_grade(item.get("rights_risk"), kws)
    return {
        "sido": sido,
        "sigungu": sgg,
        "type_category": type_category(item.get("property_type", "")),
        # 대장상 용도가 근린생활시설인지 (용도 미확인이면 None)
        "is_geunsaeng": ("근린생활" in use) if use else None,
        # 예상 실투금(갭) = 최저가 - 공시가 × 1.26 (공시가 미확인이면 None, 음수 = 전세가가 최저가보다 높음)
        "expected_gap": int(mb - round(op * 1.26)) if (op and mb) else None,
        "rights_grade": grade,
        # 특수물건 여부 — 법원 특수조건·물건비고 기준이라 명세서 확인 전에도 판정 가능
        "is_special_case": bool(set(kws) & SPECIAL_KEYWORDS),
    }


DERIVED_FIELDS = ["sido", "sigungu", "type_category", "is_geunsaeng", "expected_gap", "rights_grade", "is_special_case"]


# ════════════════════════════════════════════════════════════
# 조건 판정
# ════════════════════════════════════════════════════════════
# 첫 화면 기본값: '클린 매물'
DEFAULT_CRITERIA: dict = {
    "sido": None,                 # '서울' / '경기' / '인천'
    "sigungu": None,              # '마포구' / '성남시 분당구'
    "types": [],                  # ['apt','villa','offi','house'] — 비우면 전체
    "price_min": None,            # 최저매각가격 하한 (원, 이상)
    "price_max": None,            # 최저매각가격 상한 (원, 미만)
    "failed_min": None,           # 유찰 N회 이상 (0 = 신건만)
    "age": None,                  # 'new5'(5년 이내) / 'new10'(10년 이내) / 'old15'(15년 이상)
    "exclude_illegal": True,      # 위반건축물 제외
    "exclude_geunsaeng": True,    # 근린생활시설 제외
    "exclude_special": False,     # 법정지상권/유치권 등 특수물건 제외
    "under_100m": False,          # 공시가격 1억 이하만
    "margin_min": None,           # 안전마진 % 이상
    "gap_max": None,              # 예상 실투금(갭) 상한 (원, 이하)
    "grades": ["safe", "waiver"], # 허용할 권리 등급 — 비우면 전체
    "include_past": False,        # 매각기일이 지난 물건 포함
}


def _years_since(ymd: str, today: date) -> float | None:
    try:
        y, m, d = (int(x) for x in ymd.split("-"))
        return (today - date(y, m, d)).days / 365.25
    except (ValueError, AttributeError):
        return None


def matches(item: dict, criteria: dict | None = None, today: date | None = None) -> bool:
    c = {**DEFAULT_CRITERIA, **(criteria or {})}
    today = today or date.today()
    d = {**derive(item), **{k: item[k] for k in DERIVED_FIELDS if item.get(k) is not None}}

    # ── 1. 기본 검색 ──
    if not c["include_past"] and (item.get("auction_date") or "9999") < today.isoformat():
        return False
    if c["sido"] and d["sido"] != c["sido"]:
        return False
    if c["sigungu"] and d["sigungu"] != c["sigungu"]:
        return False
    if c["types"] and d["type_category"] not in c["types"]:
        return False
    bid = item.get("min_bid") or 0
    if c["price_min"] is not None and bid < c["price_min"]:
        return False
    if c["price_max"] is not None and bid >= c["price_max"]:
        return False
    fb = item.get("failed_bids") or 0
    if c["failed_min"] is not None:
        if c["failed_min"] == 0:
            if fb != 0:
                return False
        elif fb < c["failed_min"]:
            return False
    if c["age"]:
        yrs = _years_since(item.get("approval_date"), today)
        if yrs is None:
            return False
        if c["age"] == "new5" and yrs > 5:
            return False
        if c["age"] == "new10" and yrs > 10:
            return False
        if c["age"] == "old15" and yrs < 15:
            return False

    # ── 2. 리스크 차단 ──
    if c["exclude_illegal"] and item.get("is_illegal_building") is True:
        return False
    if c["exclude_geunsaeng"] and d["is_geunsaeng"] is True:
        return False
    if c["exclude_special"] and d["is_special_case"] is True:
        return False

    # ── 3. 투자·세금 조건 ──
    if c["under_100m"]:
        op = item.get("official_price")
        if not op or op > 100_000_000:
            return False
    if c["margin_min"] is not None:
        sm = item.get("safety_margin_pct")
        # 비교 거래의 신뢰도가 낮으면(다른 단지·면적 환산 등) 마진 수치를 믿기 어려우므로 조건 충족으로 치지 않는다
        if sm is None or sm < c["margin_min"] or item.get("nearby_trade_confidence") not in ("high", "medium"):
            return False
    if c["gap_max"] is not None:
        if d["expected_gap"] is None or d["expected_gap"] > c["gap_max"]:
            return False

    # ── 4. 권리 등급 ──
    if c["grades"] and d["rights_grade"] not in c["grades"]:
        return False
    return True


def filter_items(items: list[dict], criteria: dict | None = None, today: date | None = None) -> list[dict]:
    return [i for i in items if matches(i, criteria, today)]


# ════════════════════════════════════════════════════════════
# CLI 리포트
# ════════════════════════════════════════════════════════════
def _fmt_won(n) -> str:
    if n is None:
        return "-"
    if abs(n) >= 1e8:
        return f"{n / 1e8:.2f}억"
    return f"{round(n / 1e4):,}만"


def main() -> int:
    ap = argparse.ArgumentParser(description="조건에 맞는 경매 매물 리포트")
    ap.add_argument("--all", action="store_true", help="조건 없이 전체 (클린 매물 기본값 해제)")
    ap.add_argument("--sido", choices=["서울", "경기", "인천"])
    ap.add_argument("--sigungu", help="예: 마포구 / '성남시 분당구'")
    ap.add_argument("--types", nargs="*", choices=["apt", "villa", "offi", "house"], default=None)
    ap.add_argument("--price", choices=list(PRICE_PRESETS), help="최저가 구간 (억)")
    ap.add_argument("--price-min", type=float, help="최저가 하한 (억)")
    ap.add_argument("--price-max", type=float, help="최저가 상한 (억)")
    ap.add_argument("--failed", type=int, help="유찰 N회 이상 (0=신건)")
    ap.add_argument("--age", choices=["new5", "new10", "old15"])
    ap.add_argument("--include-illegal", action="store_true", help="위반건축물 포함")
    ap.add_argument("--include-geunsaeng", action="store_true", help="근린생활시설 포함")
    ap.add_argument("--exclude-special", action="store_true", help="특수물건 제외")
    ap.add_argument("--under-100m", action="store_true", help="공시가 1억 이하만")
    ap.add_argument("--margin", type=float, help="안전마진 %% 이상")
    ap.add_argument("--gap-max", type=float, help="예상 실투금 상한 (만원)")
    ap.add_argument("--grades", nargs="*", choices=list(GRADE_LABELS), default=None,
                    help="허용 권리 등급 (기본: safe waiver)")
    ap.add_argument("--json", action="store_true", help="JSON 으로 출력")
    ap.add_argument("--top", type=int, default=30, help="출력 건수 (기본 30)")
    a = ap.parse_args()

    c = dict(DEFAULT_CRITERIA)
    if a.all:
        c.update(exclude_illegal=False, exclude_geunsaeng=False, grades=[])
    c["sido"], c["sigungu"] = a.sido, a.sigungu
    if a.types is not None:
        c["types"] = a.types
    if a.price:
        lo, hi = PRICE_PRESETS[a.price]
        c["price_min"], c["price_max"] = (lo * 1e8 if lo else None), (hi * 1e8 if hi else None)
    if a.price_min is not None:
        c["price_min"] = a.price_min * 1e8
    if a.price_max is not None:
        c["price_max"] = a.price_max * 1e8
    c["failed_min"], c["age"] = a.failed, a.age
    if a.include_illegal:
        c["exclude_illegal"] = False
    if a.include_geunsaeng:
        c["exclude_geunsaeng"] = False
    c["exclude_special"], c["under_100m"], c["margin_min"] = a.exclude_special, a.under_100m, a.margin
    if a.gap_max is not None:
        c["gap_max"] = a.gap_max * 1e4
    if a.grades is not None:
        c["grades"] = a.grades

    with open(AUCTIONS_PATH, encoding="utf-8") as f:
        items = json.load(f).get("auctions", [])
    hits = filter_items(items, c)
    # 안전마진 높은 순 (값 없는 물건은 뒤로), 그다음 매각기일 빠른 순
    hits.sort(key=lambda x: (-(x.get("safety_margin_pct") if x.get("safety_margin_pct") is not None else -1e9),
                             x.get("auction_date") or "9999"))
    if a.json:
        json.dump(hits[: a.top], sys.stdout, ensure_ascii=False, indent=2)
        print()
        return 0

    active = [i for i in items if (i.get("auction_date") or "9999") >= date.today().isoformat()]
    unknown = sum(1 for i in active if rights_grade(i.get("rights_risk"), i.get("rights_keywords")) == "unknown")
    print(f"전체 {len(items)}건 (입찰 예정 {len(active)}건) 중 조건 충족 {len(hits)}건")
    if c["grades"] and unknown:
        print(f"※ 권리분석 미확인 {unknown}건은 권리 등급 조건 때문에 제외됨 (--grades 로 조정)")
    print("-" * 100)
    for i in hits[: a.top]:
        d = derive(i)
        sm = i.get("safety_margin_pct")
        print(f"{i.get('auction_date') or '-':10s} {i['id']:14s} {TYPE_LABELS[d['type_category']][:6]:6s} "
              f"최저 {_fmt_won(i.get('min_bid')):>8s} 마진 {(str(sm) + '%') if sm is not None else '-':>6s} "
              f"갭 {_fmt_won(d['expected_gap']):>8s} {GRADE_LABELS[d['rights_grade']]} {i.get('address', '')[:36]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
