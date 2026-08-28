"""Stage 11c: the vector index, and the measurement of whether it works.

FAISS over the Stage 11a corpus.  Three decisions worth stating:

**Exact search, not approximate.**  `IndexFlatIP` scans all 131 vectors.  An
IVF or HNSW index would be the textbook choice and would be a lie at this size:
approximate search trades recall for speed, and there is no speed to buy when a
full scan is a fraction of a millisecond.  The index type is a property of the
corpus size, and if the corpus grows past roughly a hundred thousand documents
this decision should be revisited rather than inherited.

**Inner product on normalised vectors, which is cosine.**  `embed.normalise`
guarantees unit length, so a score is a cosine in [-1, 1] and is comparable
across queries.  If that guarantee ever broke the scores would still look
plausible, which is why the suite asserts the norms rather than the scores.

**The index carries its own embedder.**  Searching an index built by embedder A
with a query embedded by B is silent nonsense: every score is meaningless and
nothing raises.  So the embedder's state is written into the manifest and
`load()` reconstructs it.  For the keyless embedder that includes its fitted
IDF weights, so a query after a restart is weighted exactly as the documents
were.

Retrieval is measured, not asserted.  `EVALUATION` is a fixed set of questions
with the documents that should come back, and `evaluate()` reports recall@1,
recall@3, recall@5 and MRR.  That is the number this project did not have.
"""

from __future__ import annotations

import fnmatch
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import corpus
import embed
from corpus import Document

ROOT = Path(__file__).resolve().parent.parent
INDEX_DIR = ROOT / "data" / "rag"
MANIFEST = "manifest.json"
VECTORS = "index.faiss"

DEFAULT_K = 6


@dataclass(frozen=True)
class Hit:
    document: Document
    score: float
    rank: int

    def as_dict(self) -> dict:
        """What a caller may show or audit.  The full text is included: an
        answer that leaned on a retrieved summary must be able to show it."""
        return {"doc_id": self.document.doc_id, "kind": self.document.kind,
                "title": self.document.title, "score": round(self.score, 4),
                "rank": self.rank, "text": self.document.text,
                "source": self.document.source, "keys": self.document.keys}


# --------------------------------------------------------------------------
# the index
# --------------------------------------------------------------------------

@dataclass
class Index:
    documents: list[Document]
    embedder: embed.Embedder
    vectors: np.ndarray
    built_at: str
    faiss_index: object = None

    def __post_init__(self):
        import faiss
        if self.faiss_index is None:
            self.faiss_index = faiss.IndexFlatIP(self.vectors.shape[1])
            self.faiss_index.add(self.vectors)

    @property
    def dim(self) -> int:
        return int(self.vectors.shape[1])

    def search(self, question: str, k: int = DEFAULT_K,
               ensure_kinds: tuple[str, ...] = ("query",)) -> list[Hit]:
        """Top-k by cosine, plus a floor.

        `ensure_kinds` adds the best-scoring document of each named kind if it
        did not already make the cut.  It exists for one kind -- `query` -- and
        for one reason: 92 of the 131 documents are region-months, so a
        question phrased around a place and a date can fill every slot with
        region-months and never surface the catalogue query that answers it.
        The floor is a *routing* aid; it adds documents, never numbers, and the
        added ones carry their real rank and score so the audit shows they were
        floated in rather than won.
        """
        if not self.documents:
            return []
        q = self.embedder.embed_query(question).astype(np.float32)[None, :]
        scores, ids = self.faiss_index.search(q, min(len(self.documents), max(k, 1)))
        hits = [Hit(self.documents[i], float(s), rank)
                for rank, (i, s) in enumerate(zip(ids[0], scores[0]), start=1)
                if i >= 0]

        chosen = {h.document.doc_id for h in hits}
        for kind in ensure_kinds:
            if any(h.document.kind == kind for h in hits):
                continue
            full_scores, full_ids = self.faiss_index.search(q, len(self.documents))
            for rank, (i, s) in enumerate(zip(full_ids[0], full_scores[0]), start=1):
                doc = self.documents[i]
                if doc.kind == kind and doc.doc_id not in chosen:
                    hits.append(Hit(doc, float(s), rank))
                    chosen.add(doc.doc_id)
                    break
        return hits

    # ---- persistence -----------------------------------------------------

    def save(self, directory: Path = INDEX_DIR) -> Path:
        import faiss
        directory.mkdir(parents=True, exist_ok=True)
        faiss.write_index(self.faiss_index, str(directory / VECTORS))
        (directory / MANIFEST).write_text(json.dumps({
            "built_at": self.built_at,
            "embedder": self.embedder.state(),
            "embedder_name": self.embedder.name,
            "dim": self.dim,
            "n_documents": len(self.documents),
            "counts": corpus.by_kind(self.documents),
            "documents": [{"doc_id": d.doc_id, "kind": d.kind, "title": d.title,
                           "text": d.text, "source": d.source, "keys": d.keys}
                          for d in self.documents],
        }, indent=1))
        return directory


def build(documents: list[Document] | None = None,
          embedder: embed.Embedder | None = None) -> Index:
    documents = documents if documents is not None else corpus.build()
    texts = [d.embedding_text() for d in documents]

    if embedder is None:
        embedder = embed.resolve()
    # The keyless embedder needs the corpus before it can weight anything.
    if isinstance(embedder, embed.HashingEmbedder) and embedder.idf is None:
        embedder.fit(texts)

    vectors = embedder.embed_documents(texts)
    if vectors.shape[0] != len(documents):
        raise RuntimeError(f"{vectors.shape[0]} vectors for {len(documents)} documents")
    # A document that embedded to nothing would rank at zero against every
    # question and never be retrieved -- a silent drop, which rule 1 forbids.
    empty = [d.doc_id for d, v in zip(documents, vectors) if not np.any(v)]
    if empty:
        raise RuntimeError(f"{len(empty)} document(s) embedded to a zero vector: "
                           f"{', '.join(empty[:5])}")
    return Index(documents, embedder, vectors,
                 time.strftime("%Y-%m-%dT%H:%M:%S%z"))


def load(directory: Path = INDEX_DIR) -> Index:
    import faiss
    manifest_path = directory / MANIFEST
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"no index at {directory}. Build one:  python etl/build_index.py")
    m = json.loads(manifest_path.read_text())
    documents = [Document(d["doc_id"], d["kind"], d["title"], d["text"],
                          d["source"], d.get("keys", {})) for d in m["documents"]]
    faiss_index = faiss.read_index(str(directory / VECTORS))
    if faiss_index.ntotal != len(documents):
        raise RuntimeError(f"index holds {faiss_index.ntotal} vectors for "
                           f"{len(documents)} documents -- rebuild it")
    vectors = faiss_index.reconstruct_n(0, faiss_index.ntotal)
    return Index(documents, embed.from_state(m["embedder"]), vectors,
                 m["built_at"], faiss_index=faiss_index)


def exists(directory: Path = INDEX_DIR) -> bool:
    return (directory / MANIFEST).exists() and (directory / VECTORS).exists()


# --------------------------------------------------------------------------
# the retriever handed to the tool loop
# --------------------------------------------------------------------------

@dataclass
class Retriever:
    index: Index
    k: int = DEFAULT_K

    @property
    def name(self) -> str:
        return self.index.embedder.name

    def retrieve(self, question: str) -> list[Hit]:
        return self.index.search(question, self.k)


def open_default(k: int = DEFAULT_K) -> Retriever | None:
    """The retriever if there is an index to open, None if there is not.

    None is a real answer here, not a failure: `chat.ask` without a retriever
    is Stage 7's behaviour, which still works and is still tested.  Retrieval
    is an addition to the loop, never a requirement of it.
    """
    if not exists():
        return None
    try:
        return Retriever(load(), k=k)
    except Exception:
        return None


# --------------------------------------------------------------------------
# measurement
# --------------------------------------------------------------------------

# question -> the documents that should come back for it, as doc_id patterns.
# These are the eleven example questions the brief and the README use, plus the
# traps this dataset actually has.  Every pattern is checked to match at least
# one real document before the run starts: a target that matches nothing would
# make a test that cannot fail.
EVALUATION: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("how salty is the Bay of Bengal compared to the Arabian Sea?",
     ("query:compare_regions", "region:Bay of Bengal", "region:Arabian Sea")),
    ("show me salinity profiles near the equator in March 2023",
     ("query:profiles_in_region", "query:nearest_profiles", "region_month:*:2023-03-01")),
    ("what are the nearest ARGO floats to 15N 68E?",
     ("query:nearest_profiles",)),
    ("which float went deepest?",
     ("query:float_inventory", "float:*")),
    ("where did float 6903139 travel?",
     ("float:6903139", "query:float_trajectory")),
    ("why does float 2902203 have fewer profiles than the index promised?",
     ("dropped:*", "query:missing_profiles")),
    ("is this data calibrated, or is it raw real-time?",
     ("glossary:data_mode", "glossary:psal_source", "query:data_provenance")),
    ("what does dbar mean, and is that the same as depth in metres?",
     ("glossary:pressure",)),
    ("do you have oxygen or chlorophyll measurements?",
     ("dataset",)),
    ("how many profiles are in the Red Sea?",
     ("region:Red Sea",)),
    ("plot temperature against depth in the Arabian Sea",
     ("query:depth_profile",)),
    ("which floats are operated by the Indian data centre?",
     ("float:*", "query:float_inventory")),
    ("what happened in the Bay of Bengal in July 2023?",
     ("region_month:Bay of Bengal:2023-07-01", "region:Bay of Bengal")),
    ("were any bad quality-control levels thrown away?",
     ("glossary:qc_flags",)),
    ("which profiles fall outside the study box?",
     ("glossary:study_box",)),
    ("how were the region boundaries decided?",
     ("glossary:regions",)),
    ("how many profiles are in the database in total?",
     ("dataset", "query:region_summary")),
    ("compare the monthly profile count over time",
     ("query:monthly_profile_counts",)),
)


def matches(doc_id: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatch(doc_id, p) for p in patterns)


def evaluate(index: Index, cases=EVALUATION, ks: tuple[int, ...] = (1, 3, 5)) -> dict:
    """Recall@k and MRR over the fixed question set.

    Recall@k here means "at least one acceptable document in the top k", which
    is the property that matters: the context block only has to *contain* the
    right summary for it to help.
    """
    all_ids = {d.doc_id for d in index.documents}
    for question, patterns in cases:
        for p in patterns:
            if not any(fnmatch.fnmatch(i, p) for i in all_ids):
                raise AssertionError(
                    f"evaluation target {p!r} matches no document in the corpus; "
                    f"the case {question[:40]!r} could never fail")

    hits_at = {k: 0 for k in ks}
    reciprocal = 0.0
    rows = []
    for question, patterns in cases:
        # Ranking only -- the ensure_kinds floor is a routing aid, and scoring
        # it would flatter the measurement.
        found = index.search(question, k=max(ks), ensure_kinds=())
        rank = next((i for i, h in enumerate(found, start=1)
                     if matches(h.document.doc_id, patterns)), None)
        for k in ks:
            hits_at[k] += int(rank is not None and rank <= k)
        reciprocal += (1.0 / rank) if rank else 0.0
        rows.append({"question": question, "rank": rank,
                     "top": found[0].document.doc_id if found else None,
                     "score": round(found[0].score, 3) if found else None})

    n = len(cases)
    return {"n": n, "recall": {k: hits_at[k] / n for k in ks},
            "mrr": reciprocal / n, "rows": rows}


def main() -> int:
    which = "auto"
    for arg in sys.argv[1:]:
        if arg.startswith("--embedder="):
            which = arg.split("=", 1)[1]

    index = load() if exists() and which == "auto" else build(embedder=embed.resolve(which))
    print(f"index      {len(index.documents)} documents, {index.dim} dimensions, "
          f"embedder {index.embedder.name}")
    result = evaluate(index)
    print(f"\nretrieval over {result['n']} questions")
    for k, v in result["recall"].items():
        print(f"  recall@{k}            {v:6.1%}")
    print(f"  MRR                 {result['mrr']:6.3f}")
    print("\n  rank  top document                              question")
    for row in result["rows"]:
        print(f"  {str(row['rank'] or '-'):>4}  {str(row['top'])[:40]:<40}  "
              f"{row['question'][:52]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
