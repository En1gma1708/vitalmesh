"""Measure retrieval quality on libs/retrieval/corpus/questions.json (D63).

    uv run python scripts/eval_retrieval.py

For every question: gold chunks = chunks containing ALL `must` phrases. Reports, for full-text,
vector and hybrid search, hit@k (a gold chunk in the top k) and MRR (1/rank of the first gold
chunk, over the top 10). A question with no gold chunk is reported and excluded, not silently
ignored. Needs Postgres with the corpus loaded and the local embedding model.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import psycopg
from vitalmesh_retrieval import Hit, Store
from vitalmesh_retrieval.embed import LocalEmbedder

ROOT = Path(__file__).resolve().parent.parent
QUESTIONS = ROOT / "libs" / "retrieval" / "corpus" / "questions.json"
DSN = "postgresql://vitalmesh@127.0.0.1:5432/vitalmesh"
KS = (1, 3, 5)


def gold_ids(conn: psycopg.Connection, must: list[str]) -> set[tuple[str, int]]:
    clause = " AND ".join("text ILIKE %s" for _ in must)
    rows = conn.execute(
        f"SELECT doc_id, ordinal FROM chunks WHERE {clause}",  # nosec B608 - clause built from fixed ILIKE placeholders
        [f"%{p}%" for p in must],
    ).fetchall()
    return {(r[0], r[1]) for r in rows}


def first_rank(hits: list[Hit], gold: set[tuple[str, int]]) -> int | None:
    for i, h in enumerate(hits, start=1):
        if (h.doc_id, h.ordinal) in gold:
            return i
    return None


def main() -> None:
    logging.disable(logging.CRITICAL)
    questions = json.loads(QUESTIONS.read_text(encoding="utf-8"))["questions"]
    embedder = LocalEmbedder()
    store = Store(DSN, embedder.dim)
    modes = ("text", "vector", "hybrid")
    ranks: dict[str, dict[str, int | None]] = {m: {} for m in modes}
    kinds: dict[str, str] = {}
    with psycopg.connect(DSN) as conn:
        for q in questions:
            gold = gold_ids(conn, q["must"])
            if not gold:
                print(f"NO GOLD CHUNK for {q['id']}: {q['must']} (excluded)")
                continue
            kinds[q["id"]] = q["kind"]
            vec = embedder.embed_query(q["question"])
            found = {
                "text": store.search_text(q["question"].rstrip("?"), 10),
                "vector": store.search_vector(vec, 10),
                "hybrid": store.hybrid(q["question"].rstrip("?"), vec, 10),
            }
            for m in modes:
                ranks[m][q["id"]] = first_rank(found[m], gold)
    n = len(kinds)
    print(f"{n} questions with gold chunks")
    for subset in ("all", "lexical", "paraphrase"):
        ids = [i for i, k in kinds.items() if subset == "all" or k == subset]
        if not ids:
            continue
        print(f"-- {subset} ({len(ids)})")
        for m in modes:
            r = [ranks[m][i] for i in ids]
            hits = {k: sum(1 for x in r if x is not None and x <= k) for k in KS}
            mrr = sum(1 / x for x in r if x) / len(r)
            cells = "  ".join(f"hit@{k} {hits[k]}/{len(ids)}" for k in KS)
            print(f"   {m:7} {cells}  MRR {mrr:.2f}")
    misses = [i for i in kinds if ranks["hybrid"][i] is None]
    print("hybrid misses in top 10:", misses or "none")
    store.close()


if __name__ == "__main__":
    main()
