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
    tool loop is tested with no API key and no network (`api/test_chat.py`).
  * `run_query` is injected, so tests can assert exactly what was executed.
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


def main(argv: list[str]):
    if not argv:
        print(__doc__)
        print("usage: python api/chat.py \"how salty is the Bay of Bengal?\"")
        return 0
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
            or Path.home().joinpath(".config", "anthropic").exists()):
        print("No Anthropic credentials found.\n"
              "  export ANTHROPIC_API_KEY=...   (or run `ant auth login`)\n"
              "The query layer and the tool loop are testable without one:\n"
              "  .venv/bin/python api/test_catalog.py\n"
              "  .venv/bin/python api/test_chat.py")
        return 2
    print(ask(" ".join(argv)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
