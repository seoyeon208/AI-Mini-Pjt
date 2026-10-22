"""모델은 작업을 제안하고, 코드는 조사 대상과 실행할 작업을 확정한다."""
import json
import re

from .routing import KNOWN_SYSTEMS

PLAN_SCHEMA = {
    "type": "object",
    "properties": {"tasks": {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "enum": ["research", "analysis"]},
                "kind": {"type": "string", "enum": ["monitoring", "pattern-analysis"]},
                "target": {"type": ["string", "null"]},
            },
            "required": ["agent", "kind", "target"],
        },
    }},
    "required": ["tasks"],
}
PLAN_PROMPT = (
    "당신은 ICT 운영 Supervisor의 planning 노드다. 표준 진단 절차를 참고해 작업을 제안하라. "
    "사용자가 지정한 시스템마다 research/monitoring 작업을 하나씩 만든다. "
    "미등록 시스템을 다른 시스템으로 바꾸거나, 요청하지 않은 시스템을 추가하지 마라. "
    "패턴 분석은 analysis/pattern-analysis(target=null)로 제안한다. "
    "매뉴얼은 조사 단계의 검색 결과에서 선택하므로 계획에 매뉴얼 ID를 지정하지 마라. "
    "작업의 최종 실행 여부는 코드가 결정한다. "
    f"등록 시스템: {', '.join(sorted(KNOWN_SYSTEMS))}. "
    "다음 스키마에 맞는 JSON 객체만 출력하라:\n"
) + json.dumps(PLAN_SCHEMA, ensure_ascii=False)


def mentioned_systems(query: str) -> tuple[list[str], list[str]]:
    # 영문 식별자 단위로 읽어 unknown-auth-service를 auth-service로 잘못 매칭하지 않는다.
    tokens = dict.fromkeys(re.findall(r"[a-zA-Z0-9_][a-zA-Z0-9_-]*", query))
    names = [t for t in tokens if t in KNOWN_SYSTEMS or
             re.search(r"[-_]service(?:[-_]|$)", t) or
             re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]*-\d+", t)]
    return ([s for s in names if s in KNOWN_SYSTEMS],
            [s for s in names if s not in KNOWN_SYSTEMS])


def parse_plan(content: str) -> dict:
    try:
        raw = json.loads(content)
    except json.JSONDecodeError:
        raw = None
    tasks = raw.get("tasks") if isinstance(raw, dict) else None
    # 대상 치환·누락 보완은 여기서 하지 않는다. 모든 백엔드가 같은 Gate를 거친다.
    return {"tasks": tasks if isinstance(tasks, list) else []}


def validate_tasks(tasks: list, systems: list[str], needs_analysis: bool) -> tuple[list[dict], list[dict], list[str]]:
    kept, rejected, seen = [], [], set()
    for task in tasks:
        if not isinstance(task, dict):
            rejected.append({"reason": "작업 형식 오류"})
            continue
        agent, kind, target = task.get("agent"), task.get("kind"), task.get("target")
        reason = None
        if agent == "research":
            if kind == "runbook":
                reason = "매뉴얼은 검색 결과에서 선택"
            elif kind != "monitoring":
                reason = "지원하지 않는 조사 작업"
            elif not isinstance(target, str):
                reason = "조사 대상 형식 오류"
            elif target not in systems:
                reason = "사용자가 지정한 등록 시스템이 아님"
        elif agent == "analysis":
            target = None
            if not needs_analysis:
                reason = "분석이 필요하지 않은 요청"
            elif kind != "pattern-analysis":
                reason = "지원하지 않는 분석 작업"
        else:
            reason = "지원하지 않는 에이전트"
        if reason is None and (agent, kind, target) in seen:
            reason = "중복 작업"
        if reason:
            rejected.append({"agent": agent if isinstance(agent, str) else None,
                             "target": target if isinstance(target, str) else None, "reason": reason})
            continue
        seen.add((agent, kind, target))
        kept.append({"agent": agent, "kind": kind, "target": target})

    added_targets = [s for s in systems if ("research", "monitoring", s) not in seen]
    kept.extend({"agent": "research", "kind": "monitoring", "target": s} for s in added_targets)
    if needs_analysis and ("analysis", "pattern-analysis", None) not in seen:
        kept.append({"agent": "analysis", "kind": "pattern-analysis", "target": None})
    # 모델이 준 id/status로 조사를 건너뛰거나 서로 다른 작업을 같은 것으로 취급하지 않는다.
    return ([{**t, "id": f"task-{i}", "status": "pending"} for i, t in enumerate(kept)],
            rejected, added_targets)
