#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -x .venv/bin/python3 ]; then
    echo "먼저 bash sh/setup_venv.sh로 가상환경을 준비하세요."
    exit 1
fi

if ! curl -fsS --max-time 2 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
    echo "Ollama 앱이나 ollama serve를 먼저 실행하세요."
    exit 1
fi

echo "백엔드: http://127.0.0.1:8100 (Ollama). 종료: Ctrl+C"
LLM_BACKEND=ollama SUPERVISOR_VERSION=v2 exec .venv/bin/python3 server.py
