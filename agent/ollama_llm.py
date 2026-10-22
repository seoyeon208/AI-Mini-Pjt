"""Ollama 모델 호출과 구조화 응답 파싱.

기본 모델은 fast=qwen3.5:2b, quality=qwen3:8b다. 서버 실행과 모델 설치가 필요하다.
cost_usd는 실측 토큰 수에 비교용 단가를 적용한 추정치이며 실제 비용이 아니다."""
import asyncio
import json

import httpx
from langsmith import traceable

from .reporting import (draft_schema, WRITE_PROMPT, REVIEW_PROMPT,
                        report_input, parse_report, parse_review, review_schema)
from .planning import PLAN_SCHEMA, PLAN_PROMPT, parse_plan
from .routing import ROUTE_SCHEMA, ROUTE_PROMPT, parse_route

OLLAMA_URL = "http://127.0.0.1:11434/api/chat"


class OllamaLLM:
    review_think = False

    def __init__(self, model: str, usd_per_1k_tokens: float, *, think: bool | None = None):
        self.name = f"ollama:{model}"
        self.model = model
        self.usd_per_1k = usd_per_1k_tokens
        self.think = think

    def _chat(self, system: str, user: str, schema: dict | None = None, *, think: bool | None = None) -> tuple[str, dict]:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "stream": False,
            "options": {"temperature": 0.0},
        }
        if schema is not None:
            payload["format"] = schema
        effective_think = self.think if think is None else think
        if effective_think is not None:
            payload["think"] = effective_think
        resp = httpx.post(OLLAMA_URL, json=payload, timeout=300.0)
        resp.raise_for_status()
        data = resp.json()
        content = data["message"]["content"]
        tokens = (data.get("prompt_eval_count") or 0) + (data.get("eval_count") or 0)
        usage = {"model": self.name, "tokens": tokens,
                 "usd": round(tokens / 1000 * self.usd_per_1k, 6)}
        return content, usage

    async def _chat_stream(self, system: str, user: str, on_token=None) -> tuple[str, dict]:
        """일반 대화만 토큰 단위로 중계한다. 운영 보고서는 전체 검증 후 공개한다."""
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "stream": True,
            "options": {"temperature": 0.0},
        }
        if self.think is not None:
            payload["think"] = self.think
        chunks: list[str] = []
        usage = {"model": self.name, "tokens": 0, "usd": 0.0}
        async with httpx.AsyncClient(timeout=300.0) as client:
            async with client.stream("POST", OLLAMA_URL, json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    data = json.loads(line)
                    piece = data.get("message", {}).get("content", "")
                    if piece:
                        chunks.append(piece)
                        if on_token is not None:
                            on_token(piece)
                    if data.get("done"):
                        tokens = ((data.get("prompt_eval_count") or 0)
                                  + (data.get("eval_count") or 0))
                        usage = {"model": self.name, "tokens": tokens,
                                 "usd": round(tokens / 1000 * self.usd_per_1k, 6)}
        usage["prompt"] = f"[SYSTEM]\n{system}\n\n[USER]\n{user}"
        return "".join(chunks), usage


    @traceable(run_type="llm", name="ollama.write")
    async def write(self, query: str, collected: list[dict], attempt: int,
                    feedback: str | None, on_token=None, gaps=None) -> tuple[dict | None, dict]:
        user = report_input(query, collected, gaps or [], feedback=feedback)
        schema = draft_schema(collected)
        prompt = WRITE_PROMPT + "\nJSON schema:\n" + json.dumps(schema, ensure_ascii=False)
        content, usage = await asyncio.to_thread(self._chat, prompt, user, schema=schema)
        usage["prompt"] = f"[SYSTEM]\n{prompt}\n\n[USER]\n{user}"
        return parse_report(content, collected), usage

    @traceable(run_type="llm", name="ollama.validate")
    def validate(self, query: str, collected: list[dict], draft: dict, gaps: list[dict]) -> tuple[dict, dict]:
        content, usage = self._chat(REVIEW_PROMPT, report_input(query, collected, gaps, draft=draft),
                                    schema=review_schema(collected, draft), think=self.review_think)
        return parse_review(content), usage


class OllamaFastLLM(OllamaLLM):
    """분류·계획·대화에 사용하는 모델."""

    @traceable(run_type="llm", name="ollama_fast.route")
    def route(self, query: str) -> tuple[dict, dict]:
        content, usage = self._chat(ROUTE_PROMPT, query, schema=ROUTE_SCHEMA)
        return parse_route(content), usage

    @traceable(run_type="llm", name="ollama_fast.plan")
    def plan(self, query: str, procedure_prompt: str) -> tuple[dict, dict]:
        user = f"## 표준 절차\n{procedure_prompt}\n\n## 사용자 요청\n{query}"
        content, usage = self._chat(PLAN_PROMPT, user, schema=PLAN_SCHEMA)
        return parse_plan(content), usage

    @traceable(run_type="llm", name="ollama_fast.chat")
    async def chat(self, query: str, history: list[dict], on_token=None) -> tuple[str, dict]:
        system = ("당신은 간결한 한국어 비서다. 한두 문장으로 답하라. "
                  "대화 이력에서 사용자가 밝힌 이름이 있으면 정확히 기억해 답하라.")
        rendered = "\n".join(f"{m['role']}: {m['content']}" for m in history[-10:])
        user = (f"## 지금까지의 대화\n{rendered or '(없음)'}\n\n"
                f"## 사용자의 새 메시지\n{query}")
        return await self._chat_stream(system, user, on_token=on_token)


class OllamaQualityLLM(OllamaLLM):
    """분석과 답변 작성에 사용하는 모델."""

    @traceable(run_type="llm", name="ollama_quality.analyze")
    def analyze(self, collected: list[dict]) -> tuple[str, dict]:
        system = ("당신은 ICT 운영 패턴 분석가다. 여러 시스템의 모니터링 결과를 비교해 "
                  "공통 원인이 의심되는 패턴이 있는지 한두 문장으로 짧게 답하라. "
                  "패턴이 없으면 '특이 패턴 없음'이라고만 답하라.")
        mon = [c["data"] for c in collected if c["source"] == "monitoring"]
        facts = "\n".join(
            f"- {d['system_name']}: 상태={d['status']}, CPU {d['cpu_percent']}%, "
            f"알림 {d['alerts']}" for d in mon) or "(모니터링 데이터 없음)"
        return self._chat(system, facts)

