#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

command -v ollama >/dev/null 2>&1 || { echo "Ollama를 먼저 설치하세요."; exit 1; }
if ! curl -fsS --max-time 2 http://127.0.0.1:11434/api/tags >/dev/null 2>&1; then
    echo "Ollama 앱이나 ollama serve를 먼저 실행하세요."
    exit 1
fi

for model in "${OLLAMA_FAST_MODEL:-qwen3.5:2b}" "${OLLAMA_QUALITY_MODEL:-qwen3:8b}"; do
    if ollama show "$model" >/dev/null 2>&1; then
        echo "설치된 모델 사용: $model"
    else
        ollama pull "$model"
    fi
done
