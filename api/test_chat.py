"""Stage 7 tests: the whole tool loop, with no API key and no network.

`ScriptedTransport` stands in for the model, so these tests assert the parts we
actually own -- what we send Claude, what we do with a tool call, what we hand
back when a parameter is refused -- rather than whether Claude is clever.  The
database calls are real: the queries run against Postgres through the same
read-only role the demo uses.

    .venv/bin/python api/test_chat.py
"""

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import catalog
import chat

passed = failed = 0


def check(label: str, ok: bool, detail: str = ""):
    global passed, failed
    passed, failed = passed + ok, failed + (not ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail else ''}")


# ---- stand-ins for the SDK's response objects -----------------------------

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
class StopDetails:
    explanation: str
    category: str = "other"


@dataclass
class FakeResponse:
    content: list
    stop_reason: str
    stop_details: Any = None
    usage: Any = field(default=None)


def text_response(text: str) -> FakeResponse:
    return FakeResponse([TextBlock(text)], "end_turn")


def tool_response(*calls) -> FakeResponse:
    blocks = [ToolUseBlock(name, params, f"toolu_{i}") for i, (name, params) in enumerate(calls)]
    return FakeResponse(blocks, "tool_use")


def main():
    live = catalog.LiveValues.load()
    conn = catalog.connect()
    print(f"live values : {len(live.regions)} regions, {len(live.wmos)} floats, "
          f"window {live.window[0]}..{live.window[1]}\n")

    # -- the happy path ----------------------------------------------------
    print("a question that needs one query")
    t = chat.ScriptedTransport([
        tool_response(("region_summary", {"region": "Bay of Bengal",
                                          "start": "2023-01-01", "end": "2024-12-31"})),
        text_response("The Bay of Bengal has 209 profiles from 3 floats."),
    ])
    ans = chat.ask("how much data is there for the Bay of Bengal?", transport=t,
                   live=live, conn=conn)
    check("returns the model's final text", ans.text.startswith("The Bay of Bengal"))
    check("audit records the query that ran",
          [a["query"] for a in ans.audit] == ["region_summary"], str(ans.audit[0]["params"]))
    check("audit records the row count", ans.audit[0]["row_count"] == 1)
    check("two turns used", ans.turns == 2, f"turns={ans.turns}")

    # -- what we actually send ---------------------------------------------
    print("\nthe request we send Claude")
    req = t.calls[0]
    check("model is claude-opus-5", req["model"] == "claude-opus-5", req["model"])
    check("adaptive thinking", req["thinking"] == {"type": "adaptive"}, str(req["thinking"]))
    check("every tool is strict with no free-form properties",
          all(s["strict"] and s["input_schema"]["additionalProperties"] is False
              for s in req["tools"]))
    check("one tool per catalogue query", len(req["tools"]) == len(catalog.QUERIES))
    check("system prompt is cached (stable prefix)",
          req["system"][0]["cache_control"] == {"type": "ephemeral"})
    check("system prompt states the real scope, not a vague one",
          "2023-01-01" in req["system"][0]["text"] and "928" in req["system"][0]["text"])
    check("system prompt rules out parameters we do not have",
          "biogeochemical" in req["system"][0]["text"])

    # -- tool results are fed back in a usable shape ------------------------
    print("\nwhat we hand back to the model")
    followup = t.calls[1]
    tool_msg = followup["messages"][-1]
    payload = json.loads(tool_msg["content"][0]["content"])
    check("results go back as one user message", tool_msg["role"] == "user")
    check("result is JSON with rows the model can read",
          payload["row_count"] == 1 and payload["rows"][0]["profiles"] == 209,
          f"profiles={payload['rows'][0]['profiles']}")
    check("assistant turn is echoed back verbatim",
          followup["messages"][1]["role"] == "assistant")

    # -- parallel tool calls -----------------------------------------------
    print("\nparallel tool calls")
    t = chat.ScriptedTransport([
        tool_response(("region_summary", {"region": "Arabian Sea",
                                          "start": "2023-01-01", "end": "2024-12-31"}),
                      ("region_summary", {"region": "Bay of Bengal",
                                          "start": "2023-01-01", "end": "2024-12-31"})),
        text_response("Arabian Sea 299, Bay of Bengal 209."),
    ])
    ans = chat.ask("compare the two", transport=t, live=live, conn=conn)
    results = t.calls[1]["messages"][-1]["content"]
    check("both queries executed", len(ans.audit) == 2)
    check("both results in a SINGLE user message", len(results) == 2,
          f"{len(results)} tool_result blocks in one message")
    check("tool_use_ids are matched back",
          {r["tool_use_id"] for r in results} == {"toolu_0", "toolu_1"})

    # -- a refused parameter, and the recovery ------------------------------
    print("\na parameter the catalogue refuses")
    t = chat.ScriptedTransport([
        tool_response(("region_summary", {"region": "Atlantic Ocean",
                                          "start": "2023-01-01", "end": "2024-12-31"})),
        tool_response(("region_summary", {"region": "Arabian Sea",
                                          "start": "2023-01-01", "end": "2024-12-31"})),
        text_response("There is no Atlantic data here; the Arabian Sea has 299 profiles."),
    ])
    ans = chat.ask("how much Atlantic data is there?", transport=t, live=live, conn=conn)
    err_result = t.calls[1]["messages"][-1]["content"][0]
    check("the bad call is recorded as an error, not a row count",
          "error" in ans.audit[0] and "row_count" not in ans.audit[0])
    check("the error goes back flagged is_error", err_result.get("is_error") is True)
    check("the error names the valid regions so the model can correct itself",
          "Arabian Sea" in err_result["content"], err_result["content"][:70])
    check("the model's corrected call then succeeds", ans.audit[1]["row_count"] == 1)

    # -- a question the data cannot answer ---------------------------------
    print("\na question outside the data")
    t = chat.ScriptedTransport([
        text_response("This database has no oxygen measurements -- only pressure, "
                      "temperature and salinity."),
    ])
    ans = chat.ask("what is the dissolved oxygen at 500m?", transport=t, live=live, conn=conn)
    check("no query is run when none applies", ans.audit == [])
    check("one turn, straight to the answer", ans.turns == 1)

    # -- safety rails -------------------------------------------------------
    print("\nsafety rails")
    t = chat.ScriptedTransport([FakeResponse([], "refusal", StopDetails("declined"))])
    ans = chat.ask("something disallowed", transport=t, live=live, conn=conn)
    check("a refusal is surfaced, not treated as an answer",
          ans.stop_reason == "refusal" and ans.refusal == "declined")

    t = chat.ScriptedTransport([tool_response(("float_inventory", {}))
                               for _ in range(chat.MAX_TURNS + 2)])
    ans = chat.ask("loop forever", transport=t, live=live, conn=conn)
    check("the tool loop is bounded", ans.stop_reason == "max_turns",
          f"stopped after {ans.turns} turns")

    calls: list = []
    t = chat.ScriptedTransport([
        tool_response(("profiles_in_region",
                       {"region": "Bay of Bengal", "start": "2023-01-01",
                        "end": "2023-01-31", "limit": 99_999_999})),
        text_response("capped"),
    ])
    ans = chat.ask("give me everything", transport=t, live=live, conn=conn)
    check("an over-large limit is refused before it reaches Postgres",
          "error" in ans.audit[0] and "5000" in ans.audit[0]["error"],
          ans.audit[0].get("error", "")[:60])

    # -- the audit trail is the point --------------------------------------
    print("\nthe audit trail")
    t = chat.ScriptedTransport([
        tool_response(("depth_profile", {"region": "Arabian Sea",
                                         "start": "2023-01-01", "end": "2023-12-31"})),
        text_response("Temperature falls from 28 degC at the surface to 5 degC at 1000 dbar."),
    ])
    ans = chat.ask("what does the Arabian Sea temperature profile look like?",
                   transport=t, live=live, conn=conn)
    check("defaulted parameters are recorded, not just the given ones",
          ans.audit[0]["params"]["bin_dbar"] == 50, str(ans.audit[0]["params"]))
    check("the printed answer shows which query produced it",
          "[depth_profile]" in str(ans))

    conn.close()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
