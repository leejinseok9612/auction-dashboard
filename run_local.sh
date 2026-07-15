#!/bin/bash
# ══════════════════════════════════════════════
#  경매 스크래퍼 로컬 실행 스크립트
#  - 매일 아침 자동 실행 (launchd 연동)
#  - 결과를 GitHub에 자동 push
# ══════════════════════════════════════════════

# 설정값 자동 감지
REPO_DIR="$(cd "$(dirname "$0")" && pwd)"   # 이 파일이 있는 폴더 = repo 루트

# GitHub PAT 토큰을 별도 파일에서 읽기 (.gitignore 처리됨)
CONFIG_FILE="$REPO_DIR/.local_config"
if [ ! -f "$CONFIG_FILE" ]; then
  echo "❌ 설정 파일이 없습니다. 아래 명령어로 생성하세요:"
  echo ""
  echo "  echo 'GIT_TOKEN=여기에_토큰_입력' > $CONFIG_FILE"
  echo ""
  exit 1
fi
source "$CONFIG_FILE"

# Python 경로 자동감지 (python3 → python 순서)
PYTHON=$(command -v python3 || command -v python)
if [ -z "$PYTHON" ]; then
  echo "❌ Python이 설치되어 있지 않습니다."
  exit 1
fi

LOG="$REPO_DIR/scraper/scraper.log"
echo "" >> "$LOG"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━" >> "$LOG"
echo "▶ 시작: $(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG"

# 패키지 확인 & 설치
$PYTHON -c "import playwright" 2>/dev/null || {
  echo "📦 playwright 설치 중..." | tee -a "$LOG"
  $PYTHON -m pip install playwright --quiet
  $PYTHON -m playwright install chromium --quiet
}

# 스크래퍼 실행
echo "🔍 경매 데이터 수집 중..." | tee -a "$LOG"
$PYTHON "$REPO_DIR/scraper/scrape_auctions.py" 2>&1 | tee -a "$LOG"

# Git push
cd "$REPO_DIR"
git add docs/data/auctions.json docs/data/cheongyak.json docs/index.html

if git diff --cached --quiet; then
  echo "ℹ️  변경 데이터 없음 — 커밋 스킵" | tee -a "$LOG"
else
  git config user.name  "auction-bot"
  git config user.email "bot@local"
  git commit -m "chore: 경매 데이터 업데이트 $(date '+%Y-%m-%d %H:%M')" >> "$LOG" 2>&1
  git remote set-url origin "https://leejinseok9612:${GIT_TOKEN}@github.com/leejinseok9612/auction-dashboard.git"
  git push origin main >> "$LOG" 2>&1
  echo "✅ GitHub 업로드 완료!" | tee -a "$LOG"
fi

echo "◀ 완료: $(date '+%Y-%m-%d %H:%M:%S')" >> "$LOG"
