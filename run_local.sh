#!/bin/bash
# ─────────────────────────────────────────────
# 경매 대시보드 자동 업데이트 스크립트
# launchd (plist) 또는 터미널에서 직접 실행 가능
# ─────────────────────────────────────────────

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

echo "=========================================="
echo " 경매 데이터 자동 업데이트"
echo " $(date '+%Y-%m-%d %H:%M:%S') KST"
echo "=========================================="

# ── 1단계: 스크래퍼 실행 ──────────────────────
echo ""
echo "[1단계] 스크래퍼 실행..."
python3 scraper/scrape_auctions.py
SCRAPER_EXIT=$?

if [ $SCRAPER_EXIT -ne 0 ]; then
  echo ""
  echo "❌ 스크래퍼 오류 (exit $SCRAPER_EXIT) — 커밋 없이 종료"
  exit 1
fi

echo ""
echo "✅ 스크래퍼 완료"

# ── 2단계: 변경사항 확인 ──────────────────────
echo ""
echo "[2단계] 변경사항 확인..."
git diff --stat docs/data/auctions.json

if git diff --quiet docs/data/auctions.json; then
  echo "ℹ️  auctions.json 변경 없음 — 커밋 스킵"
  exit 0
fi

# 신규/총 건수 출력
TOTAL=$(python3 -c "
import json
with open('docs/data/auctions.json') as f:
    d = json.load(f)
auctions = d.get('auctions', [])
print(len(auctions))
" 2>/dev/null || echo "?")

echo "  → 총 ${TOTAL}건 수집"

# ── 3단계: Git 커밋 & 푸시 ───────────────────
echo ""
echo "[3단계] Git 커밋 & 푸시..."

git add docs/data/auctions.json

git commit -m "🏠 경매 데이터 업데이트: $(date '+%Y-%m-%d %H:%M') KST (총 ${TOTAL}건)"

# 충돌 방지: pull --rebase 후 push
git pull --rebase origin main && git push origin main

if [ $? -eq 0 ]; then
  echo ""
  echo "✅ 푸시 완료 — 총 ${TOTAL}건 업데이트됨"
else
  echo ""
  echo "⚠️  푸시 실패, 1회 재시도..."
  git pull --rebase origin main && git push origin main
fi

echo ""
echo "=========================================="
echo " 완료: $(date '+%Y-%m-%d %H:%M:%S') KST"
echo "=========================================="
