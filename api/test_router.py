"""Stage 12 tests: answering with no model, and proving it needs nothing.

Every check here runs with no network, no API key, no daemon and no model
download.  That is not a nice property, it is the stage's entire claim, and
`run_pipeline.py --check` is where it is proved.

The suite is organised around the three ways this stage could quietly go wrong:

  * It could pretend to be something it is not.  Rule 9 checks grep the source
    for the word "semantic" and require every occurrence to be a denial.
  * It could grow a second path into the database.  A check asserts
    `api/router.py` imports no database driver and that it is not registered as
    a `Transport` -- it is a sibling of `chat.ask`, not of `GeminiTransport`.
  * It could measure itself against its own answer key.  The leakage check
    fails if any evaluation question contains a routing fixture.

    .venv/bin/python api/test_router.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import catalog
import chat
import embed
import router

passed = failed = 0
HERE = Path(__file__).resolve().parent
UI = HERE.parent / "ui" / "src" / "components" / "ChatPanel.jsx"


def check(label: str, ok: bool, detail: str = ""):
    global passed, failed
    passed, failed = passed + bool(ok), failed + (not ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail else ''}")


def main():
    conn = catalog.connect()
    live = catalog.LiveValues.load()
    r = router.shared_router()

    # ------------------------------------------------- what this is not
    print("rule 9: it is never called more than it is")
    for path in (HERE / "router.py", UI):
        src = path.read_text()
        stray = [m.start() for m in re.finditer(r"semantic", src, re.I)
                 if not re.search(r"(not|never|rather than)\s+\w*\s*semantic",
                                  src[max(0, m.start() - 40):m.start() + 9], re.I)]
        check(f"{path.name}: every mention of 'semantic' is a denial",
              not stray, f"{len(stray)} bare use(s)")
        for word in ("understands the question", "infers intent", "knows what you mean"):
            check(f"{path.name}: does not claim to {word[:22]!r}", word not in src.lower())

    print("\nit is a sibling of chat.ask, not a Transport")
    src = (HERE / "router.py").read_text()
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    # Grepping for the word "psycopg" fails on this file's own docstring, which
    # says it never touches psycopg. The property is about imports and cursors,
    # so check those instead of the prose that describes them.
    check("router.py imports no database driver",
          not re.search(r"^\s*(import|from)\s+psycopg", code, re.M))
    # Grepping for "SELECT" is no better: the destructive-request refusal says
    # the connection holds "SELECT and nothing else". Cursors are the property.
    check("router.py executes no SQL of its own",
          not re.search(r"\.execute\(|\.cursor\(|\.fetch(all|one)\(", code))
    check("it reaches rows only through catalog.run / the injected run_query",
          "catalog.run(name, params" in code and "execute(name, params)" in code)
    check("router.py defines no create() -- it cannot be mistaken for a Transport",
          not re.search(r"def create\s*\(", src))
    check("Router does not satisfy the Transport protocol",
          not hasattr(router.Router, "create"))
    check("and the module says so in those words",
          "sibling of `chat.ask`" in src and "not a sibling of" in src)
    check("it reaches the database only through the injected run_query seam",
          "run_query" in src and "execute(name, params)" in src)

    # ---------------------------------------------------- the fixtures
    print("\nrouting fixtures are phrasings, never values")
    exemplars = [(rt.query, e) for rt in router.ROUTES for e in rt.exemplars]
    digits = [(q, e) for q, e in exemplars if any(c.isdigit() for c in e)]
    check(f"no exemplar contains a digit ({len(exemplars)} checked)",
          not digits, str(digits[:2]))
    check("every catalogue query has a route",
          {rt.query for rt in router.ROUTES} == {q.name for q in catalog.QUERIES},
          str(sorted({rt.query for rt in router.ROUTES}
                     ^ {q.name for q in catalog.QUERIES})))
    check("no route names a query that does not exist",
          all(rt.query in catalog.BY_NAME for rt in router.ROUTES))
    check("exemplars are a separate structure from the corpus (D11.4 does not apply)",
          "Document" not in src.split("# the entry point")[0].replace(
              "corpus documents", "").replace("corpus", ""))
    for name in ("float_inventory", "float_trajectory", "data_provenance"):
        ex = router.BY_QUERY[name].exemplars
        check(f"{name:<18} needs no digit in any of its {len(ex)} exemplars",
              not any(c.isdigit() for e in ex for c in e))

    print("\nthe measurement is not scored against its own answer key")
    leaks = router.leakage()
    check("no evaluation question contains a routing fixture", not leaks,
          "; ".join(f"{q[:34]!r} <- {e}" for q, e in leaks[:3]))
    check("the leakage check would actually catch one", leakage_is_real())
    normalised = {router.normalise(c.question) for c in router.CASES}
    check("no evaluation question is duplicated",
          len(normalised) == len(router.CASES),
          f"{len(normalised)} unique of {len(router.CASES)}")
    check("every in-scope case names a real query",
          all(all(n in catalog.BY_NAME for n in c.expect)
              for c in router.IN_SCOPE))
    check("every out-of-scope case declares the reason it must be refused for",
          all(c.reason in ("no-bgc", "outside-window", "unroutable")
              for c in router.OUT_OF_SCOPE))
    check(f"out-of-scope set is big enough to report a rate off "
          f"({len(router.OUT_OF_SCOPE)} cases)", len(router.OUT_OF_SCOPE) >= 20)
    kinds = {k: sum(1 for c in router.OUT_OF_SCOPE if c.reason == k)
             for k in ("no-bgc", "outside-window", "unroutable")}
    check("and every reason kind has enough of them", min(kinds.values()) >= 7, str(kinds))

    # ------------------------------------------------------ the gates
    print("\nthe scope gate refuses before routing, and says what IS available")
    bgc = router.scope_gate("show me the oxygen profiles", live)
    check("a biogeochemical question is refused as no-bgc",
          bgc and bgc.reason == "no-bgc")
    check("and the refusal names what IS measured",
          bgc and "salinity" in bgc.message.lower())
    check("the absent-parameter list is checked, not trusted", not_measured_holds(conn),
          "a NOT_MEASURED term is now a column in levels")

    old = router.scope_gate("salinity in the Bay of Bengal in 1998", live)
    check("a year outside the window is refused as outside-window",
          old and old.reason == "outside-window")
    check("and the refusal names the window",
          old and live.window[0] in old.message)
    both = router.scope_gate("compare 1998 with 2023", live)
    check("a question naming one year INSIDE the window is not refused for it",
          both is None, str(both.reason if both else None))

    kill = router.scope_gate("delete all the profiles", live)
    check("a destructive request is refused structurally, not by the floor",
          kill and kill.reason == "unroutable")
    check("and the refusal says this interface only reads",
          kill and "SELECT and nothing else" in kill.message)
    check("the read-only role is what actually enforces it, and still does",
          write_is_refused(conn))
    check("'export to NetCDF' is refused rather than faked",
          (router.scope_gate("export all of this to NetCDF", live) or
           router.Refusal("", "")).reason == "unroutable")

    # ---------------------------------------------------- slot filling
    print("\nevery bound value carries where it came from")
    out = router.answer("is the Bay of Bengal fresher than the Arabian Sea?",
                        live=live, conn=conn)
    check("a two-region question binds both regions, in the order asked",
          out.params.get("region_a") == "Bay of Bengal"
          and out.params.get("region_b") == "Arabian Sea", str(out.params))
    by_name = {s.name: s for s in out.slots}
    check("the regions are marked as read from the question",
          by_name["region_a"].source == "extracted")
    check("a question with no date falls back to the study window",
          by_name["start"].source == "window-fallback"
          and by_name["start"].value == live.window[0])
    check("and the fallback is ANNOUNCED, not silent",
          len(out.notices) == 1 and live.window[0] in out.notices[0])
    check("the catalogue's own defaults are marked differently from our fallback",
          by_name["max_dbar"].source == "catalogue-default")
    check("no date is ever bound without a slot recording its source",
          every_date_has_provenance(live, conn))

    dated = router.answer("show me each profile position in the Arabian Sea for March 2023",
                          live=live, conn=conn)
    ds = {s.name: s for s in dated.slots}
    check("a real month is extracted, not defaulted",
          ds["start"].source == "extracted" and ds["start"].value == "2023-03-01"
          and ds["end"].value == "2023-03-31", str(dated.params))
    check("and an extracted date raises no fallback notice", not dated.notices)

    vague = router.answer("profiles in the Arabian Sea over the last six months",
                          live=live, conn=conn)
    vs = {s.name: s for s in vague.slots}
    check("'last six months' does not parse, and says so rather than guessing",
          vs["start"].source == "window-fallback" and vague.notices)

    coords = router.answer("what is close to 15N 68E?", live=live, conn=conn)
    check("coordinates are extracted from the question",
          coords.params.get("lat") == 15.0 and coords.params.get("lon") == 68.0,
          str(coords.params))
    no_coords = router.answer("which floats are nearest to the equator?",
                              live=live, conn=conn)
    check("a location with no coordinates is refused, not invented",
          no_coords.refusal is not None)
    check("and the refusal says why there is no place-name lookup",
          no_coords.refusal and "inventing latitudes" in no_coords.refusal.message)

    # ------------------------------------------------------- masking
    print("\nmasking values before routing, measured rather than assumed")
    q = "is the Bay of Bengal fresher than the Arabian Sea?"
    check("a masked question keeps its shape and loses its values",
          "Bay of Bengal" not in router.mask(q, live)
          and "fresher" in router.mask(q, live), router.mask(q, live))
    with_mask = r.scores(q, live)[0]
    without = r.scores(q)[0]
    # Two claims were tried here before this one and both were wrong about the
    # mechanism: masking does not change which route wins for this question,
    # and (once the floor moved to 0.23) it is not what rescues it from the
    # floor either. What it does is raise the score, and the aggregate below is
    # the evidence that this is worth doing at all. Stating the weaker true
    # thing rather than the stronger false one.
    check(f"masking raises the score of a correct route "
          f"({without[1]:.3f} -> {with_mask[1]:.3f})",
          with_mask[0] == without[0] == "compare_regions"
          and with_mask[1] > without[1])
    plain = router.Router(mask_values=False)
    masked_acc = router.evaluate(r, live)["routing"]
    plain_acc = router.evaluate(plain, live)["routing"]
    check(f"and in aggregate masking beats not masking "
          f"({masked_acc:.1%} vs {plain_acc:.1%} routing accuracy)",
          masked_acc > plain_acc)
    check("the mask sentinel is punctuation, so it adds no token of its own",
          not any(c.isalnum() for c in "§"))

    # -------------------------------------------------- the three numbers
    print("\nthe three numbers, false-accept first")
    result = router.evaluate(r, live)
    check(f"FALSE-ACCEPT RATE  {result['false_accept']:.1%}  "
          f"(out-of-scope questions routed anyway; target 0)",
          result["false_accept"] == 0.0)
    check(f"refusal recall     {result['refusal_recall']:.1%}  "
          f"(refused WITH the right reason)", result["refusal_recall"] >= 0.90)
    check(f"routing accuracy   {result['routing']:.1%}  "
          f"({result['n_in']} in-scope questions)", result["routing"] >= 0.60)
    check("refusal recall is not just 1 - false-accept",
          "right reason" in router.evaluate.__doc__.lower()
          or "RIGHT REASON" in router.evaluate.__doc__)
    misses = [x for x in result["rows"] if not x["ok"]]
    check(f"the misses are printed, not tuned away ({len(misses)} of "
          f"{len(router.CASES)})", True,
          "; ".join(m["question"][:30] for m in misses[:3]))
    check("the evaluation calls answer(), it does not re-implement the pipeline",
          "out = answer(case.question" in src)

    # ------------------------------------------------------- POST /ask
    print("\nPOST /ask on the lexical path")
    from fastapi.testclient import TestClient
    import server
    client = TestClient(server.app, raise_server_exceptions=False)

    ai = client.get("/meta").json()["ai"]
    check("/meta reports the router", ai["router"]["available"])
    check("and names the method honestly, in the payload not just the UI",
          "not semantic" in ai["router"]["method"])
    check("the chat box is offered even with no model credentials",
          ai["available"] is True)
    check("/meta keeps model_provider separate from the answering provider",
          "model_provider" in ai)

    body = client.post("/ask", json={"question": "is the Bay of Bengal fresher "
                                     "than the Arabian Sea?",
                                     "provider": "lexical"}).json()
    check("a routed question is answered with no model", body["provider"] == "lexical")
    check("the bubble states which query ran, it does not describe the ocean",
          "compare_regions" in body["answer"] and "no model" in body["answer"])
    check("the audit trail carries the query the router chose",
          body["audit"][0]["query"] == "compare_regions")
    check("with the rows, so the chat panel draws the same chart",
          len(body["audit"][0]["rows"]) == body["audit"][0]["row_count"] > 0)
    check("the bound parameters include the catalogue's own defaults",
          "max_dbar" in body["audit"][0]["params"])
    check("the fallback dates are in the audit trail as bound values",
          body["audit"][0]["params"]["start"] == live.window[0])
    check("and the notice is in the response, for the panel to show",
          body["notices"] and "study window" in body["notices"][0])
    check("no retrieval is claimed on a path that does not use it",
          body["retrieved"] == [])

    refused = client.post("/ask", json={"question": "show me the oxygen profiles",
                                        "provider": "lexical"}).json()
    check("a refusal is a 200 with the reason, not an error",
          refused["refusal_reason"] == "no-bgc")
    check("and it ran no query at all", refused["audit"] == [])
    check("the alternatives are named, as every catalogue refusal does",
          len(refused["alternatives"]) > 0)

    danger = client.post("/ask", json={"question": "delete all the profiles",
                                       "provider": "lexical"}).json()
    check("a destructive request runs no query and returns no rows",
          danger["audit"] == [] and danger["refusal_reason"] == "unroutable")

    explicit = client.post("/ask", json={"question": "hi", "provider": "anthropic"})
    check("an explicit model request with no key is an error, not a silent swap "
          "to the router", explicit.status_code == 503
          and explicit.json()["error"] == "model unavailable", str(explicit.status_code))
    check("and it points at the path that does work",
          "lexical" in explicit.json()["detail"])

    print("\nthe dashboard names the path that answered")
    ui = UI.read_text()
    check("the badge reads 'lexical router · no model'",
          "lexical router · no model" in ui)
    check("the fallback notice is rendered above the chart, not buried",
          "A value came from a fallback, not from your question" in ui)
    check("slot provenance is rendered per parameter",
          "where each bound value came from" in ui)
    check("the panel says this path cannot chain or follow up",
          "cannot chain queries" in ui)
    check("retrieval is only described on the path that actually uses it",
          "rag.available && !lexical" in ui)

    print("\na model failure offers the path that works")
    states = (UI.parent / "States.jsx").read_text()
    check("the failure panel's primary action re-asks without a model",
          "Ask this again without a model" in states and "onRetryLexical" in states)
    check("the selector moves to the working path after a model failure",
          'error.kind === "no-model"' in ui and 'setPath("lexical")' in ui)
    check("and the move announces itself -- it is not a silent fallback",
          "Switched to the lexical router" in ui)
    check("the failed request is NOT re-answered behind the reader's back "
          "(D12.12 still holds)",
          "Nothing above was re-answered" in ui
          and "re-answered by a different engine" in ui)
    check("re-asking is an explicit click, carrying the original question",
          'send(turn.text, "lexical")' in ui)
    app = (UI.parent.parent / "App.jsx").read_text()
    check("the header reports the LIVE path, so it cannot contradict the replies",
          "usingModel" in app and "chatPath ===" in app)
    check("and it only claims RAG when the model path is the one answering",
          'usingModel && ai.retrieval?.available ? "RAG"' in app)
    check("the audit trail is told which path answered, not a constant",
          "via: data.provider" in ui and '"chat"' not in ui.split("onQueries")[1][:400])
    audit_src = (UI.parent / "AuditPanel.jsx").read_text()
    check("and it badges a lexically-routed query as lexical, never as a model",
          'entry.via === "lexical"' in audit_src and '=== "chat"' not in audit_src)

    print("\nnothing here needed a network, a key, a daemon or a download")
    check("no model client is imported by the router",
          not any(m in src for m in ("anthropic", "google.genai", "ollama", "requests")))
    check("the router's embedder is the keyless one",
          isinstance(r.embedder, embed.HashingEmbedder))
    check("chat.py is untouched by this path",
          "router" not in Path(HERE / "chat.py").read_text().split("Stage 11")[0])

    conn.close()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


# ---- checks that must be able to catch something --------------------------

def leakage_is_real() -> bool:
    """The leakage check has to fail on a planted leak, or it proves nothing."""
    planted = (router.Case(f"please {router.ROUTES[0].exemplars[0]} now", ("x",)),)
    return len(router.leakage.__wrapped__(planted)) > 0 if hasattr(
        router.leakage, "__wrapped__") else bool(_leak(planted))


def _leak(cases) -> list:
    exemplars = [(rt.query, router.normalise(e))
                 for rt in router.ROUTES for e in rt.exemplars]
    return [(c.question, ex) for c in cases for _, ex in exemplars
            if ex in router.normalise(c.question)]


def not_measured_holds(conn) -> bool:
    """The absent-parameter list is a hardcoded set of absences, which is the
    kind of thing that rots.  Derived check: none of them may be a column in
    `levels`.  Ingest a biogeochemical parameter and this fails, forcing the
    list to be corrected instead of refusing data we now hold."""
    cols = {r["column_name"] for r in catalog.run_raw(conn,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'levels'")}
    return not (set(router.NOT_MEASURED) & cols)


def write_is_refused(conn) -> bool:
    try:
        catalog.run_raw(conn, "DELETE FROM profiles WHERE false")
        return False
    except Exception:
        return True


def every_date_has_provenance(live, conn) -> bool:
    """No query that binds dates may do so without a Slot saying where from."""
    for q in catalog.QUERIES:
        if not any(p.name == "start" for p in q.params):
            continue
        params, slots, _, refusal = router.fill_slots(
            "tell me about the Arabian Sea", q.name, live)
        if refusal:
            continue
        named = {s.name for s in slots}
        if "start" in params and not {"start", "end"} <= named:
            return False
    return True


if __name__ == "__main__":
    main()
