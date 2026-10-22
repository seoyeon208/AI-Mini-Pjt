"""v2 Supervisor — 실제 langgraph.StateGraph (Wave 4·5·6·7·8·10b·15 통합).

route → (chat 직행 | lookup: gate → research → 원문 구성·대조 | incident 파이프라인)
incident: plan(MCP Prompt 절차 반영) → gate(권위 게이트) → research(가드+병렬+재시도)
          → replan_check(부족 시 gap task 후 재조사) → analysis(needs_analysis=True일 때만
          실제 실행) → write → validate(반려 시 재작성) → finalize

모든 노드는 SupervisorDeps를 functools.partial로 주입받는 순수 함수다(Wave 13).
각 노드는 @traceable로 LangSmith에 계층 트리로 기록된다.
"""
import asyncio
import time
from functools import partial
from typing import Any, TypedDict

from langgraph.config import get_stream_writer
from langgraph.graph import END, StateGraph
from langsmith import traceable

from .deps import SupervisorDeps
from .retry import with_retry
from .routing import KNOWN_SYSTEMS, KNOWN_RUNBOOKS
from .evidence import assess_evidence, documents, monitoring_records
from .reporting import check_facts, checked_review_issues, lookup_report, report_sections, render_report
from .planning import mentioned_systems, validate_tasks


class AgentState(TypedDict, total=False):
    query: str
    thread_id: str
    history: list[dict]
    route: str
    needs_analysis: bool
    routing_reason: str
    response_model: str
    report_kind: str
    requested_systems: list[str]
    tasks: list[dict]
    gate_removed: int
    gate_added: int
    gate_blocked_reason: str | None
    collected: list[dict]
    replan_rounds: int
    retry_count: int
    parallel_elapsed: float
    draft: dict | None
    validation_attempts: int
    validation_feedback: str | None
    validation_status: str
    validation_method: str
    validation_issues: list[str]
    validation_scores: dict
    validation_review: dict
    evidence_gaps: list[dict]
    sources: list[dict]
    report: dict
    research_failures: list[dict]
    final: str
    last_prompt: str
    cost_usd: float
    events: list[dict]


RESEARCH_TIMEOUT_SECONDS = 15

def _cost(state: AgentState, usage: dict) -> float:
    return round(state.get("cost_usd", 0.0) + usage["usd"], 6)


@traceable(name="route_node")
async def route_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 5 — 모든 쿼리가 무거운 파이프라인을 탈 필요는 없다."""
    result, usage = await asyncio.to_thread(deps.fast_llm.route, state["query"])
    route = result["route"]
    systems, unknown = mentioned_systems(state["query"])
    requires_system = route == "incident" or (route == "lookup" and result["kind"] == "monitoring")
    if requires_system and route == "lookup" and len(systems) > 1:
        route = "incident"
        result["reason"] = "여러 시스템의 조회가 필요해 복합 경로 선택"
    model = deps.quality_llm if route == "incident" else deps.fast_llm
    response_model = "source-template" if route == "lookup" else model.name
    event = {"type": "RouteDecided", "route": route, "reason": result["reason"],
             "classifier_model": deps.fast_llm.name, "response_model": response_model}
    update = {"route": route, "needs_analysis": result["needs_analysis"],
              "requested_systems": systems if requires_system else [],
              "routing_reason": result["reason"], "response_model": response_model,
              "report_kind": result["kind"] if route == "lookup" else "incident",
              "cost_usd": _cost(state, usage),
              "events": state.get("events", []) + [event]}
    problem = None
    if requires_system and unknown:
        problem = f"등록되지 않은 시스템: {', '.join(unknown)}. 정확한 시스템명을 포함해 요청을 다시 보내주세요."
    elif requires_system and not systems:
        problem = "조사할 시스템명을 포함해 요청을 다시 보내주세요."
    if problem:
        problem += f" 등록된 시스템: {', '.join(sorted(KNOWN_SYSTEMS))}."
        update.update(final=problem, gate_blocked_reason=problem)
        update["events"].extend([
            {"type": "GateApplied", "blocked_reason": problem, "removed_tasks": 0,
             "removed_analysis_tasks": 0, "rejected_tasks": [], "added_targets": []},
            {"type": "Finalize", "pipeline": "target-clarification"},
        ])
    elif route == "lookup":
        update["tasks"] = [{"agent": "research", "kind": result["kind"], "target": result["target"]}]
    return update


@traceable(name="chat_answer_node")
async def chat_answer_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 5 direct 경로 — 히스토리 기반 응답 (Wave 12 세션 증명 지점).

    토큰 단위 스트리밍 — get_stream_writer()는 /chat/stream처럼 custom 모드로 소비될
    때만 실제로 전송되고, /chat(ainvoke)처럼 스트리밍하지 않는 호출에서는 자동으로
    no-op이 된다."""
    writer = get_stream_writer()
    reply, usage = await deps.fast_llm.chat(
        state["query"], state.get("history", []),
        on_token=lambda piece: writer({"type": "AnswerToken", "delta": piece}))
    return {"final": reply, "last_prompt": usage.get("prompt"),
            "cost_usd": _cost(state, usage),
            "events": state["events"] + [{"type": "Finalize", "pipeline": "chat-direct"}]}


@traceable(name="plan_node")
async def plan_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 4 — Plan/Task 분해. Wave 2의 MCP Prompt가 표준 절차를 깔아준다.

    resource와 마찬가지로 이름을 어느 서버가 들고 있는지 미리 가정하지 않고,
    find_prompt()로 연결된 서버들을 먼저 조회해 찾은 뒤에만 get_prompt를 보낸다."""
    prompt_server = await deps.mcp.find_prompt("incident_analysis_prompt")
    procedure = await deps.mcp.get_prompt(
        "incident_analysis_prompt",
        {"system_name": ", ".join(state["requested_systems"]), "symptom": state["query"]},
        server=prompt_server,
    )
    result, usage = await asyncio.to_thread(deps.fast_llm.plan, state["query"], procedure)
    targets = [t["target"] for t in result["tasks"]
               if isinstance(t, dict) and t.get("agent") == "research" and isinstance(t.get("target"), str)]
    event = {"type": "PlanCreated", "task_count": len(result["tasks"]),
             "targets": targets}
    return {"tasks": result["tasks"],
            "cost_usd": _cost(state, usage), "events": state["events"] + [event]}


@traceable(name="gate_node")
async def gate_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 6 — 조사 범위·작업 종류·중복·분석 필요 여부를 코드로 강제한다."""
    manual_lookup = state["route"] == "lookup" and not state["requested_systems"]
    tasks, rejected, added_targets = validate_tasks(
        [] if manual_lookup else state["tasks"], state["requested_systems"],
        state["route"] == "incident" and state.get("needs_analysis", False))
    scopes = ["__query__"] if manual_lookup else state["requested_systems"] if state["route"] == "incident" else []
    tasks.extend({"id": f"search-{i}", "agent": "research", "kind": "runbook_search",
                  "target": scope, "status": "pending"} for i, scope in enumerate(scopes))
    event = {"type": "GateApplied", "removed_tasks": len(rejected),
             "removed_analysis_tasks": sum(t.get("agent") == "analysis" for t in rejected),
             "rejected_tasks": rejected, "added_targets": added_targets, "blocked_reason": None}
    update = {"tasks": tasks, "gate_removed": len(rejected), "gate_added": len(added_targets),
              "events": state["events"] + [event]}
    return update


@traceable(name="research_node")
async def research_node(state: AgentState, deps: SupervisorDeps) -> dict:
    tasks = [dict(t) for t in state["tasks"]]
    pending = [t for t in tasks if t["agent"] == "research" and t["status"] == "pending"]
    if not pending:
        return {"events": state["events"] + [{"type": "ResearchSkipped", "reason": "추가 조사 없음"}]}
    collected, events = list(state.get("collected", [])), list(state["events"])
    failures = list(state.get("research_failures", []))
    retry_total = state.get("retry_count", 0)
    started = time.perf_counter()

    async def fetch(task, operation, *args):
        nonlocal retry_total
        attempts = 0
        async def invoke():
            nonlocal attempts
            attempts += 1
            async with asyncio.timeout(RESEARCH_TIMEOUT_SECONDS):
                return await operation(*args)
        try:
            data, _ = await with_retry(invoke)
        except Exception as exc:
            # 취소는 전파하고 외부 조회의 실패만 격리한다. 내부 오류 원문은 사용자에게 노출하지 않는다.
            if isinstance(exc, TimeoutError):
                code, reason = "timeout", "응답 시간 초과"
            elif isinstance(exc, OSError):
                code, reason = "connection", "연결 또는 자료 접근 실패"
            elif isinstance(exc, ValueError):
                code, reason = "invalid_response", "응답 형식 오류"
            else:
                code, reason = "tool_error", "조회 도구 실행 실패"
            failure = {"kind": task["kind"], "target": task["target"], "code": code,
                       "reason": reason, "attempts": attempts}
            failures.append(failure)
            task["status"] = "failed"
            events.append({"type": "TaskFailed", "id": task["id"], **failure})
            return None
        else:
            task["status"] = "done"
            return data
        finally:
            task["retries"] = max(0, attempts - 1)
            retry_total += task["retries"]

    async def monitor(task):
        data = await fetch(task, deps.mcp.call_tool, "get_monitoring_status", {"system_name": task["target"]})
        if data is not None:
            collected.append({"source": "monitoring", "target": task["target"], "data": data})
            events.append({"type": "TaskCompleted", "id": task["id"], "source": "monitoring",
                           "target": task["target"], "retries": task["retries"]})

    await asyncio.gather(*(monitor(t) for t in pending if t["kind"] == "monitoring"))
    records = monitoring_records(collected)

    async def search(query):
        result = await deps.mcp.call_tool("search_runbooks", {"query": query, "limit": 3})
        if not isinstance(result, dict) or not isinstance(result.get("matches"), list):
            raise ValueError("Invalid search response")
        if any(not isinstance(m, dict) or not isinstance(m.get("id"), str) for m in result["matches"]):
            raise ValueError("Invalid search match")
        return {"query": query, "matches": [m for m in result["matches"] if m["id"] in KNOWN_RUNBOOKS]}

    wanted = dict.fromkeys(t["target"] for t in pending if t["kind"] == "runbook")
    async def search_task(task):
        alerts = " ".join(records.get(task["target"], {}).get("alerts", []))
        query = alerts if state.get("replan_rounds", 0) and alerts else state["query"] + " " + alerts
        data = await fetch(task, search, query)
        if data is not None:
            collected.append({"source": "search", "target": task["target"], "data": data})
            wanted.update(dict.fromkeys(m["id"] for m in data["matches"]))
            events.append({"type": "ManualsRetrieved", "target": task["target"], "matches": data["matches"]})

    await asyncio.gather(*(search_task(t) for t in pending if t["kind"] == "runbook_search"))
    location = None
    async def read_manual(id):
        nonlocal location
        if location is None:
            location = await deps.mcp.find_resource_template("get_incident_runbook")
        server, template = location
        uri = template.format(incident_type=id)
        body = await deps.mcp.read_resource(uri, server=server)
        if not isinstance(body, str):
            raise ValueError("Invalid resource response")
        title = body.splitlines()[0].lstrip("# ") if body.strip() else id
        return {"source": "runbook", "id": id, "title": title, "uri": uri, "data": body}

    existing = documents(collected)
    for id in wanted:
        if id in existing:
            continue
        task = next((t for t in pending if t["kind"] == "runbook" and t["target"] == id), None)
        if task is None:
            task = {"id": f"resource-{id}", "agent": "research", "kind": "runbook",
                    "target": id, "status": "pending"}
            tasks.append(task)
        data = await fetch(task, read_manual, id)
        if data is not None:
            collected.append(data)
            events.append({"type": "TaskCompleted", "id": task["id"], "source": "runbook",
                           "target": id, "retries": task["retries"]})
    return {"tasks": tasks, "collected": collected, "retry_count": retry_total,
            "research_failures": failures,
            "parallel_elapsed": round(time.perf_counter() - started, 2), "events": events}


@traceable(name="replan_check_node")
async def replan_check_node(state: AgentState, deps: SupervisorDeps) -> dict:
    gaps = assess_evidence(state)
    event = {"type": "ReplanCheck", "round": state.get("replan_rounds", 0),
             "is_sufficient": not gaps, "can_replan": any(g.get("can_retry", True) for g in gaps) and state.get("replan_rounds", 0) < 1,
             "gaps": gaps}
    return {"evidence_gaps": gaps, "events": state["events"] + [event]}


@traceable(name="replan_node")
async def replan_node(state: AgentState, deps: SupervisorDeps) -> dict:
    tasks = [{"id": f"gap-{i}", "agent": "research", "kind": gap["kind"],
              "target": gap["target"], "status": "pending"} for i, gap in enumerate(state["evidence_gaps"]) if gap.get("can_retry", True)]
    event = {"type": "ReplanCreated", "task_count": len(tasks), "targets": [t["target"] for t in tasks]}
    return {"tasks": state["tasks"] + tasks, "replan_rounds": state.get("replan_rounds", 0) + 1,
            "events": state["events"] + [event]}


@traceable(name="analysis_node")
async def analysis_node(state: AgentState, deps: SupervisorDeps) -> dict:
    """Wave 6 보완 — needs_analysis=True가 정당한 경우(모델의 자기모순이 아니라
    실제로 패턴 비교가 필요하다고 판단한 경우)를 위한 실행 경로. gate가 살려둔
    agent=='analysis' task가 있을 때만 수집된 monitoring 결과들을 비교 분석한다."""
    tasks = state["tasks"]
    pending = [t for t in tasks if t["agent"] == "analysis" and t["status"] == "pending"]
    if not pending:
        return {"events": state["events"] +
                [{"type": "AnalysisSkipped", "reason": "analysis task 0개"}]}
    valid = monitoring_records(state.get("collected", []))
    if not valid:
        return {"events": state["events"] + [{"type": "AnalysisSkipped", "reason": "유효한 모니터링 자료 없음"}]}
    summary, usage = await asyncio.to_thread(
        deps.quality_llm.analyze, [{"source": "monitoring", "data": d} for d in valid.values()])
    new_tasks = [dict(t) for t in tasks]
    for t in new_tasks:
        if t["agent"] == "analysis" and t["status"] == "pending":
            t["status"] = "done"
    collected = state.get("collected", []) + [{"source": "analysis", "data": summary}]
    event = {"type": "AnalysisCompleted", "length": len(summary)}
    return {"tasks": new_tasks, "collected": collected,
            "cost_usd": _cost(state, usage), "events": state["events"] + [event]}


@traceable(name="write_node")
async def write_node(state: AgentState, deps: SupervisorDeps) -> dict:
    attempt = state.get("validation_attempts", 0) + 1
    gaps = assess_evidence(state)
    collected = state.get("collected", [])
    if not monitoring_records(collected) and not documents(collected):
        return {"draft": None, "evidence_gaps": gaps,
                "events": state["events"] + [{"type": "DraftSkipped", "reason": "답변에 사용할 자료 없음"}]}
    if state["route"] == "lookup":
        draft = lookup_report(state["report_kind"], collected, gaps)
        usage = {"usd": 0}
    else:
        draft, usage = await deps.quality_llm.write(state["query"], collected, attempt,
                                                   state.get("validation_feedback"), gaps=gaps)
    return {"draft": draft, "evidence_gaps": gaps, "last_prompt": usage.get("prompt"),
            "cost_usd": _cost(state, usage),
            "events": state["events"] + [{"type": "DraftWritten", "attempt": attempt}]}


@traceable(name="validate_node")
async def validate_node(state: AgentState, deps: SupervisorDeps) -> dict:
    attempts = state.get("validation_attempts", 0) + 1
    collected = state.get("collected", [])
    if state.get("draft") is None and not monitoring_records(collected) and not documents(collected):
        return {"validation_status": "unavailable", "validation_attempts": state.get("validation_attempts", 0),
                "validation_feedback": None, "validation_issues": [], "validation_scores": {},
                "events": state["events"] + [{"type": "Validation", "status": "unavailable",
                                              "issues": [], "scores": {}, "will_retry": False}]}
    mode = state.get("report_kind", "incident")
    issues = check_facts(state.get("draft"), collected, state.get("evidence_gaps", []), mode=mode)
    cost = state.get("cost_usd", 0)
    review_valid = True
    review = {}
    method = "source_exact" if state["route"] == "lookup" else "facts_and_llm"
    if state["route"] == "lookup":
        if state.get("draft") != lookup_report(mode, collected, state.get("evidence_gaps", [])):
            issues.append("조회 답변이 원본 관측값·매뉴얼 절차와 다릅니다.")
    elif not issues:
        reviewer = deps.review_llm or deps.quality_llm
        review, usage = await asyncio.to_thread(
            reviewer.validate, state["query"], collected, state["draft"], state.get("evidence_gaps", []))
        cost = _cost(state, usage)
        try:
            issues.extend(checked_review_issues(review, collected, state["draft"]))
        except ValueError:
            review_valid = False
            issues = ["검수 의견의 형식 또는 문장·근거 참조를 확인하지 못했습니다. 분석·조치를 공개하지 않습니다."]
    status = "failed" if issues else "partial" if state.get("evidence_gaps") else "passed"
    will_retry = bool(issues) and review_valid and attempts < 2 and state["route"] != "lookup"
    event = {"type": "Validation", "attempt": attempts, "status": status, "scores": {},
             "issues": issues, "will_retry": will_retry, "method": method}
    return {"validation_attempts": attempts, "validation_status": status, "validation_method": method, "validation_issues": issues,
            "validation_review": review,
            "validation_scores": {}, "validation_feedback": "\n".join(issues) if will_retry else None,
            "cost_usd": cost, "events": state["events"] + [event]}


@traceable(name="finalize_node")
async def finalize_node(state: AgentState, deps: SupervisorDeps) -> dict:
    report = report_sections(state.get("draft"), state.get("collected", []), state.get("evidence_gaps", []),
                          state["validation_status"], state.get("validation_issues", []))
    reply = render_report(report)
    get_stream_writer()({"type": "AnswerToken", "delta": reply})
    return {"final": reply, "sources": report["sources"], "report": report,
            "events": state["events"] + [{"type": "Finalize", "pipeline": state["route"],
                                          "validation_status": state["validation_status"]}]}


def _after_route(state: AgentState) -> str:
    if state.get("final"):
        return END
    return {"chat": "chat_answer", "lookup": "gate", "incident": "plan"}[state["route"]]


def _after_gate(state: AgentState) -> str:
    return END if state.get("final") else "research"


def _after_research(state: AgentState) -> str:
    return "write" if state["route"] == "lookup" else "replan_check"


def _after_replan_check(state: AgentState) -> str:
    last = state["events"][-1]
    return "replan" if last["can_replan"] else "analysis"


def _after_validate(state: AgentState) -> str:
    return "write" if state.get("validation_feedback") and state["validation_attempts"] < 2 else "finalize"


def build_graph(deps: SupervisorDeps):
    g = StateGraph(AgentState)
    node = lambda fn: partial(fn, deps=deps)   # Wave 13 — 명시적 의존성 주입
    g.add_node("route", node(route_node))
    g.add_node("chat_answer", node(chat_answer_node))
    g.add_node("plan", node(plan_node))
    g.add_node("gate", node(gate_node))
    g.add_node("research", node(research_node))
    g.add_node("replan_check", node(replan_check_node))
    g.add_node("replan", node(replan_node))
    g.add_node("analysis", node(analysis_node))
    g.add_node("write", node(write_node))
    g.add_node("validate", node(validate_node))
    g.add_node("finalize", node(finalize_node))

    g.set_entry_point("route")
    g.add_conditional_edges("route", _after_route,
                            {"chat_answer": "chat_answer", "plan": "plan", "gate": "gate", END: END})
    g.add_edge("chat_answer", END)
    g.add_edge("plan", "gate")
    g.add_conditional_edges("gate", _after_gate, {"research": "research", END: END})
    g.add_conditional_edges("research", _after_research,
                            {"write": "write", "replan_check": "replan_check"})
    g.add_conditional_edges("replan_check", _after_replan_check,
                            {"analysis": "analysis", "replan": "replan"})
    g.add_edge("replan", "research")
    g.add_edge("analysis", "write")
    g.add_edge("write", "validate")
    g.add_conditional_edges("validate", _after_validate,
                            {"finalize": "finalize", "write": "write"})
    g.add_edge("finalize", END)
    return g.compile()
