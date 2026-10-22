"""실제 Ollama + MCP 평가. 실행: python evaluation/run.py <결과.json>"""
import asyncio
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["LANGSMITH_TRACING"] = "false"

from agent.deps import SupervisorDeps
from agent.graph import build_graph
from agent.mcp_client import MCPClient
from agent.ollama_llm import OllamaFastLLM, OllamaQualityLLM
from agent.reporting import check_facts
from agent.session_store import SqliteSessionStore

CASES_PATH = Path(__file__).with_name("cases.json")


def assess(case, result):
    report = result.get("report") or {}
    observed = {o["system_name"] for o in report.get("observations", [])}
    found = {d["id"] for d in result.get("sources", [])}
    expected = set(case["manuals"])
    facts = check_facts(result["draft"], result.get("collected", []),
                        result.get("evidence_gaps", []), mode=result.get("report_kind", "incident")) if result.get("draft") else []
    return {
        "route_ok": result.get("route") in case["routes"],
        "scope_ok": observed == set(case["systems"]),
        "blocking_ok": bool(result.get("gate_blocked_reason")) == case.get("blocked", False),
        "retrieved": sorted(found),
        "relevant_retrieved": len(found & expected),
        "irrelevant_retrieved": len(found - expected),
        "missing_manuals": sorted(expected - found),
        "draft_fact_issues": facts,
        # 자체 검증 통과는 별도 기록하며, 이것만으로 정답이라고 판정하지 않는다.
        "self_validation": result.get("validation_status"),
    }


async def main(output, baseline, group=None, cases_path=CASES_PATH):
    cases = json.loads(cases_path.read_text())
    assert cases and len({c["id"] for c in cases}) == len(cases)
    if cases_path.resolve() == CASES_PATH.resolve():
        assert len(cases) == 15
    if group:
        cases = [case for case in cases if case["id"].startswith(group + "-")]
    fixtures = json.loads((ROOT / "data/monitoring.json").read_text())
    fast = OllamaFastLLM("qwen3:0.6b" if baseline else "qwen3.5:2b", 0.00015,
                        think=None if baseline else False)
    quality = OllamaQualityLLM("qwen3:8b", 0.005)
    tags = httpx.get("http://127.0.0.1:11434/api/tags", timeout=5).json()
    for model in (fast, quality):
        print(f"Loading {model.model}", flush=True)
        response = httpx.post("http://127.0.0.1:11434/api/generate",
                              json={"model": model.model, "keep_alive": "30m"},
                              timeout=300)
        response.raise_for_status()
    source_hashes = {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in [ROOT / "runbook_search.py", *sorted((ROOT / "agent").glob("*.py"))]
    }
    report = {"started_at": datetime.now(timezone.utc).isoformat(),
              "case_group": group or "all",
              "cases_file": cases_path.name,
              "cases_sha256": hashlib.sha256(cases_path.read_bytes()).hexdigest(),
              "source_sha256": source_hashes, "models": tags["models"],
              "fast_model": fast.name, "quality_model": quality.name,
              "fast_think": fast.think, "quality_think": quality.think,
              "review_model": fast.name, "review_think": fast.review_think,
              "conditions": "실제 MCP 호출 후 monitoring.json 고정값으로 대체; 최초 장애 연출 끔; 두 모델 사전 로드; 질문마다 빈 이력; tracing 끔",
              "cases": []}
    output.parent.mkdir(parents=True, exist_ok=True)
    mcp = MCPClient(flaky_first_tool_call=False)
    original_call = mcp.call_tool

    async def fixed_monitoring(name, arguments, server="ops"):
        data = await original_call(name, arguments, server=server)
        if name == "get_monitoring_status" and arguments["system_name"] in fixtures:
            system = arguments["system_name"]
            fixture = fixtures[system]
            data = {"system_name": system, "status": fixture["status"],
                    "cpu_percent": fixture["cpu_base"], "memory_percent": fixture["memory_base"],
                    "alerts": fixture["alerts"], "last_checked": "2026-10-20T12:00:00"}
        return data

    with tempfile.TemporaryDirectory() as directory:
        store = SqliteSessionStore(Path(directory) / "session.db")
        try:
            await mcp.start()
            graph = build_graph(SupervisorDeps(fast, quality, mcp, store, "v2", review_llm=fast))
            with patch.object(mcp, "call_tool", side_effect=fixed_monitoring):
                for case in cases:
                    print(f"START {case['id']}: {case['query']}", flush=True)
                    result = {"query": case["query"], "thread_id": case["id"],
                              "history": [], "events": [], "cost_usd": 0.0}
                    started = previous = time.perf_counter()
                    stages = []
                    error = None
                    try:
                        async for update in graph.astream(result, stream_mode="updates"):
                            now = time.perf_counter()
                            for name, delta in update.items():
                                result.update(delta)
                                stages.append({"node": name, "seconds": round(now - previous, 3)})
                                print(f"  {name}: {now - previous:.2f}s", flush=True)
                            previous = now
                    except Exception as exc:
                        error = f"{type(exc).__name__}: {exc}"
                    row = {"id": case["id"], "seconds": round(time.perf_counter() - started, 3),
                           "stages": stages, "error": error, "checks": assess(case, result),
                           "result": result}
                    report["cases"].append(row)
                    output.write_text(json.dumps(report, ensure_ascii=False, indent=2))
                    print(f"DONE {case['id']} {row['seconds']}s {row['checks']}", flush=True)
        finally:
            await mcp.stop()
            store.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--baseline", action="store_true", help="이전 0.6B 모델·기본 추론 설정")
    parser.add_argument("--group", choices=["chat", "lookup", "manual", "incident", "invalid"], help="고정 질문 중 한 유형만 재평가")
    parser.add_argument("--cases", type=Path, default=CASES_PATH, help="고정 기본 질문 외 별도 평가 질문 파일")
    args = parser.parse_args()
    destination = args.output.resolve()
    if destination.exists():
        raise SystemExit(f"기존 평가 결과를 덮어쓰지 않습니다: {destination}")
    asyncio.run(main(destination, args.baseline, args.group, args.cases))
