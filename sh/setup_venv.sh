#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

command -v python3 >/dev/null 2>&1 \
    || { echo "ERROR: python3가 설치되어 있지 않습니다."; exit 1; }

if [ -x ".venv/bin/python3" ]; then
    echo "기존 .venv의 패키지를 확인합니다."
else
    echo "가상환경을 생성합니다."
    python3 -m venv .venv
fi

echo "requirements.txt 설치 중..."
./.venv/bin/python3 -m pip install --upgrade pip --quiet
./.venv/bin/python3 -m pip install -r requirements.txt

echo
echo "설치 확인:"
./.venv/bin/python3 -c "import fastapi, uvicorn, langgraph, langsmith, fastmcp, mcp, httpx, dotenv; print('패키지 import 확인 완료')"

cat <<EOF

 .venv 준비 완료

이제 아래 중 하나로 에이전트를 띄울 수 있습니다:
  bash sh/run_agent_ollma.sh     (로컬 Ollama)
  bash sh/run_agent_openai.sh    (OpenAI API, 과금)

Ollama를 처음 쓰거나 모델을 다시 받아야 하면 먼저:
  bash sh/setup_ollama.sh
EOF
