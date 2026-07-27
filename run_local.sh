#!/bin/bash
# ─────────────────────────────────────────────
# 경매 대시보드 자동 업데이트 스크립트
# launchd는 PATH가 없으므로 전체 경로 사용
# ─────────────────────────────────────────────

export PATH="/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON=/usr/bin/python3
GIT=/usr/bin/git

echo "=========================================="
echo " 경매 데이터 자동 업데이트"
echo " $(date '+%Y-%m-%d %H:%M:%S') KST"
echo "=========================================="

# ── 1단계: 스크래퍼 실행 ──────────────────────
echo ""
echo "[1단계] 스크래퍼 실행..."
$PYTHON scraper/scrape_auctions.py
SCRAPER_EXIT=$?

if [ $SCRAPER_EXIT -ne 0 ]; then
  echo ""
  echo "ERROR: 스크래퍼 오류 (exit $SCRAPER_EXIT) - 커밋 없이 종료"
  exit 1
fi

echo ""
echo "OK: 스크래퍼 완료"

# ── 2단계: 변경사항 확인 ──────────────────────
echo ""
echo "[2단계] 변경사항 확인..."
$GIT diff --stat docs/data/auctions.json

if $GIT diff --quiet docs/data/auctions.json; then
  echo "INFO: auctions.json 변경 없음 - 커밋 스킵"
  exit 0
fi

TOTAL=$($PYTHON -c "
import json
with open('docs/data/auctions.json') as f:
    d = json.load(f)
print(len(d.get('auctions', [])))
" 2>/dev/null || echo "?")

echo "  -> 총 ${TOTAL}건 수집"

# ── 3단계: Git 커밋 & 푸시 ───────────────────
echo ""
echo "[3단계] Git 커밋 & 푸시..."

$GIT add docs/data/auctions.json
$GIT commit -m "경매 데이터 업데이트: $(date '+%Y-%m-%d %H:%M') KST (총 ${TOTAL}건)"
$GIT pull --rebase origin main && $GIT push origin main

if [ $? -eq 0 ]; then
  echo ""
  echo "OK: 푸시 완료 - 총 ${TOTAL}건 업데이트됨"
else
  echo "WARN: 푸시 실패, 1회 재시도..."
  $GIT pull --rebase origin main && $GIT push origin main
fi

echo ""
echo "=========================================="
echo " 완료: $(date '+%Y-%m-%d %H:%M:%S') KST"
echo "=========================================="
