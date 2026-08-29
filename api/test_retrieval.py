"""Stage 11 tests: the corpus, the embedders, the index, and /ask.

No network and no API key, like every other suite here.  The database calls are
real -- the corpus is generated from Postgres through the read-only role, which
is the only way to check that a glossary sentence carries the same number the
table does.

What is deliberately NOT asserted: whether the embedding is any good in a
semantic sense.  That is measured, not asserted -- `retrieval.evaluate` reports
recall over a fixed question set, and the thresholds below are floors that the
current keyless embedder clears, not aspirations.

    .venv/bin/python api/test_retrieval.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import catalog
import chat
import corpus
import embed
import retrieval

passed = failed = 0


def check(label: str, ok: bool, detail: str = ""):
    global passed, failed
    passed, failed = passed + bool(ok), failed + (not ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail else ''}")


# ---- stand-ins ------------------------------------------------------------

@dataclass
class FakeEmbedding:
    values: list


@dataclass
class FakeEmbedResponse:
    embeddings: list


class FakeGenaiModels:
    """Records the config it was called with, so asymmetry and batching are
    checkable without a key.  Returns unit-ish vectors of the right width."""

    def __init__(self, short_by: int = 0):
        self.calls: list[dict] = []
        self.short_by = short_by

    def embed_content(self, *, model, contents, config):
        self.calls.append({"model": model, "n": len(contents),
                           "task_type": config.task_type,
                           "dim": config.output_dimensionality})
        n = len(contents) - self.short_by
        return FakeEmbedResponse([FakeEmbedding([float(i + 1)] * config.output_dimensionality)
                                  for i in range(n)])


class FakeGenaiClient:
    def __init__(self, **kwargs):
        self.models = FakeGenaiModels(**kwargs)


def scripted(*responses) -> chat.ScriptedTransport:
    return chat.ScriptedTransport(list(responses))


@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class ToolUseBlock:
    name: str
    input: dict
    id: str = "toolu_1"
    type: str = "tool_use"


@dataclass
class FakeResponse:
    content: list
    stop_reason: str
    stop_details: Any = None


def text_response(text: str) -> FakeResponse:
    return FakeResponse([TextBlock(text)], "end_turn")


def tool_response(name: str, params: dict) -> FakeResponse:
    return FakeResponse([ToolUseBlock(name, params)], "tool_use")


# --------------------------------------------------------------------------

def main():
    conn = catalog.connect()
    live = catalog.LiveValues.load()

    # ------------------------------------------------------------- corpus
    print("the corpus is generated, never typed in")
    docs = corpus.build(conn, live)
    by_id = {d.doc_id: d for d in docs}
    counts = corpus.by_kind(docs)

    check("every kind is represented", all(counts[k] for k in corpus.KINDS), str(counts))
    check("doc ids are unique", len({d.doc_id for d in docs}) == len(docs))
    check("every document carries the source that produced it",
          all(d.source for d in docs))
    check("every document has text", all(len(d.text) > 40 for d in docs))

    db_regions = [r["name"] for r in catalog.run_raw(conn, "SELECT name FROM regions")]
    check("one region document per region in the database",
          counts["region"] == len(db_regions), f"{counts['region']} vs {len(db_regions)}")
    check("one query document per catalogue query",
          counts["query"] == len(catalog.QUERIES))
    check("one float document per float",
          counts["float"] == len(live.wmos))

    n_months = catalog.run_raw(conn, """
        SELECT count(*) AS n FROM (
          SELECT pr.region, date_trunc('month', p.juld)
          FROM profiles p JOIN profile_regions pr ON pr.profile_id = p.profile_id
          GROUP BY 1, 2) x""")[0]["n"]
    check("one document per region-month that has profiles",
          counts["region_month"] == n_months, f"{counts['region_month']} vs {n_months}")

    # rule 1: an empty region is a document that says so, not a missing document
    empty = [r["name"] for r in catalog.run_raw(conn, """
        SELECT r.name FROM regions r
        LEFT JOIN profile_regions pr ON pr.region = r.name
        GROUP BY r.name HAVING count(pr.profile_id) = 0""")]
    check("regions with no profiles still get a document", empty and
          all(f"region:{n}" in by_id for n in empty), f"{len(empty)} empty regions")
    check("and that document says there is no data",
          all("No profiles" in by_id[f"region:{n}"].text for n in empty))

    # rule 2: a NULL is absent, never 0.0
    check("a missing measurement renders as 'not available', never 0.00",
          corpus.num(None) == "not available" and corpus.num(0) == "0.00")
    check("no document claims a 0.00 mean where the database has NULL",
          not any("salinity 0.000 PSU" in d.text for d in docs))

    # the glossary numbers are the database's numbers
    modes = {r["data_mode"]: r["profiles"]
             for r in catalog.run_raw(conn, corpus.SQL_DATA_MODES)}
    gloss = by_id["glossary:data_mode"].text
    check("the data-mode glossary carries the real counts",
          all(f"{v:,}" in gloss for v in modes.values()), str(modes))
    check("the QC glossary quotes ingest_run's own flag list",
          catalog.run_raw(conn, "SELECT good_qc_flags g FROM ingest_run")[0]["g"]
          in by_id["glossary:qc_flags"].text)
    check("the dataset document states the real profile count",
          f"{catalog.run_raw(conn, 'SELECT count(*) n FROM profiles')[0]['n']:,}"
          in by_id["dataset"].text)
    check("the dataset document rules out BGC parameters explicitly",
          "no oxygen" in by_id["dataset"].text.lower())

    # region_month documents hand back a usable parameter set
    rm = next(d for d in docs if d.kind == "region_month")
    check("a region-month document's keys are a catalogue parameter set",
          set(rm.keys) == {"region", "start", "end"}
          and rm.keys["region"] in live.regions, str(rm.keys))
    check("those keys are accepted by the query they describe",
          catalog.BY_NAME["region_summary"].validate(dict(rm.keys), live) is not None)

    check("a duplicate doc_id is refused rather than overwriting a vector",
          refuses_duplicate())
    check("an unknown document kind is refused too", refuses_unknown_kind())

    # ---------------------------------------------------------- embedders
    print("\nembedders: unit length, and the same answer in the next process")
    hashing = embed.HashingEmbedder(dim=256)
    texts = [d.embedding_text() for d in docs]
    hashing.fit(texts)
    vectors = hashing.embed_documents(texts[:20])
    norms = np.linalg.norm(vectors, axis=1)
    check("document vectors are L2-normalised", np.allclose(norms, 1.0, atol=1e-5),
          f"min {norms.min():.6f} max {norms.max():.6f}")
    check("a query vector is L2-normalised",
          abs(float(np.linalg.norm(hashing.embed_query("how salty is the sea"))) - 1) < 1e-5)
    check("an empty text does not become NaN",
          not np.isnan(embed.normalise(np.zeros(8, dtype=np.float32))).any())
    check("embedding no documents returns an empty matrix, not an error",
          hashing.embed_documents([]).shape == (0, 256))

    check("the same text gives the same vector twice",
          np.array_equal(hashing.embed_query("Bay of Bengal"),
                         hashing.embed_query("Bay of Bengal")))
    check("and the same vector in a DIFFERENT process (hash() would not)",
          *stable_across_processes())

    check("fitting changes the weights, so IDF is actually applied",
          not np.array_equal(embed.HashingEmbedder(dim=256).embed_query("profiles database"),
                             hashing.embed_query("profiles database")))
    check("an unfitted embedder still works", 
          abs(float(np.linalg.norm(
              embed.HashingEmbedder(dim=64).embed_query("test"))) - 1) < 1e-5)

    print("\nthe Gemini embedder, without a key")
    fake = FakeGenaiClient()
    g = embed.GeminiEmbedder(client=fake, dim=8, batch=4)
    out = g.embed_documents([f"doc {i}" for i in range(10)])
    check("documents are embedded as RETRIEVAL_DOCUMENT",
          all(c["task_type"] == "RETRIEVAL_DOCUMENT" for c in fake.models.calls))
    check("a query is embedded as RETRIEVAL_QUERY -- the two are not the same string",
          embed.GeminiEmbedder(client=(f2 := FakeGenaiClient()), dim=8).embed_query("q")
          is not None and f2.models.calls[0]["task_type"] == "RETRIEVAL_QUERY")
    check("10 documents at batch 4 is 3 calls, not 10",
          len(fake.models.calls) == 3, str([c["n"] for c in fake.models.calls]))
    check("the truncated dimensionality is requested explicitly",
          all(c["dim"] == 8 for c in fake.models.calls))
    check("API vectors are normalised by us, not assumed normalised",
          np.allclose(np.linalg.norm(out, axis=1), 1.0, atol=1e-5))
    check("a short reply is refused, not padded", short_reply_refused())

    print("\npicking an embedder is a credential decision, like the model provider")
    check("hashing is chosen when no key is present",
          isinstance(without_gemini_key(lambda: embed.resolve("auto")),
                     embed.HashingEmbedder))
    check("asking for gemini with no key says how to fix it, both ways",
          gemini_without_key_message())

    # -------------------------------------------------------------- index
    print("\nthe index: exact cosine, and it carries its own embedder")
    index = retrieval.build(docs, embed.HashingEmbedder(dim=256))
    check("one vector per document", index.faiss_index.ntotal == len(docs))
    check("the index is exact, not approximate",
          type(index.faiss_index).__name__ == "IndexFlatIP",
          type(index.faiss_index).__name__)
    hits = index.search("how salty is the Bay of Bengal", k=5)
    check("a search returns k hits in descending score order",
          len(hits) == 5 + (0 if any(h.document.kind == "query" for h in hits[:5]) else 1)
          and all(a.score >= b.score for a, b in zip(hits, hits[1:])
                  if a.rank < b.rank))
    check("scores are cosines, so they sit in [-1, 1]",
          all(-1.0001 <= h.score <= 1.0001 for h in hits))
    check("a hit can be shown: it carries its text and its source SQL",
          all(h.as_dict()["text"] and h.as_dict()["source"] for h in hits))

    only_months = index.search("Bay of Bengal March 2023", k=4, ensure_kinds=())
    floored = index.search("Bay of Bengal March 2023", k=4)
    check("the routing floor adds a catalogue query when top-k found none",
          any(h.document.kind == "query" for h in floored),
          f"{[h.document.kind for h in only_months]} -> "
          f"{[h.document.kind for h in floored]}")
    check("the floor adds documents and never removes one",
          {h.document.doc_id for h in only_months} <= {h.document.doc_id for h in floored})
    check("a floated-in document keeps its real rank, so the audit shows it was floated",
          all(h.rank >= 1 for h in floored))

    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp) / "rag"
        index.save(directory)
        check("the manifest records the embedder, not just the vectors",
              json.loads((directory / retrieval.MANIFEST).read_text())["embedder"]["kind"]
              == "hashing")
        reloaded = retrieval.load(directory)
        check("a reloaded index holds the same documents",
              [d.doc_id for d in reloaded.documents] == [d.doc_id for d in docs])
        before = index.search("which float went deepest?", k=5)
        after = reloaded.search("which float went deepest?", k=5)
        check("and returns the same hits, in the same order",
              [h.document.doc_id for h in before] == [h.document.doc_id for h in after])
        check("because the fitted IDF weights survived the round trip",
              np.allclose(index.embedder.idf, reloaded.embedder.idf))
        check("exists() sees a built index", retrieval.exists(directory))
        (directory / retrieval.MANIFEST).unlink()
        check("and does not see a half-deleted one", not retrieval.exists(directory))

    check("a missing index names the command that builds it", missing_index_message())
    check("'off because you asked' and 'off because there is none' read differently",
          chat.describe_retriever(None, asked_for=False)
          != chat.describe_retriever(None, asked_for=True)
          and "no-rag" in chat.describe_retriever(None, asked_for=False))

    print("\na rejected key degrades the build loudly; it never fails the pipeline")
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "etl"))
    import build_index
    bad_key = RuntimeError("400 INVALID_ARGUMENT API_KEY_INVALID")
    no_quota = RuntimeError("429 RESOURCE_EXHAUSTED")
    unknown = RuntimeError("a segmentation fault in the dark")
    check("a rejected key is diagnosed in one sentence, not a traceback",
          "rejected by the API" in (build_index.diagnose(bad_key) or ""))
    check("so is an exhausted quota", "quota" in (build_index.diagnose(no_quota) or ""))
    check("auto falls back to the keyless embedder rather than failing the build",
          build_index.fallback_reason("auto", bad_key) is not None)
    check("an EXPLICIT --embedder=gemini never falls back -- that would be a lie",
          build_index.fallback_reason("gemini", bad_key) is None)
    check("an unrecognised error is never swallowed by the fallback",
          build_index.fallback_reason("auto", unknown) is None)
    check("the fallback notice says the result is lexical, not semantic",
          "LEXICAL" in (Path(__file__).resolve().parent.parent
                        / "etl" / "build_index.py").read_text())
    check("an embedder that cannot be rebuilt is refused, not guessed",
          cannot_rebuild_embedder())

    # ------------------------------------------------------- measurement
    print("\nretrieval is measured, not asserted")
    # Measured on the dimensionality that SHIPS. The index above is 256-wide to
    # keep the structural checks quick, and that is not the configuration a
    # user gets -- a floor measured on it would be measuring the wrong thing.
    shipped = retrieval.build(docs, embed.HashingEmbedder())
    result = retrieval.evaluate(shipped)
    narrow = retrieval.evaluate(index)
    check("every evaluation target matches a real document",
          result["n"] == len(retrieval.EVALUATION))
    check("a target that matched nothing would fail loudly", unreachable_target_fails())
    check(f"recall@3 >= 0.70  (is {result['recall'][3]:.1%})",
          result["recall"][3] >= 0.70)
    check(f"recall@5 >= 0.80  (is {result['recall'][5]:.1%})",
          result["recall"][5] >= 0.80)
    check(f"MRR >= 0.60       (is {result['mrr']:.3f})", result["mrr"] >= 0.60)
    # The width is a decision, not a default someone copied: at a quarter of it
    # the hashing collisions are measurable, and this is what measures them.
    check(f"{embed.HASHING_DIM} dimensions beats 256, so the width is doing work "
          f"({result['recall'][5]:.1%} vs {narrow['recall'][5]:.1%})",
          result["recall"][5] > narrow["recall"][5])
    check("the measurement ignores the routing floor, which would flatter it",
          "ensure_kinds=()" in Path(__file__).with_name("retrieval.py").read_text())
    misses = [r["question"] for r in result["rows"] if r["rank"] is None]
    check(f"the misses are reported rather than tuned away ({len(misses)} of "
          f"{result['n']})", True, "; ".join(m[:44] for m in misses) or "none")

    # ------------------------------------------------------- the tool loop
    print("\nretrieval reaches the model as notes, and never as an answer")
    retriever = retrieval.Retriever(index, k=4)

    t = scripted(text_response("The Bay of Bengal is fresher."))
    ans = chat.ask("how salty is the Bay of Bengal?", transport=t, live=live, conn=conn,
                   retriever=retriever)
    sent = t.calls[0]
    first = sent["messages"][0]["content"]
    check("the question is sent as blocks: notes, then the question",
          isinstance(first, list) and len(first) == 2
          and first[1]["text"] == "how salty is the Bay of Bengal?")
    check("the notes are labelled as summaries, not results",
          "SUMMARIES of the database, not query results" in first[0]["text"])
    check("the notes name their kind and their similarity",
          "similarity" in first[0]["text"] and "kind:" in first[0]["text"])
    check("the system prompt gains the retrieval rules",
          "RETRIEVED NOTES" in sent["system"][0]["text"])
    check("and tells the model not to answer from them",
          "Do not answer from them" in sent["system"][0]["text"])
    check("the answer carries what was retrieved, with scores",
          len(ans.retrieved) == len(retriever.retrieve("how salty is the Bay of Bengal?"))
          and all("score" in r and "doc_id" in r for r in ans.retrieved))
    check("the printed answer shows the retrieval trail",
          "~ retrieved" in str(ans))

    t2 = scripted(text_response("no retrieval here"))
    chat.ask("plain question", transport=t2, live=live, conn=conn)
    check("without a retriever the request is Stage 7's, unchanged",
          t2.calls[0]["messages"][0]["content"] == "plain question")
    check("and the system prompt has no retrieval section",
          "RETRIEVED NOTES" not in t2.calls[0]["system"][0]["text"])
    check("the cached prefix is byte-stable within a mode",
          stable_prefix(live, conn, retriever))

    class Broken:
        def retrieve(self, question):
            raise RuntimeError("index file is corrupt")

    t3 = scripted(text_response("answered anyway"))
    ans3 = chat.ask("still works?", transport=t3, live=live, conn=conn, retriever=Broken())
    check("a broken index does not take the answer down with it",
          ans3.text == "answered anyway")
    check("and the failure is named in the trail rather than swallowed",
          ans3.retrieved and ans3.retrieved[0]["kind"] == "error"
          and "corrupt" in ans3.retrieved[0]["text"])
    check("a broken index means no notes and no retrieval rules in the prompt",
          "RETRIEVED NOTES" not in t3.calls[0]["system"][0]["text"])

    t4 = scripted(tool_response("region_summary",
                                {"region": "Bay of Bengal", "start": "2023-01-01",
                                 "end": "2023-12-31"}),
                  text_response("done"))
    ans4 = chat.ask("summarise the bay", transport=t4, live=live, conn=conn,
                    retriever=retriever)
    check("retrieval does not disturb the audit trail",
          ans4.audit[0]["query"] == "region_summary" and "row_count" in ans4.audit[0])
    check("a number still has to come from a query, not a note",
          ans4.audit[0]["row_count"] == 1)

    # ---------------------------------------------------------------- /ask
    print("\nPOST /ask")
    from fastapi.testclient import TestClient
    import server
    client = TestClient(server.app, raise_server_exceptions=False)

    ai = client.get("/meta").json()["ai"]
    check("/meta reports whether the AI path is usable at all", "available" in ai)
    # The index is derived data under data/, which a fresh checkout has not
    # built yet. Both states are real and both are asserted -- "unavailable"
    # has to name the command that fixes it, not just be false.
    rs = ai["retrieval"]
    if rs["available"]:
        check("/meta reports the on-disk index it would use",
              rs["documents"] == len(docs), f"{rs['documents']} vs {len(docs)}")
        check("/meta names the embedder, so a stale index is visible",
              "embedder" in rs and "built_at" in rs)
    else:
        check("/meta says there is no index and how to build one",
              "build_index.py" in rs["reason"], rs["reason"])
        check("and the dashboard is told not to promise retrieval",
              rs["available"] is False)

    for bad, why in ((" ", "empty"), ("x" * 3000, "too long")):
        r = client.post("/ask", json={"question": bad})
        check(f"a {why} question is refused as a 400, like any bad parameter",
              r.status_code == 400 and r.json()["error"] == "refused", str(r.status_code))

    real_make = chat.make_transport
    chat.make_transport = lambda provider, model: scripted(
        tool_response("surface_conditions", {"start": "2023-01-01", "end": "2023-12-31"}),
        text_response("The Bay of Bengal is fresher than the Arabian Sea."))
    try:
        r = client.post("/ask", json={"question": "which region is freshest?"})
        body = r.json()
        check("a good question is a 200", r.status_code == 200, str(r.status_code))
        check("the answer comes back", body["answer"].startswith("The Bay of Bengal"))
        check("with the audit trail the CLI prints",
              body["audit"][0]["query"] == "surface_conditions")
        check("and the ROWS behind it, so the chat panel can draw the same chart",
              len(body["audit"][0]["rows"]) == body["audit"][0]["row_count"] > 0,
              str(body["audit"][0]["row_count"]))
        check("rows are numbers, not Decimal strings",
              isinstance(body["audit"][0]["rows"][0]["mean_temp_c"], (int, float)))
        check("the bound parameters include the defaults the catalogue filled",
              "max_dbar" in body["audit"][0]["params"])
        # Whether notes come back depends on there being an index on disk, and
        # a fresh checkout has not built one. Both states are asserted: with an
        # index the notes must be auditable, without one /ask must still answer
        # -- retrieval is an addition to the loop at the HTTP layer too.
        if rs["available"]:
            check("what was retrieved is reported too",
                  len(body["retrieved"]) > 0 and "doc_id" in body["retrieved"][0])
            check("every retrieved note carries the SQL that produced it",
                  all(n["source"] for n in body["retrieved"]))
        else:
            check("with no index, /ask still answers and reports no notes",
                  body["retrieved"] == [] and body["answer"])
            check("and the query behind the answer still ran",
                  body["audit"][0]["row_count"] > 0)
        check("the provider that answered is named", body["provider"] in
              ("anthropic", "gemini"))

        chat.make_transport = lambda provider, model: scripted(
            tool_response("profiles_in_region",
                          {"region": "Atlantis", "start": "2023-01-01",
                           "end": "2023-12-31"}),
            text_response("There is no region called Atlantis."))
        body = client.post("/ask", json={"question": "profiles in Atlantis?"}).json()
        check("a refused query inside the loop is still a 200 answer",
              "Atlantis" in body["answer"])
        check("the refusal is in the trail, with the valid values named",
              "error" in body["audit"][0] and "Valid regions" in body["audit"][0]["error"])
        check("a refused entry carries no rows to draw",
              "rows" not in body["audit"][0])

        chat.make_transport = lambda provider, model: scripted(
            text_response("no retrieval"))
        r = client.post("/ask", json={"question": "hello", "retrieval": False})
        check("retrieval can be switched off per request",
              r.status_code == 200 and r.json()["retrieved"] == [])
    finally:
        chat.make_transport = real_make

    r = client.post("/ask", json={"question": "hi", "provider": "anthropic"})
    check("no credentials for the asked-for provider is a 503, not a 400",
          r.status_code == 503 and r.json()["error"] == "model unavailable",
          str(r.status_code))
    check("and the 503 says which variable to set",
          "ANTHROPIC_API_KEY" in r.json()["detail"])
    check("a model outage and a database outage are different 503 bodies",
          "model unavailable" != "database unavailable")

    # ------------------------------------------------------ UI/catalogue drift
    # The dashboard's chat suggestions are filled from /meta's own examples, so
    # they cannot name a region that does not exist -- but they DO name query
    # names and example keys, and nothing else re-checks those. A suggestion
    # built on a renamed query would render as a dead button, or as the word
    # "undefined" inside a question someone is invited to click.
    print("\nthe dashboard cannot drift from the catalogue")
    displays = (Path(__file__).resolve().parent.parent / "ui" / "src" / "displays.js")
    src = displays.read_text()
    drawn = set(re.findall(r"^  (\w+): \{$", src, re.M))
    check("every catalogue query has a declared display",
          {q.name for q in catalog.QUERIES} <= drawn,
          str(sorted({q.name for q in catalog.QUERIES} - drawn)))
    check("and displays.js declares no query that does not exist",
          drawn <= {q.name for q in catalog.QUERIES},
          str(sorted(drawn - {q.name for q in catalog.QUERIES})))

    suggestions = re.findall(
        r"\{ from: \"(\w+)\", ask: \(([^)]*)\) => [`\"]([^`\"]*)[`\"]", src)
    check("the chat suggestions exist at all", len(suggestions) >= 4, str(len(suggestions)))
    # A pattern that skips what it cannot parse is a check that passes because
    # it looked away: one suggestion written with a plain string instead of a
    # template literal went unread here for a whole stage. Every entry in the
    # list must be accounted for, or this section is not checking the list.
    check("and every entry in the list was parsed, not skipped",
          len(suggestions) == src.count('{ from: "'),
          f"{len(suggestions)} parsed of {src.count('{ from: ')} declared")
    for name, _args, template in suggestions:
        query = catalog.BY_NAME.get(name)
        check(f"suggestion for {name:<20} names a real query", query is not None)
        if query is None:
            continue
        keys = set(re.findall(r"\$\{(?:f\.\w+\()?e\.(\w+)\)?\}", template))
        check(f"  and every value it interpolates is in that query's example",
              keys <= set(query.example), f"{name}: {sorted(keys - set(query.example))}")

    print("\nthe server still hardcodes no data of its own")
    src = Path(__file__).with_name("server.py").read_text()
    for term in ("Arabian Sea", "Bay of Bengal", "2023-01-01", "6903139"):
        check(f"no literal {term!r:<14} in server.py", term not in src)
    for term in ("Arabian Sea", "Bay of Bengal", "2903143"):
        check(f"no literal {term!r:<14} in corpus.py",
              term not in Path(__file__).with_name("corpus.py").read_text())

    conn.close()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


# ---- checks that need to catch something ----------------------------------

def refuses_duplicate() -> bool:
    """Two documents sharing an id would return the wrong text at a plausible
    score, so the corpus builder refuses it rather than indexing it."""
    d = corpus.Document("x", "dataset", "t", "text", "sql")
    try:
        corpus.validate([d, d])
    except ValueError as exc:
        return "duplicate doc_id" in str(exc)
    return False


def refuses_unknown_kind() -> bool:
    try:
        corpus.validate([corpus.Document("x", "invented", "t", "text", "sql")])
    except ValueError as exc:
        return "unknown kind" in str(exc)
    return False


def stable_across_processes() -> tuple[bool, str]:
    """Re-embed in a fresh interpreter with a randomised hash seed."""
    here = Path(__file__).resolve().parent
    code = ("import sys; sys.path.insert(0, %r)\n"
            "import embed, json\n"
            "e = embed.HashingEmbedder(dim=32)\n"
            "print(json.dumps([round(float(x), 6) for x in e.embed_query('Bay of Bengal')]))"
            ) % str(here)
    env = dict(os.environ, PYTHONHASHSEED="random")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         env=env)
    if out.returncode != 0:
        return False, out.stderr.strip().splitlines()[-1][:70]
    other = json.loads(out.stdout)
    mine = [round(float(x), 6) for x in embed.HashingEmbedder(dim=32).embed_query("Bay of Bengal")]
    return other == mine, "identical" if other == mine else "DIFFERED"


def short_reply_refused() -> bool:
    fake = FakeGenaiClient(short_by=1)
    try:
        embed.GeminiEmbedder(client=fake, dim=4, batch=8).embed_documents(["a", "b", "c"])
    except RuntimeError as exc:
        return "refusing to guess" in str(exc)
    return False


def without_gemini_key(fn):
    saved = {k: os.environ.pop(k) for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY")
             if k in os.environ}
    try:
        return fn()
    finally:
        os.environ.update(saved)


def gemini_without_key_message() -> bool:
    def attempt():
        try:
            embed.resolve("gemini")
        except RuntimeError as exc:
            return "GEMINI_API_KEY" in str(exc) and "--embedder=hashing" in str(exc)
        return False
    return without_gemini_key(attempt)


def missing_index_message() -> bool:
    with tempfile.TemporaryDirectory() as tmp:
        try:
            retrieval.load(Path(tmp))
        except FileNotFoundError as exc:
            return "build_index.py" in str(exc)
    return False


def cannot_rebuild_embedder() -> bool:
    try:
        embed.from_state({"kind": "something-else"})
    except ValueError as exc:
        return "cannot rebuild" in str(exc)
    return False


def unreachable_target_fails() -> bool:
    index = retrieval.build([corpus.Document("only", "dataset", "t", "some text here", "sql")],
                            embed.HashingEmbedder(dim=32))
    try:
        retrieval.evaluate(index, cases=(("q", ("no:such:document",)),), ks=(1,))
    except AssertionError as exc:
        return "could never fail" in str(exc)
    return False


def stable_prefix(live, conn, retriever) -> bool:
    """Two different questions must produce byte-identical system prompts, or
    the cache breakpoint buys nothing."""
    a = scripted(text_response("a"))
    b = scripted(text_response("b"))
    chat.ask("question one", transport=a, live=live, conn=conn, retriever=retriever)
    chat.ask("a completely different question two", transport=b, live=live, conn=conn,
             retriever=retriever)
    return a.calls[0]["system"] == b.calls[0]["system"]


if __name__ == "__main__":
    main()
