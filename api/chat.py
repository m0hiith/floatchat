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

    def __str__(self) -> str:
        lines = [self.text, ""]
        for a in self.audit:
            status = f"{a['row_count']} rows" if "row_count" in a else f"REFUSED: {a['error']}"
            lines.append(f"  [{a['query']}] {json.dumps(a['params'], default=str)} -> {status}")
        return "\n".join(lines)


def build_system(live: catalog.LiveValues) -> str:
    with catalog.connect() as conn:
        n = catalog.run_raw(conn, "SELECT (SELECT count(*) FROM profiles) AS p, "
                                  "(SELECT count(*) FROM levels) AS l")[0]
    return SYSTEM_PROMPT.format(
        n_floats=len(live.wmos), n_profiles=f"{n['p']:,}", n_levels=f"{n['l']:,}",
        win_start=live.window[0], win_end=live.window[1],
        regions=", ".join(live.regions))


def ask(question: str,
        transport: Transport | None = None,
        live: catalog.LiveValues | None = None,
        run_query: Callable[[str, dict], dict] | None = None,
        conn=None) -> Answer:
    """One question in, one answer plus its audit trail out."""
    live = live or catalog.LiveValues.load()
    transport = transport or AnthropicTransport()
    own_conn = conn is None and run_query is None
    if own_conn:
        conn = catalog.connect()

    def default_run(name: str, params: dict) -> dict:
        return catalog.run(name, params, live=live, conn=conn)

    execute = run_query or default_run
    tools = catalog.tool_schemas(live)

    # The system prompt and the tool list are byte-stable across questions, so
    # they sit in front of the cache breakpoint and the volatile question after.
    system = [{"type": "text", "text": build_system(live),
               "cache_control": {"type": "ephemeral"}}]
    messages: list[dict] = [{"role": "user", "content": question}]
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
                              refusal=getattr(detail, "explanation", "declined"))

            if response.stop_reason != "tool_use":
                text = "".join(b.text for b in response.content if b.type == "text")
                return Answer(text.strip(), audit, turn, response.stop_reason)

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
                      audit, MAX_TURNS, "max_turns")
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


def make_transport(provider: str, model: str | None) -> Transport:
    """Provider choice is a transport choice and nothing else -- `ask` and the
    catalogue never learn which model answered (D8.1)."""
    if provider == "gemini":
        import gemini
        return gemini.GeminiTransport(model=model or gemini.DEFAULT_MODEL)
    return AnthropicTransport()


def report_provider_error(provider: str, exc: Exception) -> int:
    """A wrong key or a renamed model is a setup problem, not a stack trace.

    Both are things someone hits on a fresh machine, so each gets a one-line
    diagnosis and the command that fixes it -- D7.7's rule, applied to the
    second provider.
    """
    text = str(exc)
    var = "GEMINI_API_KEY" if provider == "gemini" else "ANTHROPIC_API_KEY"
    if "API_KEY_INVALID" in text or "API key not valid" in text or "authentication" in text.lower():
        print(f"{provider}: the key in ${var} was rejected by the API.\n"
              f"  ${var} is set, so this is a wrong or expired key, not a missing one.")
        if provider == "gemini":
            print("  new key: https://aistudio.google.com/apikey\n"
                  f"  then:    export {var}=...")
        return 2
    if "NOT_FOUND" in text or "was not found" in text:
        print(f"{provider}: that model id does not exist for this key.\n"
              "  see what it can reach:  python api/chat.py --models\n"
              '  then:                   python api/chat.py --model=NAME "..."')
        return 2
    if "RESOURCE_EXHAUSTED" in text or "429" in text:
        # "limit: 0" is not a rate limit -- it means this tier cannot call
        # this model at all, which waiting will never fix.
        if "limit: 0" in text:
            print(f"{provider}: this key's tier has NO quota for that model, so "
                  "retrying will not help.\n"
                  "  pick one its tier allows:  python api/chat.py --models\n"
                  '  e.g.                       python api/chat.py --model=gemini-3.5-flash "..."')
        else:
            print(f"{provider}: rate limited. Wait and retry, or use a smaller model "
                  "with --model=.")
        return 2
    if "UNAVAILABLE" in text or "503" in text:
        print(f"{provider}: the model is busy (503). This is transient -- retry, or "
              "pin a steadier one with --model=.")
        return 2
    raise exc


def main(argv: list[str]):
    provider, model = None, None
    while argv and argv[0].startswith("--"):
        flag = argv.pop(0)
        if flag in ("--gemini", "--anthropic"):
            provider = flag[2:]
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

    if not argv:
        print(__doc__)
        print('usage: python api/chat.py [--gemini|--anthropic] [--model=NAME] "how salty '
              'is the Bay of Bengal?"')
        print("       python api/chat.py --models      # what this Gemini key can reach")
        print(f"detected provider: {provider or 'none -- no credentials found'}")
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
        print(ask(" ".join(argv), transport=make_transport(provider, model)))
    except Exception as exc:                      # a bad key or a bad model id
        return report_provider_error(provider, exc)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
