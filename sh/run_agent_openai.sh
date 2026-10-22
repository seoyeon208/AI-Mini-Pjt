#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -x .venv/bin/python3 ]; then
    echo "먼저 bash sh/setup_venv.sh로 가상환경을 준비하세요."
    exit 1
fi

# dotenv parses .env as data; it is never executed as a shell script.
.venv/bin/python3 - <<'CHECK'
import os
from dotenv import load_dotenv
load_dotenv('.env')
if not os.environ.get('OPENAI_API_KEY', '').strip() or os.environ['OPENAI_API_KEY'] == 'sk-...':
    raise SystemExit('OPENAI_API_KEY를 .env에 설정하세요.')
CHECK

echo "OpenAI API 요청에는 비용이 발생합니다."
read -r -p "실행할까요? [y/N] " confirm
case "$confirm" in
    y|Y|yes|YES) ;;
    *) exit 0 ;;
esac
LLM_BACKEND=openai SUPERVISOR_VERSION=v2 exec .venv/bin/python3 server.py
