"""저장된 증거로 실제 검수 모델의 수치 의미 대조를 확인한다. 로컬 Ollama 필요."""
import argparse
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ["LANGCHAIN_TRACING_V2"] = "false"
os.environ["LANGSMITH_TRACING"] = "false"

from agent.ollama_llm import OllamaFastLLM
from agent.reporting import check_facts, checked_review_issues, review_items


def main(source, output, model_name="qwen3.5:2b"):
    if output.exists():
        raise SystemExit(f"기존 결과를 덮어쓰지 않습니다: {output}")
    result = next(c["result"] for c in json.loads(source.read_text())["cases"] if c["id"] == "incident-1")
    collected = [c for c in result["collected"] if c["source"] != "analysis"]
    query = "auth-service의 현재 CPU·메모리 수치를 설명하고, 매뉴얼의 커넥션 풀 기준값과 구분해줘."
    draft = {"observations": result["draft"]["observations"], "actions": [], "limitations": [],
             "analysis": "auth-service의 현재 CPU는 78%, 메모리는 61%입니다. 매뉴얼의 90%는 커넥션 풀 사용률에 대한 조치 기준입니다."}
    model = OllamaFastLLM(model_name, 0.00015, think=False)
    rows = []
    for name, analysis, expected_rejection in [
        ("correct_repeat", draft["analysis"], False),
        ("swapped_metrics", "auth-service의 현재 CPU는 61%, 메모리는 78%입니다.", True),
        ("threshold_as_measurement", "auth-service의 현재 CPU는 90%, 메모리는 61%입니다.", True),
        ("unsupported_cause", "auth-service의 응답 지연 원인은 배포 오류로 확정되었습니다.", True),
        ("condition_preserved", draft["analysis"], False),
        ("condition_removed", draft["analysis"], True),
        ("missing_data_as_normal", "auth-service의 디스크에는 문제가 없습니다.", True),
        ("plausible_hypothesis", "외부 호출 지연 가능성이 있으나 구체적 원인은 로그 확인이 필요합니다.", False),
        ("disclosed_limit", "리전별 지연 분포는 제공되지 않아 확인할 수 없습니다.", False),
        ("correct_threshold_comparison", "p95 지연은 2.4s로 기준 1.0s를 초과합니다.", False),
        ("measurement_as_lower_bound", "auth-service의 CPU 사용률은 78% 초과입니다.", True),
    ]:
        candidate = deepcopy(draft)
        candidate["analysis"] = analysis
        if name.startswith("condition_"):
            action = ("5분 내 p95가 임계치(1.0s) 아래로 내려오지 않으면 캐시 계층 또는 읽기 복제본으로 트래픽을 분산한다."
                      if name == "condition_preserved" else "캐시 계층 또는 읽기 복제본으로 트래픽을 분산한다.")
            candidate["actions"] = [{"text": action, "sources": ["high-latency"]}]
        started = time.perf_counter()
        facts = check_facts(candidate, collected)
        review, grounded, localized, issues = None, None, None, []
        if not facts:
            review, _ = model.validate(query, collected, candidate, [])
            try:
                issues = checked_review_issues(review, collected, candidate)
                grounded = True
            except ValueError:
                grounded = False
            claims, _ = review_items(collected, candidate)
            error_texts = [claims[c["claim_id"]]["text"] for c in review.get("checks", [])
                           if c.get("verdict") in ("contradiction", "unsupported") and c.get("claim_id") in claims]
            expected_text = candidate["actions"][0]["text"] if name == "condition_removed" else analysis
            localized = grounded and (error_texts == [expected_text] if expected_rejection else not error_texts)
        rejected = bool(facts or grounded is False or issues)
        rows.append({"id": name, "draft": candidate, "code_issues": facts, "review": review,
                     "review_grounded": grounded, "error_localized": localized, "issues": issues,
                     "expected_rejection": expected_rejection, "rejected": rejected,
                     "decision_stage": "code" if facts else "llm", "pipeline_correct": rejected == expected_rejection,
                     "seconds": round(time.perf_counter() - started, 3)})
        output.write_text(json.dumps({"model": model.name, "source": source.name, "query": query,
                                      "collected": collected, "cases": rows}, ensure_ascii=False, indent=2))
        print(name, "expected:", expected_rejection, "rejected:", rejected, facts or review, flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--model", default="qwen3.5:2b")
    args = parser.parse_args()
    main(args.source, args.output, args.model)
