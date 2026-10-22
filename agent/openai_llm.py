"""OpenAI Chat Completions 호출과 JSON 응답 파싱.

OPENAI_API_KEY가 필요하며 API 비용이 발생한다. JSON 모드 응답의 스키마는 코드에서 검사한다.
cost_usd는 total_tokens에 비교용 단가를 적용한 추정치이며 실제 청구액과 다르다."""
import asyncio
import os
import json

import httpx
from langsmith import traceable

from .reporting import (draft_schema, WRITE_PROMPT, REVIEW_PROMPT,
                        report_input, parse_report, parse_review, review_schema)
from .planning import PLAN_PROMPT, parse_plan
from .routing import ROUTE_PROMPT, parse_route

OPENAI_URL = "https://api.openai.com/v1/chat/completions"


class OpenAILLM:
    def __init__(self, model: str, usd_per_1k_tokens: float):
        self.name = f"openai:{model}"
        self.model = model
        self.usd_per_1k = usd_per_1k_tokens
        self.api_key = os.environ.get("OPENAI_API_KEY", "")

    def _chat(self, system: str, user: str, json_mode: bool = False) -> tuple[str, dict]:
        if not self.api_key:
            raise RuntimeError(
                "OPENAI_API_KEY가 설정되지 않았습니다 — .env 또는 환경변수로 지정하세요."
            )
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "temperature": 0,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.api_key}",
                   "Content-Type": "application/json"}
        resp = httpx.post(OPENAI_URL, json=payload, headers=headers, timeout=120.0)
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"]["content"]
        tokens = data.get("usage", {}).get("total_tokens", 0)
        usage = {"model": self.name, "tokens": tokens,
                 "usd": round(tokens / 1000 * self.usd_per_1k, 6)}
        return content, usage


    @traceable(run_type="llm", name="openai.write")
    async def write(self, query: str, collected: list[dict], attempt: int,
                    feedback: str | None, on_token=None, gaps=None) -> tuple[dict | None, dict]:
        user = report_input(query, collected, gaps or [], feedback=feedback)
        prompt = WRITE_PROMPT + "\nJSON schema:\n" + json.dumps(draft_schema(collected), ensure_ascii=False)
        content, usage = await asyncio.to_thread(self._chat, prompt, user, json_mode=True)
        usage["prompt"] = f"[SYSTEM]\n{prompt}\n\n[USER]\n{user}"
        return parse_report(content, collected), usage

    @traceable(run_type="llm", name="openai.validate")
    def validate(self, query: str, collected: list[dict], draft: dict, gaps: list[dict]) -> tuple[dict, dict]:
        prompt = REVIEW_PROMPT + "\nJSON schema:\n" + json.dumps(review_schema(collected, draft), ensure_ascii=False)
        content, usage = self._chat(prompt, report_input(query, collected, gaps, draft=draft), json_mode=True)
        return parse_review(content), usage


class OpenAIFastLLM(OpenAILLM):
    """분류·계획·대화에 사용하는 모델."""

    @traceable(run_type="llm", name="openai_fast.route")
    def route(self, query: str) -> tuple[dict, dict]:
        content, usage = self._chat(ROUTE_PROMPT, query, json_mode=True)
        return parse_route(content), usage

    @traceable(run_type="llm", name="openai_fast.plan")
    def plan(self, query: str, procedure_prompt: str) -> tuple[dict, dict]:
        user = f"## 표준 절차\n{procedure_prompt}\n\n## 사용자 요청\n{query}"
        content, usage = self._chat(PLAN_PROMPT, user, json_mode=True)
        return parse_plan(content), usage

    @traceable(run_type="llm", name="openai_fast.chat")
    async def chat(self, query: str, history: list[dict], on_token=None) -> tuple[str, dict]:
        """완성된 응답을 on_token 콜백으로 한 번에 전달한다. 토큰별 스트리밍은 사용하지 않는다."""
        system = ("당신은 간결한 한국어 비서다. 한두 문장으로 답하라. "
                  "대화 이력에서 사용자가 밝힌 이름이 있으면 정확히 기억해 답하라.")
        rendered = "\n".join(f"{m['role']}: {m['content']}" for m in history[-10:])
        user = (f"## 지금까지의 대화\n{rendered or '(없음)'}\n\n"
                f"## 사용자의 새 메시지\n{query}")
        content, usage = await asyncio.to_thread(self._chat, system, user)
        usage["prompt"] = f"[SYSTEM]\n{system}\n\n[USER]\n{user}"
        if on_token is not None:
            on_token(content)
        return content, usage


class OpenAIQualityLLM(OpenAILLM):
    """분석과 답변 작성에 사용하는 모델."""

    @traceable(run_type="llm", name="openai_quality.analyze")
    def analyze(self, collected: list[dict]) -> tuple[str, dict]:
        system = ("당신은 ICT 운영 패턴 분석가다. 여러 시스템의 모니터링 결과를 비교해 "
                  "공통 원인이 의심되는 패턴이 있는지 한두 문장으로 짧게 답하라. "
                  "패턴이 없으면 '특이 패턴 없음'이라고만 답하라.")
        mon = [c["data"] for c in collected if c["source"] == "monitoring"]
        facts = "\n".join(
            f"- {d['system_name']}: 상태={d['status']}, CPU {d['cpu_percent']}%, "
            f"알림 {d['alerts']}" for d in mon) or "(모니터링 데이터 없음)"
        return self._chat(system, facts)

