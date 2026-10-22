"""수집 결과의 유효성과 부족한 근거를 판단한다. 추측을 관측값에 섞지 않는다."""
import math


def monitoring_records(collected: list[dict]) -> dict[str, dict]:
    records = {}
    for item in collected:
        if item["source"] != "monitoring":
            continue
        data = item["data"]
        if not isinstance(data, dict) or data.get("status") not in ("healthy", "degraded", "down"):
            continue
        name = data.get("system_name")
        if not isinstance(name, str) or (item.get("target") and name != item["target"]):
            continue
        if all(type(data.get(key)) in (int, float) and math.isfinite(data[key]) and 0 <= data[key] <= 100
               for key in ("cpu_percent", "memory_percent")):
            records[name] = data
    return records


def documents(collected: list[dict]) -> dict[str, dict]:
    return {item["id"]: item for item in collected if item["source"] == "runbook" and item.get("id")
            and isinstance(item.get("data"), str) and item["data"].strip()
            and not item["data"].startswith("# Runbook Not Found")}


def assess_evidence(state: dict) -> list[dict]:
    collected = state.get("collected", [])
    monitoring = monitoring_records(collected)
    docs = documents(collected)
    systems = state.get("requested_systems", [])
    gaps = [{"kind": "monitoring", "target": name, "reason": f"{name}: 유효한 모니터링 수치 미확인"}
            for name in systems if name not in monitoring]
    scopes = systems if state["route"] == "incident" else (["__query__"] if not systems else [])
    searches = {c["target"]: c["data"] for c in collected if c["source"] == "search"}
    missing_docs = set()
    for scope in scopes:
        matches = searches.get(scope, {}).get("matches", [])
        if not matches:
            label = "질문" if scope == "__query__" else scope
            gaps.append({"kind": "runbook_search", "target": scope, "reason": f"{label}: 관련 매뉴얼 검색 근거 부족"})
        for match in matches:
            if match["id"] not in docs and match["id"] not in missing_docs:
                missing_docs.add(match["id"])
                gaps.append({"kind": "runbook", "target": match["id"], "reason": f"{match['id']}: 매뉴얼 원문 미확인"})
    failures = {(f["kind"], f["target"]): f for f in state.get("research_failures", [])}
    for gap in gaps:
        failure = failures.get((gap["kind"], gap["target"]))
        if failure:
            gap["reason"] += f" — {failure['reason']} ({failure['attempts']}회 시도)"
            gap["can_retry"] = False
    return gaps
