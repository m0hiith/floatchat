/**
 * The conversational half of the dashboard, and since Stage 16 the half it
 * opens in.
 *
 * The thing worth noticing about this file is how little of it is its own.  An
 * answer's charts are drawn by `ResultPanel` from `displays.js`, exactly as the
 * catalogue panel draws them, from rows that came out of the same eleven
 * queries.  There is no chat-specific renderer and no chat-specific query
 * path: the router's only power is choosing which catalogue query runs and
 * what goes into its parameters, and this panel shows both.
 *
 * Two things are on screen for every answer, and neither is optional:
 *
 *   the answer      which catalogue query ran -- a statement about what ran,
 *                   never a sentence about the ocean, because nothing in this
 *                   path writes prose about the data
 *   the queries     the parameters the catalogue BOUND (defaults included),
 *                   where each bound value came from, and the chart drawn from
 *                   the rows that came back
 *
 * **One engine, named everywhere (D16.8).**  Stage 12 added a second path and
 * refused to blur it; Stage 16 removed the model path from the dashboard
 * altogether rather than leaving a selector whose other setting needs a key.
 * The composer states the engine before you send, the reply carries a "lexical
 * router · no model" badge, and the audit trail chips every query `lexical`.
 * All three say the same thing because there is now only one thing to say --
 * and `provider: "lexical"` is sent explicitly on every call, so a key
 * appearing in the API's environment cannot quietly change what answered.
 *
 * **Stage 16's layout.**  An empty thread is a landing screen -- heading,
 * composer and curated suggestions centred together -- instead of a card
 * pinned above half a screen of nothing.  No sentence was softened to suit the
 * promotion, and the audit trail did not move.
 */

import { useEffect, useRef, useState } from "react";
import { ask } from "../api";
import ResultPanel from "./ResultPanel";
import { DisplayBoundary, ModelFailure, Refusal, ApiFailure, Spinner } from "./States";
import { displayFor, suggestionsFor } from "../displays";
import { formatParams, monthYear, rowCount, timeOfDay } from "../format";

export default function ChatPanel({ meta, outlines, onQueries, onSwitchToCatalogue }) {
  const [turns, setTurns] = useState([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const bottom = useRef(null);

  const ai = meta.ai ?? {};
  const router = ai.router ?? {};
  // One path, named the same way in all three places it appears -- the
  // composer before you send, the badge on the reply, the chip in the audit
  // trail (D16.8). There is no selector left to disagree with any of them.
  const suggestions = suggestionsFor(meta, { monthYear });

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns, busy]);

  async function send(question) {
    // A suggestion chip passes its own text. Only a send from the composer
    // clears the composer -- clearing it for a chip would throw away something
    // the reader had half-typed.
    const fromDraft = question === undefined;
    const text = (question ?? draft).trim();
    if (!text || busy) return;
    if (fromDraft) setDraft("");
    setBusy(true);
    const at = timeOfDay();
    setTurns((t) => [...t, { role: "user", text, at }]);

    try {
      // Explicit on every call, never inferred from what the server happens to
      // have configured: this dashboard asks the lexical router, and a key
      // appearing in the API's environment must not silently change the engine
      // behind an answer badged `lexical router · no model`.
      const data = await ask(text, { retrieval: false, provider: "lexical" });
      setTurns((t) => [...t, { role: "assistant", at: timeOfDay(), data }]);
      // The audit trail is shared with the catalogue panel: a query the model
      // chose and a query a human chose land in the same list, because they
      // are the same queries.
      onQueries?.(
        (data.audit ?? []).map((entry) => ({
          at,
          status: "error" in entry ? "refused" : "ok",
          query: entry.query,
          params: entry.params,
          rows: entry.row_count,
          error: entry.error,
          // The provider that actually answered. This was hardcoded to "chat"
          // and the trail badged a lexically-routed query as `model`, which is
          // the one thing the badge exists to prevent.
          via: data.provider,
        })),
      );
    } catch (error) {
      // A failed question stays failed and is rendered as a failure. Nothing is
      // re-asked by another engine, which is D12.12 and is now structural:
      // there is no other engine in this dashboard to fall back to.
      setTurns((t) => [...t, { role: "assistant", at: timeOfDay(), error, text }]);
    } finally {
      setBusy(false);
    }
  }

  // The router, not `ai.available`. `ai.available` is true when EITHER path
  // can answer, so on a server with a model and a broken router it would show
  // a chat box that cannot answer anything this dashboard is willing to ask.
  if (!router.available) {
    return (
      <div className="mx-auto max-w-2xl py-8">
        <ModelFailure
          error={{ message: "The lexical router is not available on this server.",
                   detail: router.reason }}
          onSwitchToCatalogue={onSwitchToCatalogue}
        />
      </div>
    );
  }

  // An empty thread is a LANDING screen, not a half-filled transcript: the
  // heading, the composer and the suggestions sit together in the middle of
  // the page, and the thread only claims the height once there is something in
  // it. Before Stage 16 the opening card was pinned to the top with ~500px of
  // dead space under it, which is what a secondary tab looks like.
  const empty = turns.length === 0;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div
        className={`mx-auto flex w-full min-h-0 flex-1 flex-col ${
          empty ? "max-w-2xl justify-center" : "max-w-3xl"
        }`}
      >
        {empty ? (
          <Opening meta={meta} router={router}
                   onSwitchToCatalogue={onSwitchToCatalogue} />
        ) : (
          <div className="min-h-0 flex-1 space-y-4 overflow-y-auto pr-1">
            {turns.map((turn, i) =>
              turn.role === "user" ? (
                <Question key={i} turn={turn} />
              ) : (
                <Reply key={i} turn={turn} meta={meta} outlines={outlines}
                       onSwitchToCatalogue={onSwitchToCatalogue} />
              ),
            )}

            {busy && (
              <div className="rounded-lg border border-slate-300 bg-white">
                <Spinner label="Choosing a query and running it" />
              </div>
            )}
            <div ref={bottom} />
          </div>
        )}

        <div className={`shrink-0 space-y-2 ${empty ? "mt-7" : "mt-3"}`}>
          <div className="rounded-2xl border border-slate-300 bg-white p-2.5 shadow-sm
                          transition focus-within:border-slate-400 focus-within:ring-4
                          focus-within:ring-slate-200/60">
            <textarea
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  send();
                }
              }}
              rows={empty ? 3 : 2}
              placeholder="Ask about the ARGO floats — enter to send, shift+enter for a new line"
              className="w-full resize-none px-2 py-1.5 text-[15px] leading-relaxed text-slate-800 outline-none placeholder:text-slate-400"
            />
            <div className="flex flex-wrap items-center justify-between gap-2 border-t border-slate-100 px-2 pt-2">
              <div className="flex flex-wrap items-center gap-2.5">
                {/* The engine, named before you send, in the same words the
                    reply is badged with. It was a selector until D16.8 and is
                    now a statement of fact, which is the only thing that
                    changed about it: there is one path, and nothing on screen
                    may imply a choice the dashboard does not offer. */}
                <span className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] font-medium text-white">
                  lexical router · no model
                </span>
                <span className="font-mono text-[11px] text-slate-400">
                  {router.routes} routes · {router.exemplars} examples · floor {router.floor}
                </span>
              </div>
              <button
                onClick={() => send()}
                disabled={busy || draft.trim().length === 0}
                className="rounded-lg bg-slate-800 px-5 py-1.5 text-sm font-medium text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:bg-slate-300"
              >
                {busy ? "Asking…" : "Ask"}
              </button>
            </div>
          </div>

          {/* Under the composer, where a landing screen puts them. Still a
              `ul > li > button` each, and still generated from the catalogue's
              own examples -- no question here contains a value this dashboard
              made up (rule 2). */}
          {empty && suggestions.length > 0 && (
            <ul className="grid gap-1.5 pt-1 sm:grid-cols-2">
              {suggestions.map((s) => (
                <li key={s.from} className="flex">
                  <button
                    onClick={() => send(s.text)}
                    className="w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-left text-xs leading-snug text-slate-600 hover:border-slate-400 hover:bg-slate-50"
                  >
                    {s.text}
                  </button>
                </li>
              ))}
            </ul>
          )}

          <p className={`px-1 text-[11px] leading-snug text-slate-400 ${
            empty ? "mx-auto max-w-xl text-center" : ""}`}>
            {`Your wording is matched against written examples to pick one of the ${meta.queries.length} catalogue queries. This is lexical matching, not a language model and not semantic search: it cannot follow up, cannot chain queries and cannot write prose about the data.`}{" "}
            It cannot write SQL either, and the connection it runs on holds{" "}
            <code className="font-mono">SELECT</code> and nothing else. Every number below came
            from a query named in its own answer.
          </p>
        </div>
      </div>
    </div>
  );
}

/**
 * The landing screen's heading.  Every sentence here is either read from
 * `/meta` or is a property of the data; none of it is decoration.  The units
 * line and the BGC sentence stay because they are the two things a reader
 * would otherwise assume wrongly before typing anything.
 */
function Opening({ meta, router, onSwitchToCatalogue }) {
  const db = meta.database;
  return (
    <div className="shrink-0 text-center">
      <h2 className="text-2xl font-semibold tracking-tight text-slate-900 sm:text-[1.7rem]">
        Ask a question in English
      </h2>
      <p className="mx-auto mt-3 max-w-xl text-sm leading-relaxed text-slate-500">
        {db.profiles.toLocaleString("en")} profiles from {db.floats} floats,{" "}
        {db.levels.toLocaleString("en")} measured levels, {db.window.start} to{" "}
        {db.window.end}. Pressure in dbar, temperature in °C, salinity in PSU — and no
        biogeochemical parameters, because these floats do not carry any.
      </p>
      {/* Retrieval is deliberately NOT described here. The index still exists
          and `/ask` still uses it on the model path, but this dashboard does
          not take that path any more (D16.8), and a landing screen explaining a
          mechanism that will not run for the question you are about to type is
          the same false sentence as a badge naming the wrong engine. */}
      {router.available && (
        <p className="mx-auto mt-3 max-w-xl text-[11px] leading-snug text-slate-400">
          The <strong className="text-slate-500">lexical router</strong> is answering: your
          wording is matched against {router.exemplars} written examples to pick one of the{" "}
          {meta.queries.length} queries. No model, no key, no network. It cannot chain
          queries, cannot follow up, and writes no prose about the data — the chart and the
          audit trail are the answer.
        </p>
      )}
      {/* The catalogue is the second door now, so the chat panel says where it
          is. It is the same eleven queries either way -- which is the claim
          this line gets to make precisely because it is true. */}
      <p className="mt-5 text-[11px] text-slate-400">
        Or pick a query by hand:{" "}
        <button
          onClick={onSwitchToCatalogue}
          className="underline underline-offset-2 hover:text-slate-600"
        >
          open the catalogue
        </button>{" "}
        — the same {meta.queries.length} queries, the same audit trail.
      </p>
    </div>
  );
}

function Question({ turn }) {
  return (
    <div className="flex justify-end">
      <div className="max-w-[85%] rounded-lg rounded-br-sm bg-slate-800 px-3.5 py-2 text-sm text-white">
        {turn.text}
      </div>
    </div>
  );
}

function Reply({ turn, meta, outlines, onSwitchToCatalogue }) {
  if (turn.error) {
    const error = turn.error;
    // Kept as a rendering, not as an offer: this panel asks for `lexical` on
    // every call, so a 503 about credentials means the server disagrees with
    // the request it was sent. It is shown as the failure it is, and there is
    // no button here that re-asks it another way (D12.12, D16.8).
    if (error.kind === "no-model") {
      return <ModelFailure error={error} onSwitchToCatalogue={onSwitchToCatalogue} />;
    }
    if (error.kind === "refused") return <Refusal error={error} />;
    return <ApiFailure error={error} />;
  }

  // `retrieved` is deliberately not read. The API still returns it on the
  // model path; this dashboard does not take that path, so there is no panel
  // here that could show notes no question in this thread was asked with.
  const { answer, audit = [], provider, turns: steps, refusal,
          refusal_reason: reason, alternatives = [], notices = [], slots = [],
          considered = [] } = turn.data;
  const lexical = provider === "lexical";

  return (
    <div className="space-y-3">
      <div className="rounded-lg rounded-bl-sm border border-slate-300 bg-white px-4 py-3">
        {lexical && (
          <div className="mb-2 flex flex-wrap items-center gap-2">
            <span className="rounded bg-slate-800 px-1.5 py-0.5 text-[10px] font-medium text-white">
              lexical router · no model
            </span>
            {considered.length > 0 && (
              <span className="font-mono text-[10px] text-slate-400">
                best match {considered[0].query} {considered[0].score.toFixed(3)}
              </span>
            )}
          </div>
        )}

        {refusal ? (
          <div>
            <p className="text-sm leading-relaxed whitespace-pre-wrap text-amber-800">
              {refusal}
            </p>
            {reason && (
              <p className="mt-1.5 text-[11px] text-amber-700">
                refused as <code className="font-mono">{reason}</code> — no query was run,
                so nothing below is data.
              </p>
            )}
            {alternatives.length > 0 && (
              <ul className="mt-2 flex flex-wrap gap-1.5">
                {alternatives.map((a) => (
                  <li key={a}
                      className="rounded bg-amber-50 px-2 py-0.5 font-mono text-[11px] text-amber-900 ring-1 ring-amber-300">
                    {a}
                  </li>
                ))}
              </ul>
            )}
          </div>
        ) : answer ? (
          <p className="text-sm leading-relaxed whitespace-pre-wrap text-slate-800">{answer}</p>
        ) : (
          <p className="text-sm text-slate-500">No text was returned.</p>
        )}

        <p className="mt-2 text-[11px] text-slate-400">
          {provider} · {steps} {steps === 1 ? "step" : "steps"} ·{" "}
          {audit.length === 0
            ? "no query was run — nothing above came from the database"
            : `${audit.length} ${audit.length === 1 ? "query" : "queries"}`}
        </p>
      </div>

      {/* A value the question did not supply is announced BEFORE the chart, not
          discovered afterwards in the trail. D10.5 made the catalogue's
          defaults visible; a router that quietly picked a date range would
          undo that work. */}
      {notices.map((n, i) => (
        <div key={i} className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2">
          <p className="text-[11px] font-semibold text-amber-900">
            A value came from a fallback, not from your question
          </p>
          <p className="mt-0.5 text-[11px] leading-snug text-amber-800">{n}</p>
        </div>
      ))}

      {audit.map((entry, i) => (
        <QueryResult key={i} entry={entry} meta={meta} outlines={outlines} seq={i} />
      ))}

      {slots.length > 0 && <Slots slots={slots} />}
    </div>
  );
}

/** Every bound parameter and where its value came from. */
function Slots({ slots }) {
  const [open, setOpen] = useState(false);
  const labels = {
    extracted: ["read from your question", "bg-emerald-100 text-emerald-900"],
    "window-fallback": ["fallback — not in your question", "bg-amber-100 text-amber-900"],
    "catalogue-default": ["the catalogue's own default", "bg-slate-200 text-slate-700"],
    missing: ["not found", "bg-red-100 text-red-900"],
  };
  return (
    <div className="rounded-lg border border-slate-200 bg-white">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-baseline justify-between gap-2 px-3 py-2 text-left hover:bg-slate-50"
      >
        <span className="text-[11px] font-semibold text-slate-600">
          where each bound value came from ({slots.length})
        </span>
        <span className="text-[11px] text-slate-400">{open ? "hide" : "show"}</span>
      </button>
      {open && (
        <ul className="divide-y divide-slate-100 border-t border-slate-100">
          {slots.map((s) => {
            const [label, tone] = labels[s.source] ?? [s.source, "bg-slate-200"];
            return (
              <li key={s.name} className="flex flex-wrap items-baseline gap-2 px-3 py-1.5">
                <code className="font-mono text-[11px] text-slate-800">
                  {s.name}={String(s.value)}
                </code>
                <span className={`rounded px-1.5 py-px text-[10px] ${tone}`}>{label}</span>
                {s.evidence && (
                  <span className="font-mono text-[10px] text-slate-400">
                    from “{s.evidence}”
                  </span>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

/**
 * One catalogue query the model ran, drawn the way the catalogue panel draws
 * it.  A refusal is shown as a refusal — including that the model was handed
 * the valid values and got to try again, which is the loop working, not
 * failing.
 */
function QueryResult({ entry, meta, outlines, seq }) {
  const spec = displayFor(entry.query);

  if (entry.error) {
    return (
      <div className="rounded-lg border border-amber-300 bg-amber-50 px-4 py-3">
        <div className="font-mono text-xs font-semibold text-amber-900">{entry.query}</div>
        <p className="mt-1 font-mono text-[11px] break-words text-amber-800">{entry.error}</p>
        <p className="mt-1.5 text-[11px] text-amber-700">
          The catalogue refused this before any SQL was bound, and handed the model the valid
          values so it could correct itself.
        </p>
      </div>
    );
  }

  return (
    <div className="rounded-lg border border-slate-300 bg-slate-50 p-3">
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
        <span className="font-mono text-xs font-semibold text-slate-800">{entry.query}</span>
        <span className="rounded bg-slate-200 px-2 py-0.5 text-[11px] text-slate-600">
          {spec?.display ?? "table"} · {rowCount(entry.row_count)}
        </span>
      </div>
      <p className="mb-2 font-mono text-[11px] break-words text-slate-500">
        {formatParams(entry.params)}
      </p>
      {spec ? (
        <DisplayBoundary resetKey={`${entry.query}:${seq}`}>
          <ResultPanel
            result={{ ...entry, rows: entry.rows ?? [], row_count: entry.row_count }}
            spec={spec}
            meta={meta}
            outlines={outlines}
          />
        </DisplayBoundary>
      ) : (
        <p className="text-xs text-slate-500">
          This query has no declared display in <code className="font-mono">displays.js</code>.
        </p>
      )}
    </div>
  );
}
