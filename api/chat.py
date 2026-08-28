"""Stage 7: the natural-language layer -- Claude on top of the query catalogue.

The model is given the eleven catalogue queries as tools and nothing else.  It
picks one, fills the parameters, we execute it against the read-only role, and
it writes the answer from the rows that come back.  It cannot write SQL, cannot
reach the database directly, and cannot invent a region or a float that is not
in the tool schema's enum (D6.1, D6.2).

Every turn returns an audit trail: which queries ran, with which parameters,
and how many rows each returned.  A number on screen can always be traced to a
named query someone wrote.

Two seams make this testable:

  * `Transport` -- anything with `.create(**kwargs)`.  `AnthropicTransport` is
    the real SDK; `ScriptedTransport` replays canned responses, so the whole
    tool loop is tested with no API key and no network (`api/test_chat.py`);
    `gemini.GeminiTransport` runs the identical loop on Gemini (Stage 8).
  * `run_query` is injected, so tests can assert exactly what was executed.

The request below is written in Anthropic's shape because that is the shape
this file was built against.  It is a wire format, not a commitment to a
vendor: `api/gemini.py` translates it, and `ask` never learns which model
answered.

Stage 11 adds a third, optional seam: a `retriever`.  When one is supplied,
the question is prefixed with summaries pulled from a FAISS index over this
same database (`api/retrieval.py`), and the model is told plainly what they
are -- orientation, never evidence.  Retrieval changes which tool is chosen
and what parameters go into it; it never becomes a number in the answer, and
the system prompt says so in as many words.  `ask` without a retriever is
exactly Stage 7's behaviour and is still tested that way.

The notes go in the USER turn, not the system prompt, and that is deliberate:
the system prompt and the tool list are the byte-stable cache prefix, and
folding a per-question block into them would invalidate the cache on every
single question.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent))

import catalog
from catalog import QueryError

MODEL = "claude-opus-5"
MAX_TOKENS = 16_000
MAX_TURNS = 8               # a question needing more than this is a bug, not a query

SYSTEM_PROMPT = """\
You are FloatChat, a question-answering interface over a database of ARGO \
ocean float profiles.

WHAT THE DATABASE ACTUALLY CONTAINS -- do not imply anything wider:
  * {n_floats} floats, {n_profiles} profiles, {n_levels} measured levels.
  * Dates {win_start} to {win_end} only. Nothing before or after exists here.
  * The North Indian Ocean. Named regions available: {regions}.
  * Core ARGO measurements only: pressure (dbar), temperature (degrees C), \
practical salinity (PSU). There is no oxygen, chlorophyll, nitrate or any \
other biogeochemical parameter.

HOW TO ANSWER
  * Answer only from rows returned by the tools. Never state a number that did \
not come from a tool result, and never estimate one.
  * If the tools cannot answer the question, say so plainly and say what the \
database does cover. Do not substitute a different question you can answer \
without saying that is what you are doing.
  * If a question reaches outside {win_start}..{win_end}, or names a region or \
float that is not in the enums, say what is available instead.
  * Always give units. Temperature in degrees C, salinity in PSU, pressure in \
decibars (roughly metres of depth).
  * Keep answers short. A sentence or two plus a small table when there are \
numbers to line up.

DATA QUALITY -- mention these when they bear on the answer
  * Each profile has a data_mode: D is delayed-mode (a scientist has \
calibrated it), A is real-time with adjustments applied, R is raw real-time. \
D is the most trustworthy.
  * Levels flagged bad by QC were removed during ingest, so counts here are of \
good data only. Salinity loses far more levels to QC than temperature does.
  * If a profile count looks lower than someone expects, the missing_profiles \
tool says exactly which profiles were refused and why. Use it rather than \
speculating.
"""

# Appended to the system prompt only when a retriever is in play, so the
# cached prefix stays byte-stable within each mode instead of drifting.
RETRIEVAL_PROMPT = """\

RETRIEVED NOTES
  * The user turn may open with notes retrieved from a vector index built over \
this same database. Each note is a summary generated from a SQL query, and it \
names that query.
  * They are there to orient you: which region, which float, which month, \
which of the tools above actually answers this. Use them to CHOOSE a tool and \
to FILL its parameters.
  * Do not answer from them. Every number you state must come from a tool \
result in this conversation, even when a note appears to contain that number \
already. A note summarises the database; a tool result is the database.
  * If the notes have nothing to do with the question, ignore them silently. \
Never tell the user that notes were retrieved.
"""


class Transport(Protocol):
    def create(self, **kwargs) -> Any: ...


@dataclass
class AnthropicTransport:
    """The real SDK. Credentials resolve from the environment or an `ant` profile."""
    client: Any = None

    def __post_init__(self):
        if self.client is None:
            import anthropic
            self.client = anthropic.Anthropic()

    def create(self, **kwargs) -> Any:
        return self.client.messages.create(**kwargs)


@dataclass
class ScriptedTransport:
    """Replays prepared responses in order. Records what it was asked.

    The recorded call is a deep copy: `ask` mutates one `messages` list in
    place, so storing the reference would make every recorded turn show the
    conversation's final state instead of what was actually sent that turn.
    """
    responses: list
    calls: list = field(default_factory=list)

    def create(self, **kwargs) -> Any:
        self.calls.append(copy.deepcopy(kwargs))
        if not self.responses:
            raise AssertionError("ScriptedTransport ran out of responses")
        return self.responses.pop(0)


@dataclass
class Answer:
    text: str
    audit: list[dict]           # every query that ran, in order
    turns: int
    stop_reason: str
    refusal: str | None = None
    # What retrieval put in front of the question, with scores. Empty when no
    # retriever was supplied. Shown for the same reason the audit trail is:
    # if a summary steered the answer, the user gets to see the summary.
    retrieved: list[dict] = field(default_factory=list)

    def __str__(self) -> str:
        lines = [self.text, ""]
        for r in self.retrieved:
            lines.append(f"  ~ retrieved [{r['doc_id']}] {r['score']:.3f}")
        for a in self.audit:
            status = f"{a['row_count']} rows" if "row_count" in a else f"REFUSED: {a['error']}"
            lines.append(f"  [{a['query']}] {json.dumps(a['params'], default=str)} -> {status}")
        return "\n".join(lines)


def context_block(hits: list) -> str:
    """The retrieved notes, as one labelled text block.

    Labelled, numbered and score-carrying on purpose: the model should be able
    to tell these apart from the question, and a reader of the transcript
    should be able to tell them apart from a tool result.
    """
    lines = ["Retrieved notes from the FloatChat index. These are SUMMARIES of the "
             "database, not query results. Use them to pick a tool and its parameters; "
             "run that tool for any number you state.", ""]
    for i, hit in enumerate(hits, start=1):
        d = hit.document
        lines.append(f"[{i}] {d.title}   (kind: {d.kind}, similarity {hit.score:.3f})")
        lines.append(d.text)
        lines.append("")
    return "\n".join(lines).rstrip()


def build_system(live: catalog.LiveValues, with_retrieval: bool = False) -> str:
    with catalog.connect() as conn:
        n = catalog.run_raw(conn, "SELECT (SELECT count(*) FROM profiles) AS p, "
                                  "(SELECT count(*) FROM levels) AS l")[0]
    text = SYSTEM_PROMPT.format(
        n_floats=len(live.wmos), n_profiles=f"{n['p']:,}", n_levels=f"{n['l']:,}",
        win_start=live.window[0], win_end=live.window[1],
        regions=", ".join(live.regions))
    return text + RETRIEVAL_PROMPT if with_retrieval else text


def ask(question: str,
        transport: Transport | None = None,
        live: catalog.LiveValues | None = None,
        run_query: Callable[[str, dict], dict] | None = None,
        conn=None,
        retriever=None) -> Answer:
    """One question in, one answer plus its audit trail out.

    `retriever` is anything with `.retrieve(question) -> [Hit]`.  None means
    Stage 7 behaviour: no notes, no retrieval section in the system prompt.
    """
    live = live or catalog.LiveValues.load()
    transport = transport or AnthropicTransport()
    own_conn = conn is None and run_query is None
    if own_conn:
        conn = catalog.connect()

    def default_run(name: str, params: dict) -> dict:
        return catalog.run(name, params, live=live, conn=conn)

    execute = run_query or default_run
    tools = catalog.tool_schemas(live)

    # Retrieval failing must not take the answer down with it: the loop worked
    # for three stages without an index and still does.  A broken index is
    # reported in the trail rather than raised (rule 1 -- it gets a name).
    hits, retrieved = [], []
    if retriever is not None:
        try:
            hits = list(retriever.retrieve(question))
            retrieved = [h.as_dict() for h in hits]
        except Exception as exc:
            retrieved = [{"doc_id": "retrieval-failed", "kind": "error",
                          "title": "retrieval unavailable", "score": 0.0, "rank": 0,
                          "text": str(exc), "source": "", "keys": {}}]
            hits = []

    # The system prompt and the tool list are byte-stable across questions, so
    # they sit in front of the cache breakpoint and the volatile question after.
    system = [{"type": "text", "text": build_system(live, with_retrieval=bool(hits)),
               "cache_control": {"type": "ephemeral"}}]
    # Notes first, question last: the question stays the most recent thing the
    # model read, and the cached prefix above is untouched either way.
    content: Any = question if not hits else [
        {"type": "text", "text": context_block(hits)},
        {"type": "text", "text": question},
    ]
    messages: list[dict] = [{"role": "user", "content": content}]
    audit: list[dict] = []

    try:
        for turn in range(1, MAX_TURNS + 1):
            response = transport.create(
                model=MODEL,
                max_tokens=MAX_TOKENS,
                system=system,
                tools=tools,
                thinking={"type": "adaptive"},
                messages=messages,
            )

            if response.stop_reason == "refusal":
                detail = getattr(response, "stop_details", None)
                return Answer("", audit, turn, "refusal",
                              refusal=getattr(detail, "explanation", "declined"),
                              retrieved=retrieved)

            if response.stop_reason != "tool_use":
                text = "".join(b.text for b in response.content if b.type == "text")
                return Answer(text.strip(), audit, turn, response.stop_reason,
                              retrieved=retrieved)

            messages.append({"role": "assistant", "content": response.content})

            # Parallel tool calls come back in one assistant message; every
            # result must go back in ONE user message or the model stops
            # making parallel calls.
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                params = dict(block.input)
                try:
                    out = execute(block.name, params)
                    audit.append({"query": block.name, "params": out["params"],
                                  "row_count": out["row_count"]})
                    payload = json.dumps({"row_count": out["row_count"],
                                          "rows": out["rows"]}, default=str)
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": payload})
                except QueryError as exc:
                    # Hand the refusal back so the model can correct itself --
                    # the message always names the valid values (D6.2).
                    audit.append({"query": block.name, "params": params, "error": str(exc)})
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": str(exc), "is_error": True})
            messages.append({"role": "user", "content": results})

        return Answer("I could not answer that within the allowed number of query steps.",
                      audit, MAX_TURNS, "max_turns", retrieved=retrieved)
    finally:
        if own_conn and conn is not None:
            conn.close()


ANTHROPIC_HELP = (
    "  export ANTHROPIC_API_KEY=...   (or run `ant auth login`)\n"
    "  export GEMINI_API_KEY=...      (then: python api/chat.py --gemini \"...\")\n"
)


def have_anthropic() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY")
                or os.environ.get("ANTHROPIC_AUTH_TOKEN")
                or Path.home().joinpath(".config", "anthropic").exists())


def have_gemini() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))


def open_retriever(k: int | None = None):
    """The Stage 11 retriever, or None.

    Imported lazily and failing to None on purpose: a checkout with no index
    built, or without faiss installed, must still be able to ask a question.
    Retrieval is an addition to this loop, never a requirement of it.
    """
    try:
        import retrieval
    except ImportError:
        return None
    return retrieval.open_default(**({"k": k} if k else {}))


def describe_retriever(retriever, asked_for: bool = True) -> str:
    """Off because you said so, and off because there is nothing to open, are
    different states.  Reporting them with one sentence would send someone to
    build an index they already have."""
    if not asked_for:
        return "off -- --no-rag was passed"
    if retriever is None:
        return "off -- no index (build one: python etl/build_index.py)"
    index = retriever.index
    return (f"on -- {len(index.documents)} documents, embedder {index.embedder.name}, "
            f"top {retriever.k}, built {index.built_at}")


def make_transport(provider: str, model: str | None) -> Transport:
    """Provider choice is a transport choice and nothing else -- `ask` and the
    catalogue never learn which model answered (D8.1)."""
    if provider == "gemini":
        import gemini
        return gemini.GeminiTransport(model=model or gemini.DEFAULT_MODEL)
    return AnthropicTransport()


def diagnose_provider_error(provider: str, exc: Exception) -> str | None:
    """A wrong key or a renamed model is a setup problem, not a stack trace.

    Returns the diagnosis and the command that fixes it, or None when this is
    not a failure we recognise -- in which case the caller must not pretend to
    understand it.  Split out from the printing so `api/server.py` can put the
    same sentence in a 503 body that the CLI puts on the terminal; one source
    of truth for what a bad key looks like.
    """
    text = str(exc)
    var = "GEMINI_API_KEY" if provider == "gemini" else "ANTHROPIC_API_KEY"
    if "API_KEY_INVALID" in text or "API key not valid" in text or "authentication" in text.lower():
        fix = ("  new key: https://aistudio.google.com/apikey\n"
               f"  then:    export {var}=..." if provider == "gemini" else
               f"  set a valid ${var}")
        return (f"{provider}: the key in ${var} was rejected by the API.\n"
                f"  ${var} is set, so this is a wrong or expired key, not a missing one.\n"
                + fix)
    if "NOT_FOUND" in text or "was not found" in text:
        return (f"{provider}: that model id does not exist for this key.\n"
                "  see what it can reach:  python api/chat.py --models\n"
                '  then:                   python api/chat.py --model=NAME "..."')
    if "RESOURCE_EXHAUSTED" in text or "429" in text:
        # "limit: 0" is not a rate limit -- it means this tier cannot call
        # this model at all, which waiting will never fix.
        if "limit: 0" in text:
            return (f"{provider}: this key's tier has NO quota for that model, so "
                    "retrying will not help.\n"
                    "  pick one its tier allows:  python api/chat.py --models\n"
                    '  e.g.                       python api/chat.py --model=gemini-3.5-flash "..."')
        return (f"{provider}: rate limited. Wait and retry, or use a smaller model "
                "with --model=.")
    if "UNAVAILABLE" in text or "503" in text:
        return (f"{provider}: the model is busy (503). This is transient -- retry, or "
                "pin a steadier one with --model=.")
    return None


def report_provider_error(provider: str, exc: Exception) -> int:
    diagnosis = diagnose_provider_error(provider, exc)
    if diagnosis is None:
        raise exc
    print(diagnosis)
    return 2


def resolve_provider(requested: str | None = None) -> str | None:
    """Which provider a call would actually use.  None means no credentials.

    `api/server.py` needs this to tell the dashboard whether to offer a chat
    box at all, and it must not re-implement the rule -- the CLI and the HTTP
    layer have to agree about what "configured" means.
    """
    if requested in ("anthropic", "gemini"):
        have = have_anthropic() if requested == "anthropic" else have_gemini()
        return requested if have else None
    return "anthropic" if have_anthropic() else "gemini" if have_gemini() else None


def main(argv: list[str]):
    provider, model, use_rag, rag_k = None, None, True, None
    while argv and argv[0].startswith("--"):
        flag = argv.pop(0)
        if flag in ("--gemini", "--anthropic"):
            provider = flag[2:]
        elif flag == "--no-rag":
            use_rag = False
        elif flag.startswith("--rag-k="):
            rag_k = int(flag.split("=", 1)[1])
        elif flag.startswith("--model="):
            model, provider = flag.split("=", 1)[1], provider or "gemini"
        elif flag == "--models":
            import gemini
            try:
                print("\n".join(gemini.available_models()))
            except Exception as exc:
                return report_provider_error("gemini", exc)
            return 0
        else:
            print(f"unknown flag {flag}")
            return 2

    if provider is None:                      # whichever key is actually present
        provider = "anthropic" if have_anthropic() else "gemini" if have_gemini() else None

    # Opened before the credential check so `chat.py` with no arguments can
    # report the retrieval state on a machine with no key at all.
    retriever = open_retriever(rag_k) if use_rag else None

    if not argv:
        print(__doc__)
        print('usage: python api/chat.py [--gemini|--anthropic] [--model=NAME] [--no-rag] '
              '"how salty is the Bay of Bengal?"')
        print("       python api/chat.py --models      # what this Gemini key can reach")
        print(f"detected provider: {provider or 'none -- no credentials found'}")
        print(f"retrieval        : {describe_retriever(retriever, use_rag)}")
        return 0

    if provider is None or (provider == "anthropic" and not have_anthropic()) \
            or (provider == "gemini" and not have_gemini()):
        print(f"No credentials for provider '{provider or 'any'}'.\n" + ANTHROPIC_HELP +
              "The query layer and the tool loop are testable without either:\n"
              "  .venv/bin/python api/test_catalog.py\n"
              "  .venv/bin/python api/test_chat.py\n"
              "  .venv/bin/python api/test_gemini.py")
        return 2

    try:
        print(ask(" ".join(argv), transport=make_transport(provider, model),
                  retriever=retriever))
    except Exception as exc:                      # a bad key or a bad model id
        return report_provider_error(provider, exc)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
