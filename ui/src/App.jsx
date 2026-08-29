/**
 * The dashboard: two ways into the same eleven queries.
 *
 * Stage 10 built this with the AI switched off, deliberately — the platform
 * had to be complete and provable before anything routed into it, so every
 * query type has a defined visual output a human can reach with no model in
 * the loop.  Stage 11 switches it on beside that, and the arrangement is the
 * argument: **Chat** and **Catalogue** are two front doors to one catalogue,
 * and they share an audit trail.  A query the model chose and a query a human
 * chose land in the same list, drawn by the same displays.js mapping, from
 * rows returned by the same read-only role.
 *
 * Stage 16 changes which door is open when you arrive.  The page lands in
 * Chat; the catalogue is one click away and loses nothing.  That is an
 * emphasis change and it is deliberately not more than one: both paths still
 * run the same eleven queries, the audit trail is still mounted beside the
 * conversation rather than folded away, and the badge still names the engine
 * that answered.
 *
 * Everything the page knows — regions, floats, the date window, which
 * parameters exist and what values they accept, whether there is a model at
 * all — arrives from GET /meta.  The only knowledge held in the browser is how
 * each query is DRAWN (displays.js), which is a presentation choice with no
 * counterpart in the database.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { getMeta, getRegionOutlines, runQuery, ApiError, API_BASE } from "./api";
import { displayFor } from "./displays";
import ParamControls, { toRequestParams, missingRequired } from "./components/ParamControls";
import ResultPanel from "./components/ResultPanel";
import AuditPanel from "./components/AuditPanel";
import ChatPanel from "./components/ChatPanel";
import { ApiFailure, Refusal, Spinner, Idle, DisplayBoundary } from "./components/States";
import { timeOfDay } from "./format";

export default function App() {
  const [meta, setMeta] = useState(null);
  const [metaError, setMetaError] = useState(null);
  const [outlines, setOutlines] = useState(null);
  const [selected, setSelected] = useState(null);
  const [values, setValues] = useState({});
  const [result, setResult] = useState({ status: "idle" });
  const [audit, setAudit] = useState([]);
  // Chat is the front door (D16.1). The catalogue keeps everything it had and
  // is one click away, but it is no longer what a reader lands in. It was the
  // default while it was the only path that could answer without a key; since
  // Stage 12 the chat tab answers without one too, so landing a reader in a
  // parameter form was showing them the second-best door first.
  const [mode, setMode] = useState("chat");

  // One trail, two sources. `seq` is assigned here so a batch of queries from
  // a single question keeps its order without either caller counting.
  const addAudit = useCallback((entries) => {
    setAudit((a) => [...a, ...entries.map((e, i) => ({ seq: a.length + i, ...e }))]);
  }, []);

  const loadMeta = useCallback(async () => {
    setMeta(null);
    setMetaError(null);
    try {
      const data = await getMeta();
      setMeta(data);
      setSelected((s) => s ?? data.queries[0]?.name ?? null);
    } catch (error) {
      // Nothing is rendered half-loaded. No dropdowns at all is correct here:
      // empty dropdowns would claim the database is empty.
      setMetaError(error instanceof ApiError ? error : new ApiError("bad", String(error)));
    }
  }, []);

  useEffect(() => { loadMeta(); }, [loadMeta]);

  // Outlines are optional decoration. Their failure must not take the map down.
  useEffect(() => {
    if (!meta) return;
    getRegionOutlines().then(setOutlines).catch(() => setOutlines(null));
  }, [meta]);

  const query = useMemo(
    () => meta?.queries.find((q) => q.name === selected) ?? null,
    [meta, selected],
  );
  const spec = selected ? displayFor(selected) : null;
  const missing = query ? missingRequired(query.params, values) : [];

  // What would answer the next question, derived once and read by the tab
  // badge, the composer and the badge on every reply rather than each deciding
  // for itself. Since D16.8 there is exactly one answer to that question in
  // this dashboard: the lexical router. `canAnswer` therefore reads the
  // ROUTER's availability, not `ai.available` -- `ai.available` is true when
  // *either* path can answer, and a model this UI will not call is not a
  // reason to offer a chat box that cannot answer either.
  const ai = meta?.ai ?? {};
  const canAnswer = !!ai.router?.available;

  function pick(name) {
    setSelected(name);
    setValues({});
    setResult({ status: "idle" });
  }

  async function run() {
    if (!query) return;
    const sent = toRequestParams(query.params, values);
    setResult({ status: "loading" });
    const at = timeOfDay();

    try {
      const data = await runQuery(query.name, sent);
      // Which parameters the catalogue filled in that we did not send. This is
      // the defaults-honesty check, computed from the response, not the form.
      const defaulted = Object.keys(data.params ?? {}).filter(
        (k) => !(k in sent) && data.params[k] !== null,
      );
      setResult({ status: "ok", ...data });
      addAudit([{ at, status: "ok", query: data.query, params: data.params,
                  rows: data.row_count, defaulted }]);
    } catch (error) {
      const kind = error.kind === "refused" ? "refused" : "failed";
      setResult({ status: kind, error });
      addAudit([{ at, status: kind, query: query.name, params: sent,
                  error: error.message }]);
    }
  }

  // ---------------------------------------------------------------- chrome

  if (metaError) {
    return (
      <div className="mx-auto max-w-2xl p-8">
        <Header meta={null} mode={mode} onMode={setMode} />
        <div className="mt-6">
          <ApiFailure error={metaError} onRetry={loadMeta} />
        </div>
      </div>
    );
  }

  if (!meta) {
    return (
      <div className="mx-auto max-w-2xl p-8">
        <Header meta={null} mode={mode} onMode={setMode} />
        <div className="mt-6 rounded-lg border border-slate-300 bg-white">
          <Spinner label={`Loading /meta from ${API_BASE}`} />
        </div>
      </div>
    );
  }

  if (mode === "chat") {
    return (
      <div className="mx-auto flex h-screen max-w-[110rem] flex-col gap-3 p-3 sm:p-4">
        <Header meta={meta} mode={mode} onMode={setMode} canAnswer={canAnswer} />
        {/* The conversation takes the width it can and the trail keeps a fixed
            rail. The trail stays MOUNTED here rather than folding behind a
            control: it is the evidence that every number came out of a query,
            and evidence you have to go looking for is evidence nobody reads
            (D10.5). What Stage 16 changed is the emphasis, not the audit. */}
        <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_20rem]">
          <main className="min-h-0">
            <ChatPanel
              meta={meta}
              outlines={outlines}
              onQueries={addAudit}
              onSwitchToCatalogue={() => setMode("catalogue")}
            />
          </main>
          <div className="min-h-0 lg:max-h-full">
            <AuditPanel entries={audit} />
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="mx-auto flex h-screen max-w-[110rem] flex-col gap-4 p-4">
      {/* The badge describes what the Chat tab WOULD answer with, so it is
          passed here too: this tab must not describe the other one wrongly,
          which is how D13.3's false sentence was reached from this side. */}
      <Header meta={meta} mode={mode} onMode={setMode} canAnswer={canAnswer} />

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 lg:grid-cols-[19rem_minmax(0,1fr)_21rem]">
        {/* left: the picker and its controls */}
        <aside className="flex min-h-0 flex-col gap-3">
          {/* The picker keeps its place; only the parameter form scrolls. Selecting
              a query low in the list must not scroll the list off the screen. */}
          <section className="shrink-0 rounded-lg border border-slate-300 bg-white">
            <h2 className="border-b border-slate-200 px-3 py-2 text-sm font-semibold text-slate-800">
              Queries <span className="font-normal text-slate-400">({meta.queries.length})</span>
            </h2>
            <ul className="p-1.5">
              {meta.queries.map((q) => {
                const d = displayFor(q.name);
                const active = q.name === selected;
                return (
                  <li key={q.name}>
                    <button
                      onClick={() => pick(q.name)}
                      className={`w-full rounded px-2 py-1.5 text-left ${
                        active ? "bg-slate-800 text-white" : "hover:bg-slate-100"
                      }`}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="font-mono text-xs">{q.name}</span>
                        <span className={`shrink-0 rounded px-1.5 py-px text-[10px] uppercase ${
                          active ? "bg-white/20" : "bg-slate-200 text-slate-600"
                        }`}>
                          {d?.display ?? "?"}
                        </span>
                      </div>
                    </button>
                  </li>
                );
              })}
            </ul>
          </section>

          {query && (
            <section className="flex min-h-0 flex-col rounded-lg border border-slate-300 bg-white">
              <div className="shrink-0 border-b border-slate-200 px-3 py-2">
                <h2 className="text-sm font-semibold text-slate-800">Parameters</h2>
                <p className="mt-0.5 text-[11px] leading-snug text-slate-500">
                  {query.description}
                </p>
              </div>
              <div className="min-h-0 flex-1 overflow-y-auto p-3">
                <ParamControls
                  params={query.params}
                  values={values}
                  onChange={(k, v) => setValues((s) => ({ ...s, [k]: v }))}
                  meta={meta}
                />
                <button
                  onClick={run}
                  disabled={missing.length > 0 || result.status === "loading"}
                  className="mt-4 w-full rounded bg-slate-800 px-3 py-2 text-sm font-medium text-white
                             hover:bg-slate-700 disabled:cursor-not-allowed disabled:bg-slate-300"
                >
                  {result.status === "loading" ? "Running…" : "Run query"}
                </button>
                {missing.length > 0 && (
                  <p className="mt-2 text-[11px] text-slate-500">
                    Waiting for: <code className="font-mono">{missing.join(", ")}</code>
                  </p>
                )}
              </div>
            </section>
          )}
        </aside>

        {/* centre: the result */}
        <main className="min-h-0 overflow-y-auto rounded-lg border border-slate-300 bg-slate-50 p-4">
          {query && (
            <div className="mb-3 flex items-baseline justify-between gap-3">
              <h2 className="font-mono text-sm font-semibold text-slate-800">{query.name}</h2>
              <span className="rounded bg-slate-200 px-2 py-0.5 text-[11px] uppercase text-slate-600">
                {spec?.display}
                {spec?.mode ? ` · ${spec.mode}` : ""}
                {spec?.orientation ? ` · ${spec.orientation}` : ""}
              </span>
            </div>
          )}

          {result.status === "idle" && (
            <Idle>
              Choose parameters and run the query. Results appear here as a{" "}
              <strong>{spec?.display}</strong>.
            </Idle>
          )}
          {result.status === "loading" && (
            <div className="rounded-lg border border-slate-300 bg-white">
              <Spinner label={`Running ${query.name}`} />
            </div>
          )}
          {result.status === "refused" && <Refusal error={result.error} />}
          {result.status === "failed" && <ApiFailure error={result.error} onRetry={run} />}
          {result.status === "ok" && spec && (
            <DisplayBoundary resetKey={`${selected}:${audit.length}`}>
              <ResultPanel result={result} spec={spec} meta={meta} outlines={outlines} />
            </DisplayBoundary>
          )}
        </main>

        {/* right: the audit trail, expanded */}
        <div className="min-h-0 lg:max-h-full">
          <AuditPanel entries={audit} />
        </div>
      </div>
    </div>
  );
}

/**
 * `canAnswer` is passed in, never re-derived here.  It describes what would
 * answer RIGHT NOW, and since D16.8 that is one engine: the lexical router.
 *
 * The badge is a single state for the same reason it used to be three -- it
 * has to be true.  When the dashboard could pick between engines it said which
 * one was selected (RAG / model · X / no model, D13.3); now that it cannot, it
 * says the only thing left to say, in the same words the reply badge uses.
 */
function Header({ meta, mode, onMode, canAnswer }) {
  const ai = meta?.ai;
  const badge = { text: "no model", tone: "bg-slate-200 text-slate-600" };
  return (
    <header className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1 rounded-lg border border-slate-300 bg-white px-4 py-2.5">
      <div className="flex items-center gap-3">
        <h1 className="text-base font-semibold text-slate-900">FloatChat</h1>
        {meta && (
          <nav className="flex rounded-md border border-slate-300 p-0.5 text-xs">
            {/* Chat first, because chat is what the page opens in (D16.1).
                The tab is offered whenever the ROUTER can answer, which needs
                no key and no network -- not whenever `ai.available` is true,
                because that is also true on a server whose only working path
                is the model this dashboard no longer calls (D16.8). */}
            <Tab
              active={mode === "chat"}
              onClick={() => onMode("chat")}
              title={canAnswer ? undefined : ai?.router?.reason}
              muted={!canAnswer}
            >
              Chat
              {canAnswer && (
                <span className={`ml-1.5 rounded px-1 py-px text-[10px] font-medium ${badge.tone}`}>
                  {badge.text}
                </span>
              )}
            </Tab>
            <Tab active={mode === "catalogue"} onClick={() => onMode("catalogue")}>
              Catalogue
            </Tab>
          </nav>
        )}
        <span className="hidden text-xs text-slate-500 xl:inline">
          {mode === "chat"
            ? "a lexical router picks the query — no model in this path"
            : "the query catalogue, the same queries by hand"}
        </span>
      </div>
      {meta ? (
        <dl className="flex flex-wrap gap-x-5 gap-y-1 text-xs text-slate-600">
          <Stat label="floats" value={meta.database.floats} />
          <Stat label="profiles" value={meta.database.profiles.toLocaleString("en")} />
          <Stat label="levels" value={meta.database.levels.toLocaleString("en")} />
          <Stat label="regions" value={meta.regions.length} />
          <Stat
            label="window"
            value={`${meta.database.window.start} → ${meta.database.window.end}`}
          />
        </dl>
      ) : (
        <span className="text-xs text-slate-400">{API_BASE}</span>
      )}
    </header>
  );
}

function Tab({ active, onClick, children, title, muted }) {
  return (
    <button
      onClick={onClick}
      title={title}
      className={`rounded px-2.5 py-1 font-medium ${
        active
          ? "bg-slate-800 text-white"
          : muted
            ? "text-slate-400 hover:bg-slate-100"
            : "text-slate-600 hover:bg-slate-100"
      }`}
    >
      {children}
    </button>
  );
}

function Stat({ label, value }) {
  return (
    <div className="flex items-baseline gap-1.5">
      <dt className="text-slate-400">{label}</dt>
      <dd className="font-medium tabular-nums text-slate-800">{value}</dd>
    </div>
  );
}
