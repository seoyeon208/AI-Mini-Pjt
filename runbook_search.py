"""작은 로컬 매뉴얼 모음의 BM25 검색. 문서 변경 후 MCP 서버를 재시작한다."""
from collections import Counter
from functools import lru_cache
import math
from pathlib import Path
import re

RUNBOOK_DIR = Path(__file__).resolve().parent / "data" / "runbooks"
STOP_WORDS = "runbook get_monitoring_status service connection usage 원인 분석 대응 절차 확인 매뉴얼 찾아줘 알려줘 시스템 서비스 현재 사용률"


def _tokens(text: str) -> list[str]:
    text = text.lower()
    for word in STOP_WORDS.split():
        text = text.replace(word, " ")
    terms = re.findall(r"[a-z]+|[가-힣]+", text)
    # 형태소 분석기를 설치하지 않고도 '메모리가'와 '메모리'의 공통 부분을 검색한다.
    return [token for term in terms for token in
            ([term] if term.isascii() else [term[i:i + 2] for i in range(len(term) - 1)])]


@lru_cache(maxsize=1)
def _documents() -> list[dict]:
    documents = []
    for path in sorted(RUNBOOK_DIR.glob("*.md")):
        body = path.read_text()
        title = body.splitlines()[0].lstrip("# ")
        keywords = " ".join(line for line in body.splitlines() if line.startswith("검색어:"))
        counts = Counter(_tokens(f"{title}\n{title}\n{keywords}\n{body}"))
        documents.append({"id": path.stem, "title": title,
                          "uri": f"runbook://incident-response/{path.stem}",
                          "excerpt": body[:400], "counts": counts,
                          "topics": set(_tokens(f"{title}\n{keywords}"))})
    return documents


def search_runbooks(query: str, limit: int = 3) -> dict:
    if not 1 <= limit <= 6:
        raise ValueError("limit must be between 1 and 6")
    documents = _documents()
    if not documents:
        return {"matches": []}
    terms = set(_tokens(query))
    average_length = sum(sum(d["counts"].values()) for d in documents) / len(documents)
    matches = []
    for document in documents:
        # 조치 본문에 우연히 등장하는 단어만으로 다른 장애 유형을 선택하지 않는다.
        if not terms.intersection(document["topics"]):
            continue
        counts = document["counts"]
        length = sum(counts.values())
        score = 0.0
        for term in terms:
            frequency = counts[term]
            if not frequency:
                continue
            document_frequency = sum(term in d["counts"] for d in documents)
            idf = math.log(1 + (len(documents) - document_frequency + 0.5) / (document_frequency + 0.5))
            score += idf * frequency * 2.5 / (frequency + 1.5 * (0.25 + 0.75 * length / average_length))
        if score > 0:
            matches.append({k: v for k, v in document.items() if k not in ("counts", "topics")} | {"score": round(score, 4)})
    matches.sort(key=lambda item: (-item["score"], item["id"]))
    return {"matches": matches[:limit]}
