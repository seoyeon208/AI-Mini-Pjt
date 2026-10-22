"""Wave 4/10a — 결정론적 Fake LLM 2종 (fast/quality) + 실측 비용 계산.

실제 LLM 호출 없이도 파이프라인 전체가 결정론적으로 재현되도록, 프롬프트 내용에 따라
정해진 응답을 돌려준다. 각 호출은 LangSmith에 run_type="llm"으로 기록되고, 문자열
길이 기반 토큰 추정치로 비용을 계산해 상태에 누적한다 (Wave 10a의 모델 분리 근거).
"""
import re

from langsmith import traceable

from .evidence import monitoring_records, documents
from .planning import mentioned_systems


class FakeLLM:
    def __init__(self, name: str, usd_per_1k_tokens: float):
        self.name = name
        self.usd_per_1k = usd_per_1k_tokens

    def _usage(self, prompt: str, completion: str) -> dict:
        tokens = (len(prompt) + len(completion)) // 4 + 1
        return {"model": self.name, "tokens": tokens,
                "usd": round(tokens / 1000 * self.usd_per_1k, 6)}


    @traceable(run_type="llm", name="fake.write")
    async def write(self, query: str, collected: list[dict], attempt: int,
                    feedback: str | None, on_token=None, gaps=None) -> tuple[dict, dict]:
        observations = [{key: record[key] for key in ("system_name", "status", "cpu_percent", "memory_percent")}
                        for record in monitoring_records(collected).values()]
        actions = []
        for doc in documents(collected).values():
            steps = re.findall(r"^\d+\.\s+(.+)$", doc["data"], re.M)
            actions.extend({"text": step, "sources": [doc["id"]]} for step in steps[:2])
        report = {"observations": observations, "analysis": "조회한 상태와 관련 매뉴얼을 정리했습니다. 원인 확정에는 추가 확인이 필요합니다.",
                  "actions": actions, "limitations": [gap["reason"] for gap in gaps or []]}
        return report, self._usage(query, str(report))

    @traceable(run_type="llm", name="fake.validate")
    def validate(self, query: str, collected: list[dict], draft: dict, gaps: list[dict]) -> tuple[dict, dict]:
        # fake는 연결 확인용이다. 의미 품질 평가는 Ollama/OpenAI 모델이 수행한다.
        from .reporting import review_items
        claims, evidence = review_items(collected, draft)
        review = {"checks": [{"claim_id": id, "verdict": "supported", "evidence_ids": list(evidence)[:1],
                              "reason": "fake 연결 검사"} for id in claims]}
        return review, self._usage(str(draft), str(review))


class FastLLM(FakeLLM):
    """분류·계획 등 짧고 정형적인 단계 전담 (저비용)."""

    @traceable(run_type="llm", name="fast_llm.route")
    def route(self, query: str) -> tuple[dict, dict]:
        # 데모 전용 규칙이다. 실제 백엔드는 공통 프롬프트로 모델이 분류한다.
        analysis = bool(re.search(r"원인|분석|비교|진단", query))
        systems, unknown = mentioned_systems(query)
        runbook = bool(re.search(r"매뉴얼|runbook|절차", query, re.I))
        operational = bool(systems or unknown or re.search(r"사용률|현재|상태|장애|지연|급증|타임아웃", query))
        route = "incident" if analysis or len(systems) > 1 or "보고서" in query else "lookup" if runbook or operational else "chat"
        kind = target = None
        if route == "lookup":
            kind = "runbook" if runbook else "monitoring"
            if runbook:
                rules = [(r"CPU|급증", "cpu-spike"), (r"메모리|memory", "memory-leak"),
                         (r"지연|latency", "high-latency"), (r"타임아웃|네트워크", "network-timeout"),
                         (r"디스크|disk", "disk-full"), (r"DB|데이터베이스", "db-down")]
                target = next((t for pattern, t in rules if re.search(pattern, query, re.I)), None)
            else:
                target = systems[0] if len(systems) == 1 else None
        result = dict(route=route, needs_analysis=analysis, kind=kind, target=target,
                      reason="fake 데모 규칙으로 분류")
        return result, self._usage(query, str(result))

    @traceable(run_type="llm", name="fast_llm.plan")
    def plan(self, query: str, procedure_prompt: str) -> tuple[dict, dict]:
        """데모 계획. 불필요한 analysis 제안은 Gate가 제거하는지 확인하기 위해 남긴다."""
        systems, _ = mentioned_systems(query)
        tasks = [
            {"agent": "research", "kind": "monitoring", "target": s}
            for s in systems
        ]
        tasks.append({"agent": "analysis", "kind": "pattern-analysis", "target": None})
        result = {"tasks": tasks}
        return result, self._usage(query + procedure_prompt, str(result))

    @traceable(run_type="llm", name="fast_llm.chat")
    async def chat(self, query: str, history: list[dict], on_token=None) -> tuple[str, dict]:
        """Wave 5의 direct 경로 + Wave 12 증명용 — 히스토리에서 이름을 기억해낸다.

        fake 백엔드는 실제 네트워크 호출이 없어 토큰 단위로 쪼갤 대상이 없다 —
        ollama_llm.py와 인터페이스(on_token 콜백)만 맞추고 결과를 한 번에 반환한다."""
        for msg in reversed(history):
            m = re.search(r"제 이름은 (\S+?)입니다", msg.get("content", ""))
            if m and re.search(r"이름이 뭐", query):
                reply = f"{m.group(1)}님이라고 말씀하셨어요. 무엇을 도와드릴까요?"
                if on_token is not None:
                    on_token(reply)
                return reply, self._usage(query, reply)
        m = re.search(r"제 이름은 (\S+?)입니다", query)
        if m:
            reply = f"안녕하세요, {m.group(1)}님! 무엇을 도와드릴까요?"
        else:
            reply = "안녕하세요! 무엇을 도와드릴까요?"
        if on_token is not None:
            on_token(reply)
        return reply, self._usage(query, reply)


class QualityLLM(FakeLLM):
    """분석·최종 답변 작성 전담 (고품질·고비용)."""

    @traceable(run_type="llm", name="quality_llm.analyze")
    def analyze(self, collected: list[dict]) -> tuple[str, dict]:
        """Wave 6 보완 — 여러 시스템의 monitoring 결과 사이에 공통 패턴이 있는지 비교."""
        mon = [c["data"] for c in collected if c["source"] == "monitoring"]
        degraded = [d["system_name"] for d in mon if d.get("status") != "healthy"]
        if len(degraded) >= 2:
            summary = (f"{', '.join(degraded)} 복수 시스템이 동시에 저하됨 — "
                       f"공유 의존성 등 공통 원인이 의심됨")
        elif degraded:
            summary = f"{degraded[0]}만 단독 저하 — 다른 시스템과의 연관성은 추가 확인 필요"
        else:
            summary = "모니터링된 시스템 모두 healthy — 패턴상 이상 징후 없음"
        return summary, self._usage(str(collected), summary)

