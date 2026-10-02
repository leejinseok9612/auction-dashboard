#!/bin/bash
# dev 가지에서 실행할 명령 (작업 단계마다 바꿔 가며 사용)
set -x
MAX_ENRICH_PER_RUN=600 RIGHTS_MAX_PER_RUN=80 python scraper/main.py
python scraper/filters.py --top 15 || true
