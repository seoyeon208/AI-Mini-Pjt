# Ops Desk

서비스 상태와 대응 매뉴얼을 조회하고, 근거와 확인할 사항을 함께 보여주는 운영 진단 도우미입니다.
수업 제공 프로젝트를 바탕으로 요청 분류, BM25 검색, 답변 검증과 사용자 화면을 확장했습니다.
모니터링 데이터와 매뉴얼은 실습용 예시이며 실제 운영 시스템에 연결되어 있지 않습니다.

## 주요 기능

- 요청 3개 유형 분류: 일반 대화, 단순 조회, 복합 분석
- Ollama 2B·8B 모델 분리, 단순 조회는 원문 기반 정형 응답
- 매뉴얼 6종의 BM25 검색, 조치 원문과 출처 연결
- 조사 계획 검증, 부족한 자료 추가 조사 최대 1회
- 코드 사실 대조와 LLM 검수, 답변 재작성 최대 1회
- MCP 부분 실패 격리와 독립 조회 동시 실행
- Vue 채팅 화면, SSE 진행 기록, 오류 안내와 질문 복원

## 실행

Python 3.11 이상을 사용합니다. 별도 프론트엔드 빌드는 필요 없으며 Vue는 CDN에서 불러옵니다.

```bash
bash sh/setup_venv.sh
cp .env.example .env
```

### 모델 없이 화면과 흐름 확인

첫 번째 터미널:

```bash
LLM_BACKEND=fake .venv/bin/python3 server.py
```

두 번째 터미널:

```bash
bash sh/run_frontend.sh
```

브라우저에서 http://127.0.0.1:8600 에 접속합니다. `fake`는 규칙 기반 데모이며 실제 모델 평가용이 아닙니다.

### 로컬 Ollama 모델 사용

Ollama 앱이나 `ollama serve`로 Ollama를 먼저 실행합니다. 모델이 없다면 준비 스크립트를 실행합니다.
기존 모델과 실행 중인 Ollama는 삭제하거나 종료하지 않습니다.

```bash
bash sh/setup_ollama.sh
bash sh/run_agent_ollma.sh
```

프론트엔드는 다른 터미널에서 `bash sh/run_frontend.sh`로 실행합니다.
모델을 바꾸면 `.env`의 모델명도 맞춰야 합니다. 준비 스크립트에 다른 모델명을 줄 때는
`OLLAMA_FAST_MODEL=... OLLAMA_QUALITY_MODEL=... bash sh/setup_ollama.sh`를 사용합니다.

OpenAI를 사용하려면 `.env`에 본인 `OPENAI_API_KEY`를 입력하고
`bash sh/run_agent_openai.sh`를 실행합니다. 이 경로는 실제 API 비용이 발생합니다.

## 사용 예

- `auth-service CPU 사용률 알려줘`
- `CPU 급증 대응 매뉴얼 찾아줘`
- `payments-service와 gateway-service의 공통 원인을 분석해줘`

Enter로 전송하고 Shift+Enter로 줄바꿈합니다. 조회·진단 대상은 화면에서 선택하거나 정확한 서비스 ID를 입력합니다.

## 테스트와 평가

외부 모델과 추적 전송을 사용하지 않는 회귀 테스트:

```bash
LANGCHAIN_TRACING_V2=false LANGSMITH_TRACING=false .venv/bin/python3 -m unittest discover -s tests -q
```

`evaluation/`의 고정 질문과 기존 결과는 개선 근거를 재검토할 수 있도록 유지했습니다.
새 평가 결과는 Git에서 제외되는 `evaluation/runs/`에 저장합니다.
결과에는 질문·답변·근거 원문이 포함될 수 있으므로 공개 전에 내용을 확인해야 합니다.

- [상세 구조와 동작](doc/README.md)
- [설계 결정과 한계](doc/ADR.md)
- [평가 방법](evaluation/README.md)
- [답변 검증 결과](evaluation/REVIEW-QUALITY.md)
- [MCP 동시 실행 측정](evaluation/MCP-CONCURRENCY.md)
- [브라우저 통합 점검](evaluation/INTEGRATION-CHECK.md)

## 파일 구성

```text
agent/                 요청 분류·계획·조사·보고서·검증·세션
data/                 예시 모니터링과 매뉴얼
tests/                회귀 테스트와 MCP fixture
evaluation/           평가 코드·고정 사례·기존 결과
doc/                  설계와 사용 문서
sh/                   환경 준비와 실행 스크립트
server.py             FastAPI 백엔드 (127.0.0.1:8100)
web_demo_server.py    웹 화면과 API 프록시 (127.0.0.1:8600)
web_chat.html         Vue 채팅 화면
```

## 로컬 데이터와 공개 범위

`.env`, 대화 DB, 로그, 가상환경, 캐시는 Git에서 제외합니다. 실행 시 `session.db`가 자동 생성되며
재시작해도 대화가 유지됩니다. 실행 스크립트는 DB를 초기화하거나 같은 포트의 다른 프로세스를 강제 종료하지 않습니다.

기본 예제 설정에서 LangSmith 외부 추적은 꺼져 있습니다. 추적을 켜면 질문과 답변 등이 외부로 전송될 수 있습니다.
서비스에는 사용자 인증이 없으며 로컬 사용을 전제로 합니다. 외부 공개 운영을 위한 인증과 세션 접근 통제는 포함하지 않습니다.
