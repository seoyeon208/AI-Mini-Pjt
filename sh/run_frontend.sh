#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -x .venv/bin/python3 ]; then
    echo "먼저 bash sh/setup_venv.sh로 가상환경을 준비하세요."
    exit 1
fi

if ! curl -fsS --max-time 2 http://127.0.0.1:8100/health >/dev/null 2>&1; then
    echo "먼저 다른 터미널에서 백엔드(server.py)를 실행하세요."
    exit 1
fi

echo "채팅 화면: http://127.0.0.1:8600. 종료: Ctrl+C"
exec .venv/bin/python3 web_demo_server.py
