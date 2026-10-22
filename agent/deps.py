"""그래프 노드에서 공유하는 모델, MCP 클라이언트, 세션 저장소."""
from dataclasses import dataclass
from typing import Any


@dataclass
class SupervisorDeps:
    fast_llm: Any   # 분류·계획·대화 모델
    quality_llm: Any   # 분석·답변 작성 모델
    mcp: Any   # MCP stdio 클라이언트 (Tool/Resource/Prompt)
    session_store: Any   # SQLite 세션 저장소
    version: str   # SUPERVISOR_VERSION (v1/v2)
    review_llm: Any = None
