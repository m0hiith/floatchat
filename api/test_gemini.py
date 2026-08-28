"""Stage 8 tests: the Gemini adapter, with no API key and no network.

A fake `genai` client stands in for the API, so these assert the translation we
own -- what a Gemini request is built from, what a Gemini response becomes, and
that the Stage 7 loop is byte-for-byte unaware which provider answered.  Real
`google.genai.types` objects are constructed throughout, so a schema Gemini's
own pydantic models would reject fails here rather than in the demo.

The database calls are real: queries run against Postgres through the same
read-only role.

    .venv/bin/python api/test_gemini.py
"""

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from google.genai import types

import catalog
import chat
import gemini

passed = failed = 0


def check(label: str, ok: bool, detail: str = ""):
    global passed, failed
    passed, failed = passed + ok, failed + (not ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail else ''}")


# ---- a stand-in for google.genai.Client ----------------------------------

@dataclass
class FakeModels:
    responses: list
    seen: list = field(default_factory=list)

    def generate_content(self, *, model, contents, config):
        self.seen.append({"model": model, "contents": contents, "config": config})
        if not self.responses:
            raise AssertionError("FakeClient ran out of responses")
        return self.responses.pop(0)


@dataclass
class FakeClient:
    models: FakeModels


@dataclass
class FakeCandidate:
    content: Any
    finish_reason: Any = "STOP"


@dataclass
class FakeRaw:
    candidates: list
    prompt_feedback: Any = None


def raw(*parts, finish="STOP") -> FakeRaw:
    return FakeRaw([FakeCandidate(types.Content(role="model", parts=list(parts)), finish)])


def call_part(name, args, cid="fc_0", sig=b"signature-bytes"):
    return types.Part(function_call=types.FunctionCall(id=cid, name=name, args=args),
                      thought_signature=sig)


def transport_for(*responses) -> gemini.GeminiTransport:
    return gemini.GeminiTransport(model="gemini-test",
                                  client=FakeClient(FakeModels(list(responses))),
                                  types=types)


def main():
    live = catalog.LiveValues.load()
    conn = catalog.connect()
    print(f"live values : {len(live.regions)} regions, {len(live.wmos)} floats, "
          f"window {live.window[0]}..{live.window[1]}\n")

    # -- the catalogue as Gemini function declarations ----------------------
    print("the catalogue becomes Gemini function declarations")
    decls = gemini.to_gemini_tools(catalog.tool_schemas(live), types)[0].function_declarations
    by_name = {d.name: d for d in decls}
    check("one declaration per catalogue query", len(decls) == len(catalog.QUERIES),
          f"{len(decls)} declarations")
    check("names match the catalogue exactly",
          {d.name for d in decls} == {q.name for q in catalog.QUERIES})
    schema = by_name["region_summary"].parameters_json_schema
    check("region enum still comes from the database",
          schema["properties"]["region"]["enum"] == list(live.regions),
          f"{len(live.regions)} regions")
    check("float enum still comes from the database",
          by_name["float_trajectory"].parameters_json_schema["properties"]["wmo"]["enum"]
          == list(live.wmos))
    check("additionalProperties is stripped (Gemini rejects it)",
          all("additionalProperties" not in json.dumps(d.parameters_json_schema)
              for d in decls))
    check("required parameters survive the translation",
          set(schema["required"]) == {"region", "start", "end"}, str(schema["required"]))
    check("descriptions survive, so the model knows what a query is for",
          "profiles" in by_name["region_summary"].description.lower())

    # -- what the transport actually sends ---------------------------------
    print("\nthe request we send Gemini")
    t = transport_for(raw(types.Part(text="Bay of Bengal: 209 profiles.")))
    ans = chat.ask("how much data is there?", transport=t, live=live, conn=conn)
    sent = t.client.models.seen[0]
    cfg = sent["config"]
    check("the transport's own model is used, not the Anthropic id",
          sent["model"] == "gemini-test", sent["model"])
    check("the system prompt becomes system_instruction",
          "FloatChat" in cfg.system_instruction and "928" in cfg.system_instruction)
    check("cache_control is dropped, not passed through",
          "cache_control" not in str(cfg.system_instruction))
    check("max_tokens becomes max_output_tokens",
          cfg.max_output_tokens == chat.MAX_TOKENS, str(cfg.max_output_tokens))
    check("thinking is requested", cfg.thinking_config.include_thoughts is True)
    check("the SDK is forbidden from calling tools for us",
          cfg.automatic_function_calling.disable is True)
    check("the question goes over as a user Content",
          sent["contents"][0].role == "user"
          and sent["contents"][0].parts[0].text == "how much data is there?")
    check("a plain answer runs no query", ans.audit == [] and ans.turns == 1)

    # -- a real tool call, executed against Postgres ------------------------
    print("\na tool call round-trip")
    original = call_part("region_summary",
                         {"region": "Bay of Bengal", "start": "2023-01-01",
                          "end": "2024-12-31"}, "fc_a")
    t = transport_for(
        raw(original),
        raw(types.Part(text="The Bay of Bengal has 209 profiles from 3 floats.")),
    )
    ans = chat.ask("how much data is there for the Bay of Bengal?", transport=t,
                   live=live, conn=conn)
    check("a function_call becomes a tool_use the loop understands",
          [a["query"] for a in ans.audit] == ["region_summary"])
    check("the query really ran against Postgres", ans.audit[0]["row_count"] == 1)
    check("the model's text is returned", ans.text.startswith("The Bay of Bengal"))
    follow = t.client.models.seen[1]["contents"]
    fr = follow[2].parts[0].function_response
    check("the result goes back as a function_response",
          follow[2].role == "user" and fr is not None)
    check("named, because Gemini matches results by name", fr.name == "region_summary")
    check("and identified, because names collide on parallel calls", fr.id == "fc_a")
    check("rows go back as a dict, not a JSON string",
          isinstance(fr.response, dict) and fr.response["rows"][0]["profiles"] == 209,
          f"profiles={fr.response['rows'][0]['profiles']}")

    # -- thought signatures ------------------------------------------------
    print("\nthought signatures survive the round trip")
    echoed = follow[1].parts[0]
    check("the echoed part is the SAME object Gemini sent, not a rebuild",
          echoed is original)
    check("so the opaque signature goes back byte-identical",
          echoed.thought_signature == b"signature-bytes", str(echoed.thought_signature))
    check("a rebuilt part would have lost it",
          types.Part(function_call=types.FunctionCall(
              id="fc_a", name="region_summary", args={})).thought_signature is None)

    print("\nreasoning never leaks into the answer")
    t = transport_for(raw(types.Part(text="Let me think about salinity...", thought=True),
                          types.Part(text="Surface salinity is 32.9 PSU.")))
    ans = chat.ask("how salty is it?", transport=t, live=live, conn=conn)
    check("thought parts are excluded from the answer text",
          ans.text == "Surface salinity is 32.9 PSU.", ans.text)

    # -- parallel calls with the same function name ------------------------
    print("\nparallel calls to the same query")
    t = transport_for(
        raw(call_part("region_summary", {"region": "Arabian Sea", "start": "2023-01-01",
                                         "end": "2024-12-31"}, "fc_1"),
            call_part("region_summary", {"region": "Bay of Bengal", "start": "2023-01-01",
                                         "end": "2024-12-31"}, "fc_2")),
        raw(types.Part(text="Arabian Sea 291, Bay of Bengal 209.")),
    )
    ans = chat.ask("compare the two", transport=t, live=live, conn=conn)
    responses = t.client.models.seen[1]["contents"][2].parts
    check("both queries executed", len(ans.audit) == 2)
    check("both results in ONE user turn", len(responses) == 2,
          f"{len(responses)} function_response parts")
    check("same name, different ids -- the results are not ambiguous",
          {p.function_response.id for p in responses} == {"fc_1", "fc_2"}
          and {p.function_response.name for p in responses} == {"region_summary"})
    check("the two regions are told apart",
          {a["params"]["region"] for a in ans.audit} == {"Arabian Sea", "Bay of Bengal"})

    # -- refusals still reach the model as refusals -------------------------
    print("\na parameter the catalogue refuses")
    t = transport_for(
        raw(call_part("region_summary", {"region": "Atlantic Ocean", "start": "2023-01-01",
                                         "end": "2024-12-31"}, "fc_bad")),
        raw(call_part("region_summary", {"region": "Arabian Sea", "start": "2023-01-01",
                                         "end": "2024-12-31"}, "fc_ok")),
        raw(types.Part(text="No Atlantic data here; the Arabian Sea has 291 profiles.")),
    )
    ans = chat.ask("how much Atlantic data is there?", transport=t, live=live, conn=conn)
    err = t.client.models.seen[1]["contents"][2].parts[0].function_response.response
    check("Gemini cannot reach a region that is not in the database",
          "error" in ans.audit[0] and "row_count" not in ans.audit[0])
    check("the refusal arrives as an error, not as data", set(err) == {"error"},
          str(list(err)))
    check("it names the valid regions so the model can correct itself",
          "Arabian Sea" in err["error"], err["error"][:60])
    check("the corrected call then succeeds", ans.audit[1]["row_count"] == 1)

    print("\nsafety rails hold on this provider too")
    t = transport_for(
        raw(call_part("profiles_in_region",
                      {"region": "Bay of Bengal", "start": "2023-01-01",
                       "end": "2023-01-31", "limit": 99_999_999}, "fc_big")),
        raw(types.Part(text="capped")),
    )
    ans = chat.ask("give me everything", transport=t, live=live, conn=conn)
    check("an over-large limit is refused before it reaches Postgres",
          "error" in ans.audit[0] and "5000" in ans.audit[0]["error"],
          ans.audit[0].get("error", "")[:50])

    t = transport_for(*[raw(call_part("float_inventory", {}, f"fc_{i}"))
                        for i in range(chat.MAX_TURNS + 2)])
    ans = chat.ask("loop forever", transport=t, live=live, conn=conn)
    check("the tool loop is still bounded", ans.stop_reason == "max_turns",
          f"stopped after {ans.turns} turns")

    # -- finish reasons ----------------------------------------------------
    print("\nGemini finish reasons map onto the loop's vocabulary")
    cases = [("STOP", "end_turn"), ("MAX_TOKENS", "max_tokens"),
             ("SAFETY", "refusal"), ("PROHIBITED_CONTENT", "refusal"),
             ("FinishReason.STOP", "end_turn")]
    for finish, expected in cases:
        got = gemini.from_gemini_response(raw(types.Part(text="x"), finish=finish))
        check(f"{finish} -> {expected}", got.stop_reason == expected, got.stop_reason)

    blocked = FakeRaw([], prompt_feedback=type("PF", (), {"block_reason": "SAFETY"})())
    ans = chat.ask("something disallowed", transport=transport_for(blocked),
                   live=live, conn=conn)
    check("a blocked prompt is surfaced as a refusal, not an empty answer",
          ans.stop_reason == "refusal" and "SAFETY" in ans.refusal, ans.refusal)

    # -- the audit trail is unchanged --------------------------------------
    print("\nthe audit trail does not care who answered")
    t = transport_for(
        raw(call_part("depth_profile", {"region": "Arabian Sea", "start": "2023-01-01",
                                        "end": "2023-12-31"}, "fc_d")),
        raw(types.Part(text="28 degC at the surface, 5 degC at 1000 dbar.")),
    )
    ans = chat.ask("what does the Arabian Sea profile look like?", transport=t,
                   live=live, conn=conn)
    check("defaulted parameters are still recorded",
          ans.audit[0]["params"]["bin_dbar"] == 50, str(ans.audit[0]["params"]))
    check("the printed answer still names the query", "[depth_profile]" in str(ans))
    check("Answer is the same type Stage 7 returns", isinstance(ans, chat.Answer))

    conn.close()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
