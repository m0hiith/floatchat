"""Stage 8: the same tool loop, driven by Gemini instead of Claude.

Nothing in Stage 6 or Stage 7 changes.  The catalogue still owns the SQL, the
`floatchat_ro` role still owns the permissions, and `chat.ask()` still owns the
loop and the audit trail.  This module is only an adapter that satisfies the
`Transport` protocol (D7.4): Anthropic-shaped request in, Anthropic-shaped
response out, a Gemini `generate_content` call in the middle.

That is the whole point of having had a seam.  Swapping the model provider
touches one new file and `main()`, and all 56 existing checks still pass
unmodified.

Four things genuinely differ between the two APIs, and each is handled here:

  * **Tool schemas.**  Anthropic takes `strict: true` +
    `additionalProperties: false` and enforces them.  Gemini has no `strict`,
    so the schema is advisory there.  This does not weaken the system: an
    invented parameter is refused by `Query.validate` and a bad region by
    `Param.coerce`, in our process, before Postgres is reached (D6.2).  The
    schema was never what made this safe.
  * **Call identity.**  Anthropic matches a result to a call by
    `tool_use_id`.  Gemini matches by function *name*, which is ambiguous when
    the model calls the same query twice in parallel -- as it does for a
    region comparison.  We send `id` as well as `name` on every
    `FunctionResponse`, and rebuild the id -> name map from the transcript.
  * **Thought signatures.**  Gemini requires the opaque `thought_signature`
    that came with a function call to be sent back on the next turn or the
    call is rejected.  So every block keeps the original `types.Part` it came
    from and we return that exact object, rather than rebuilding one.
  * **Prompt caching.**  `cache_control` has no Gemini equivalent; Gemini
    caches long stable prefixes implicitly.  The breakpoint is dropped, and
    the system prompt is still the stable prefix, so the behaviour is the same
    even though the instruction is not.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")

# Gemini finish reasons that mean "the model declined", not "the model finished".
REFUSALS = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "IMAGE_SAFETY",
            "RECITATION"}


# --------------------------------------------------------------------------
# Anthropic-shaped response objects
#
# `chat.ask` reads `.stop_reason`, iterates `.content` looking at `.type`,
# `.text`, `.name`, `.input`, `.id`, and echoes the block list straight back
# into `messages`.  These satisfy that contract.  `part` is the Gemini part
# the block came from, kept so the echo is byte-identical (thought signatures).
# --------------------------------------------------------------------------

@dataclass
class TextBlock:
    text: str
    part: Any = None
    type: str = "text"


@dataclass
class ThinkingBlock:
    """A part Gemini marked `thought=True`.  Deliberately NOT type 'text', so
    reasoning can never leak into the answer `chat.ask` assembles."""
    text: str
    part: Any = None
    type: str = "thinking"


@dataclass
class ToolUseBlock:
    name: str
    input: dict
    id: str
    part: Any = None
    type: str = "tool_use"


@dataclass
class OpaqueBlock:
    """A part carrying no text and no call -- a bare thought signature, say.
    It is meaningless to us and must still be echoed back verbatim."""
    part: Any = None
    type: str = "opaque"


@dataclass
class StopDetails:
    explanation: str
    category: str = "other"


@dataclass
class GeminiResponse:
    content: list
    stop_reason: str
    stop_details: Any = None
    raw: Any = None
    usage: Any = field(default=None)


# --------------------------------------------------------------------------
# request translation
# --------------------------------------------------------------------------

def clean_schema(schema: dict) -> dict:
    """Strip keys Gemini's function schema does not accept.

    Only `additionalProperties` is removed, and only because Gemini rejects
    it.  Every constraint it drops is re-checked by the catalogue anyway, so
    this loosens what the model is *told*, never what it is *allowed*.
    """
    out = {}
    for k, v in schema.items():
        if k == "additionalProperties":
            continue
        if isinstance(v, dict):
            out[k] = clean_schema(v)
        elif isinstance(v, list):
            out[k] = [clean_schema(i) if isinstance(i, dict) else i for i in v]
        else:
            out[k] = v
    return out


def to_gemini_tools(tools: list[dict], types) -> list:
    """Anthropic tool definitions -> one Gemini Tool holding all declarations."""
    return [types.Tool(function_declarations=[
        types.FunctionDeclaration(
            name=t["name"],
            description=t["description"],
            parameters_json_schema=clean_schema(t["input_schema"]),
        ) for t in tools])]


def call_names(messages: list[dict]) -> dict[str, str]:
    """id -> function name, read back off the transcript.

    Anthropic's `tool_result` carries only the id; Gemini's `FunctionResponse`
    wants the name.  The assistant turns we echoed back hold both.
    """
    names = {}
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            btype = b.get("type") if isinstance(b, dict) else getattr(b, "type", None)
            if btype != "tool_use":
                continue
            bid = b["id"] if isinstance(b, dict) else b.id
            bname = b["name"] if isinstance(b, dict) else b.name
            names[bid] = bname
    return names


def result_payload(block: dict) -> dict:
    """A tool_result's content -> the dict Gemini wants under `response`.

    `chat.ask` JSON-encodes success payloads and passes refusals through as
    plain text with `is_error`.  We decode the one and wrap the other, so the
    error still reaches the model as an error rather than as data.
    """
    content = block.get("content", "")
    if block.get("is_error"):
        return {"error": content}
    try:
        return json.loads(content)
    except (TypeError, ValueError):
        return {"result": content}


def to_gemini_contents(messages: list[dict], types) -> list:
    """The Anthropic message list -> Gemini `Content` objects."""
    names = call_names(messages)
    contents = []
    for m in messages:
        role = "user" if m["role"] == "user" else "model"
        content = m["content"]
        parts = []
        if isinstance(content, str):
            parts.append(types.Part(text=content))
        else:
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    tid = b["tool_use_id"]
                    parts.append(types.Part(function_response=types.FunctionResponse(
                        id=tid, name=names.get(tid, "unknown"),
                        response=result_payload(b))))
                elif getattr(b, "part", None) is not None:
                    parts.append(b.part)            # verbatim: keeps signatures
                elif isinstance(b, dict) and b.get("type") == "text":
                    parts.append(types.Part(text=b["text"]))
                elif getattr(b, "type", None) == "tool_use":
                    parts.append(types.Part(function_call=types.FunctionCall(
                        id=b.id, name=b.name, args=dict(b.input))))
                elif getattr(b, "text", None) is not None:
                    parts.append(types.Part(text=b.text))
        if parts:
            contents.append(types.Content(role=role, parts=parts))
    return contents


# --------------------------------------------------------------------------
# response translation
# --------------------------------------------------------------------------

def from_gemini_response(response: Any) -> GeminiResponse:
    blocked = getattr(getattr(response, "prompt_feedback", None), "block_reason", None)
    if blocked:
        return GeminiResponse([], "refusal", StopDetails(str(blocked)), raw=response)

    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return GeminiResponse([], "refusal", StopDetails("no candidate returned"),
                              raw=response)

    cand = candidates[0]
    finish = str(getattr(cand, "finish_reason", "") or "").rsplit(".", 1)[-1].upper()
    parts = getattr(getattr(cand, "content", None), "parts", None) or []

    blocks: list = []
    for i, p in enumerate(parts):
        call = getattr(p, "function_call", None)
        if call is not None:
            blocks.append(ToolUseBlock(
                name=call.name, input=dict(call.args or {}),
                id=call.id or f"{call.name}_{i}", part=p))
        elif getattr(p, "text", None):
            blocks.append((ThinkingBlock if getattr(p, "thought", False) else TextBlock)
                          (text=p.text, part=p))
        else:
            blocks.append(OpaqueBlock(part=p))

    if any(b.type == "tool_use" for b in blocks):
        return GeminiResponse(blocks, "tool_use", raw=response)
    if finish in REFUSALS:
        return GeminiResponse(blocks, "refusal",
                              StopDetails(f"Gemini stopped: {finish}"), raw=response)
    if finish == "MAX_TOKENS":
        return GeminiResponse(blocks, "max_tokens", raw=response)
    return GeminiResponse(blocks, "end_turn", raw=response)


# --------------------------------------------------------------------------
# the transport
# --------------------------------------------------------------------------

@dataclass
class GeminiTransport:
    """Satisfies `chat.Transport`.  The Anthropic model id in the request is
    ignored -- the caller cannot know this transport's model, and pretending
    otherwise would put a name in the logs that was never called."""
    model: str = DEFAULT_MODEL
    client: Any = None
    types: Any = None
    include_thoughts: bool = True
    calls: list = field(default_factory=list)

    def __post_init__(self):
        if self.types is None:
            from google.genai import types as genai_types
            self.types = genai_types
        if self.client is None:
            from google import genai
            self.client = genai.Client()

    def create(self, **kwargs) -> GeminiResponse:
        types = self.types
        system = "\n".join(b["text"] for b in kwargs.get("system", []))
        config = types.GenerateContentConfig(
            system_instruction=system or None,
            max_output_tokens=kwargs.get("max_tokens"),
            tools=to_gemini_tools(kwargs.get("tools", []), types),
            # we drive the loop ourselves; the SDK must not call tools for us
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
            thinking_config=(types.ThinkingConfig(include_thoughts=self.include_thoughts)
                             if kwargs.get("thinking") else None),
        )
        contents = to_gemini_contents(kwargs.get("messages", []), types)
        self.calls.append({"model": self.model, "config": config, "contents": contents})
        raw = self.client.models.generate_content(
            model=self.model, contents=contents, config=config)
        return from_gemini_response(raw)


def available_models(client=None) -> list[str]:
    """What this key can actually reach.  `python api/chat.py --models`."""
    if client is None:
        from google import genai
        client = genai.Client()
    return sorted(m.name for m in client.models.list()
                  if "generateContent" in (m.supported_actions or []))
