"""Build the guideline corpus in Postgres from libs/retrieval/corpus/manifest.json (D63).

    uv run python scripts/build_corpus.py            # download (cached), extract, embed, load
    uv run python scripts/build_corpus.py --limit 40 # first 40 pages of each document (quick check)

Raw downloads are cached in .cache/corpus (gitignored). Loading a document replaces its chunks in
one transaction, so the script is safe to re-run. Needs the compose stack (Postgres) running.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import time
import urllib.request
from pathlib import Path

from pypdf import PdfReader
from vitalmesh_retrieval import Chunk, Document, Store
from vitalmesh_retrieval.chunking import chunk_pages
from vitalmesh_retrieval.embed import LocalEmbedder

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "libs" / "retrieval" / "corpus" / "manifest.json"
CACHE = ROOT / ".cache" / "corpus"
DSN = "postgresql://vitalmesh@127.0.0.1:5432/vitalmesh"


def fetch(url: str, dest: Path) -> Path:
    if not dest.exists():
        agent = {"User-Agent": "Mozilla/5.0 (vitalmesh corpus build)"}
        req = urllib.request.Request(url, headers=agent)
        with urllib.request.urlopen(req, timeout=120) as r:  # nosec B310 - https URLs from our own manifest, build time only
            dest.write_bytes(r.read())
    return dest


def main() -> None:
    logging.disable(logging.CRITICAL)  # pypdf warns about fonts on every page
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="only the first N pages of each document")
    args = ap.parse_args()
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    CACHE.mkdir(parents=True, exist_ok=True)
    embedder = LocalEmbedder()
    store = Store(DSN, embedder.dim)
    store.migrate()
    for entry in manifest["documents"]:
        started = time.perf_counter()
        pdf = fetch(entry["source_url"], CACHE / f"{entry['doc_id']}.pdf")
        sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
        reader = PdfReader(str(pdf))
        pages = [(p.extract_text() or "") for p in reader.pages]
        if args.limit:
            pages = pages[: args.limit]
        pieces = chunk_pages(pages, first_page=entry["first_page"])
        vectors = embedder.embed_passages([t for _, t in pieces])
        chunks = [
            Chunk(text=t, embedding=v, locator=loc)
            for (loc, t), v in zip(pieces, vectors, strict=True)
        ]
        doc = Document(
            doc_id=entry["doc_id"],
            title=entry["title"],
            source_url=entry["source_url"],
            licence=entry["licence"],
            publisher=entry["publisher"],
        )
        n = store.add_document(doc, chunks)
        print(
            f"{doc.doc_id}: {len(pages)} pages -> {n} chunks, sha256 {sha[:12]}, "
            f"{time.perf_counter() - started:.0f} s"
        )
    print("documents, chunks:", store.count())
    store.close()


if __name__ == "__main__":
    main()
