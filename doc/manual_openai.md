# OpenAI 사용 가이드

환경 준비는 [프로젝트 README](../README.md#실행)를 참고합니다.
`.env`의 `OPENAI_API_KEY`에 본인 키를 입력하고 다음 명령으로 실행합니다.

```bash
bash sh/run_agent_openai.sh
```

실행 확인 후 보내는 요청은 유료 OpenAI API를 호출합니다. 질문과 수집한 근거가 외부 API로 전송됩니다.
모델은 `.env`의 `OPENAI_FAST_MODEL`, `OPENAI_QUALITY_MODEL`로 선택합니다.

다른 터미널에서 `bash sh/run_frontend.sh`를 실행하고 http://127.0.0.1:8600 에 접속합니다.
화면 사용과 질문 예시는 [사용 가이드](manual_ollma.md#질문-예시)를 참고합니다.
이전 버전의 점수 평가 화면은 현재 검증 방식과 달라 제외했습니다.
현재 저장된 Ollama 평가 결과를 OpenAI 모델의 검증 결과로 해석하지 않습니다.
