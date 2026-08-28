/**
 * Stage 11: the conversational half of the dashboard.
 *
 * The thing worth noticing about this file is how little of it is new.  An
 * answer's charts are drawn by `ResultPanel` from `displays.js`, exactly as the
 * catalogue panel draws them, from rows that came out of the same eleven
 * queries.  There is no chat-specific renderer and no chat-specific query
 * path: the model's only power is choosing which catalogue query runs and what
 * goes into its parameters, and this panel shows both.
 *
 * Three things are on screen for every answer, and none of them is optional:
 *
 *   the answer      what the model said
 *   the queries     which catalogue query ran, with the parameters the
 *                   catalogue BOUND (defaults included), and the chart drawn
 *                   from the rows it returned
 *   the notes       what the vector index retrieved before the model chose,
 *                   each with the SQL that generated it
 *
 * The notes are collapsed and the queries are not.  That ordering is the
 * project's argument in miniature: retrieval steered the choice, and the
 * number came from the query.
 *
 * Stage 12 adds a second path and refuses to blur it.  The lexical router
 * answers with no model at all, and everything on screen says so: the composer
 * names the path before you send, the reply carries a "lexical router · no
 * model" badge, and the bubble states which query ran rather than describing
 * the ocean.  A reader must never have to guess which engine produced what
 * they are looking at.
 */

import { useEffect, useRef, useState } from "react";
import { ask } from "../api";
import ResultPanel from "./ResultPanel";
import { DisplayBoundary, ModelFailure, Refusal, ApiFailure, Spinner } from "./States";
import { displayFor, suggestionsFor } from "../displays";
import { formatParams, monthYear, rowCount, timeOfDay } from "../format";

export default function ChatPanel({ meta, outlines, onQueries, onSwitchToCatalogue,
                                   path, onPath }) {
  const [turns, setTurns] = useState([]);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [useRetrieval, setUseRetrieval] = useState(true);
  const bottom = useRef(null);

  const ai = meta.ai ?? {};
  const rag = ai.retrieval ?? {};
  const router = ai.router ?? {};
  // The selection lives in App so the header cannot contradict the replies:
  // a tab badged "RAG" above a thread of answers badged "no model" is the
  // dashboard disagreeing with itself about what just happened.
  const setPath = onPath;
  const lexical = path === "lexical";
  const suggestions = suggestionsFor(meta, { monthYear });

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [turns, busy]);

  async function send(question) {
    const text = (question ?? draft).trim();
    if (!text || busy) return;
    setDraft("");
    setBusy(true);
    const at = timeOfDay();
    setTurns((t) => [...t, { role: "user", text, at }]);

    try {
      const data = await ask(text, {
        retrieval: useRetrieval && !lexical,
        provider: lexical ? "lexical" : null,
      });
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
      setTurns((t) => [...t, { role: "assistant", at: timeOfDay(), error }]);
    } finally {
      setBusy(false);
    }
  }

  if (!ai.available) {
    return (
      <div className="mx-auto max-w-2xl py-8">
        <ModelFailure
          error={{ message: "This server has no model credentials.", detail: ai.reason }}
          onSwitchToCatalogue={onSwitchToCatalogue}
        />
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* `flex-1` on an empty thread left ~500px of dead space between the
          opening card and the composer. The thread only claims the space once
          there is something in it. */}
      <div className={`space-y-4 overflow-y-auto pr-1 ${
        turns.length === 0 ? "shrink-0" : "min-h-0 flex-1"}`}>
        {turns.length === 0 && (
          <Opening meta={meta} rag={rag} router={router} lexical={lexical} />
        )}

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

      <div className={`space-y-2 ${turns.length === 0 ? "mt-3" : "mt-3 shrink-0"}`}>
        {turns.length === 0 && suggestions.length > 0 && (
          <ul className="flex flex-wrap gap-1.5">
            {suggestions.map((s) => (
              <li key={s.from}>
                <button
                  onClick={() => send(s.text)}
                  className="rounded-full border border-slate-300 bg-white px-3 py-1 text-xs text-slate-600 hover:border-slate-400 hover:bg-slate-50"
                >
                  {s.text}
                </button>
              </li>
            ))}
          </ul>
        )}

        <div className="rounded-lg border border-slate-300 bg-white p-2">
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send();
              }
            }}
            rows={2}
            placeholder="Ask about the ARGO floats — enter to send, shift+enter for a new line"
            className="w-full resize-none px-2 py-1 text-sm text-slate-800 outline-none placeholder:text-slate-400"
          />
          <div className="flex flex-wrap items-center justify-between gap-2 border-t border-slate-100 px-2 pt-2">
            <div className="flex flex-wrap items-center gap-3">
              {/* Which engine answers is chosen before sending and shown after.
                  There is no automatic fallback from one to the other: an
                  answer that silently changed engine would read as though a
                  model wrote it. */}
              <div className="flex rounded border border-slate-300 p-0.5 text-[11px]">
                <button
                  onClick={() => setPath("model")}
                  disabled={!ai.model_provider}
                  title={ai.model_provider ? undefined : ai.reason}
                  className={`rounded px-2 py-0.5 ${
                    !lexical ? "bg-slate-800 text-white"
                      : ai.model_provider ? "text-slate-600 hover:bg-slate-100"
                        : "text-slate-300"
                  }`}
                >
                  model{ai.model_provider ? ` · ${ai.model_provider}` : ""}
                </button>
                <button
                  onClick={() => setPath("lexical")}
                  className={`rounded px-2 py-0.5 ${
                    lexical ? "bg-slate-800 text-white" : "text-slate-600 hover:bg-slate-100"
                  }`}
                >
                  lexical router · no model
                </button>
              </div>
              {lexical ? (
                <span className="text-[11px] text-slate-500">
                  <span className="font-mono text-slate-400">
                    {router.routes} routes · {router.exemplars} examples · floor {router.floor}
                  </span>
                </span>
              ) : (
                <label className="flex items-center gap-1.5 text-[11px] text-slate-500">
                  <input
                    type="checkbox"
                    checked={useRetrieval}
                    onChange={(e) => setUseRetrieval(e.target.checked)}
                    disabled={!rag.available}
                  />
                  {rag.available ? (
                    <>
                      retrieval{" "}
                      <span className="font-mono text-slate-400">
                        {rag.documents} docs · {rag.embedder}
                      </span>
                    </>
                  ) : (
                    <>retrieval unavailable — {rag.reason}</>
                  )}
                </label>
              )}
            </div>
            <button
              onClick={() => send()}
              disabled={busy || draft.trim().length === 0}
              className="rounded bg-slate-800 px-4 py-1.5 text-sm font-medium text-white hover:bg-slate-700 disabled:cursor-not-allowed disabled:bg-slate-300"
            >
              {busy ? "Asking…" : "Ask"}
            </button>
          </div>
        </div>
        <p className="px-1 text-[11px] leading-snug text-slate-400">
          {lexical
            ? `Your wording is matched against written examples to pick one of the ${meta.queries.length} catalogue queries. This is lexical matching, not a language model and not semantic search: it cannot follow up, cannot chain queries and cannot write prose about the data.`
            : `The model chooses one of the ${meta.queries.length} catalogue queries and fills its parameters.`}{" "}
          Either way it cannot write SQL, and the connection it runs on holds{" "}
          <code className="font-mono">SELECT</code> and nothing else. Every number below came
          from a query named in its own answer.
        </p>
      </div>
    </div>
  );
}

function Opening({ meta, rag, router, lexical }) {
  const db = meta.database;
  return (
    <div className="rounded-lg border border-dashed border-slate-300 bg-white p-6">
      <h3 className="text-sm font-semibold text-slate-800">Ask a question in English</h3>
      <p className="mt-1 text-sm text-slate-500">
        {db.profiles.toLocaleString("en")} profiles from {db.floats} floats,{" "}
        {db.levels.toLocaleString("en")} measured levels, {db.window.start} to{" "}
        {db.window.end}. Pressure in dbar, temperature in °C, salinity in PSU — and no
        biogeochemical parameters, because these floats do not carry any.
      </p>
      {/* Only describe retrieval on the path that uses it. Saying "put in
          front of your question so the model knows which query to reach for"
          while a router with no model is answering would be describing a
          mechanism that is not running. */}
      {rag.available && !lexical && (
        <p className="mt-3 text-[11px] text-slate-400">
          {rag.documents} summaries are indexed in {rag.dimensions} dimensions
          ({rag.embedder}); the closest {rag.k} are put in front of your question so the model
          knows which query to reach for. They orient it. They never become the answer.
        </p>
      )}
      {router.available && lexical && (
        <p className="mt-2 text-[11px] text-slate-400">
          The <strong className="text-slate-500">lexical router</strong> is answering: your
          wording is matched against {router.exemplars} written examples to pick one of the{" "}
          {meta.queries.length} queries. No model, no key, no network. It cannot chain
          queries, cannot follow up, and writes no prose about the data — the chart and the
          audit trail are the answer.
        </p>
      )}
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
    if (error.kind === "no-model") {
      return <ModelFailure error={error} onSwitchToCatalogue={onSwitchToCatalogue} />;
    }
    if (error.kind === "refused") return <Refusal error={error} />;
    return <ApiFailure error={error} />;
  }

  const { answer, audit = [], retrieved = [], provider, turns: steps, refusal,
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
      {retrieved.length > 0 && <Retrieved notes={retrieved} />}
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

/** What the vector index put in front of the question, and where it came from. */
function Retrieved({ notes }) {
  const [open, setOpen] = useState(false);
  const failed = notes.length === 1 && notes[0].kind === "error";

  if (failed) {
    return (
      <p className="px-1 text-[11px] text-amber-700">
        Retrieval was unavailable for this question and the model answered without it:{" "}
        <span className="font-mono">{notes[0].text}</span>
      </p>
    );
  }

  return (
    <div className="rounded-lg border border-slate-200 bg-white">
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-baseline justify-between gap-2 px-3 py-2 text-left hover:bg-slate-50"
      >
        <span className="text-[11px] font-semibold text-slate-600">
          {notes.length} summaries retrieved before the model chose a query
        </span>
        <span className="text-[11px] text-slate-400">{open ? "hide" : "show"}</span>
      </button>
      {open && (
        <ol className="divide-y divide-slate-100 border-t border-slate-100">
          {notes.map((note) => (
            <li key={note.doc_id} className="px-3 py-2">
              <div className="flex items-baseline justify-between gap-2">
                <span className="text-[11px] font-semibold text-slate-700">{note.title}</span>
                <span className="shrink-0 font-mono text-[10px] text-slate-400">
                  {note.kind} · {note.score.toFixed(3)}
                </span>
              </div>
              <p className="mt-1 text-[11px] leading-snug text-slate-500">{note.text}</p>
              <details className="mt-1">
                <summary className="cursor-pointer text-[10px] text-slate-400">
                  the query that generated this summary
                </summary>
                <pre className="mt-1 overflow-x-auto rounded bg-slate-50 p-2 font-mono text-[10px] whitespace-pre-wrap text-slate-500">
                  {note.source}
                </pre>
              </details>
            </li>
          ))}
        </ol>
      )}
      <p className="border-t border-slate-100 px-3 py-1.5 text-[10px] text-slate-400">
        Summaries of the database, not query results. They tell the model where to look; the
        numbers above came from the queries listed with them.
      </p>
    </div>
  );
}
