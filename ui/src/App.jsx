/**
 * Stage 10: the dashboard, with the AI switched off.
 *
 * Eleven queries, dropdowns, and an audit trail.  No chat box: the point of
 * this stage is that the platform is complete and provable before anything
 * routes into it, so every query type has a defined visual output that a human
 * can reach without a model in the loop.
 *
 * Everything the page knows — regions, floats, the date window, which
 * parameters exist and what values they accept — arrives from GET /meta.  The
 * only knowledge held in the browser is how each query is DRAWN (displays.js),
 * which is a presentation choice with no counterpart in the database.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { getMeta, getRegionOutlines, runQuery, ApiError, API_BASE } from "./api";
import { displayFor } from "./displays";
import ParamControls, { toRequestParams, missingRequired } from "./components/ParamControls";
import ResultPanel from "./components/ResultPanel";
import AuditPanel from "./components/AuditPanel";
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
      setAudit((a) => [
        ...a,
        { seq: a.length, at, status: "ok", query: data.query,
          params: data.params, rows: data.row_count, defaulted },
      ]);
    } catch (error) {
      const kind = error.kind === "refused" ? "refused" : "failed";
      setResult({ status: kind, error });
      setAudit((a) => [
        ...a,
        { seq: a.length, at, status: kind, query: query.name,
          params: sent, error: error.message },
      ]);
    }
  }

  // ---------------------------------------------------------------- chrome

  if (metaError) {
    return (
      <div className="mx-auto max-w-2xl p-8">
        <Header meta={null} />
        <div className="mt-6">
          <ApiFailure error={metaError} onRetry={loadMeta} />
        </div>
      </div>
    );
  }

  if (!meta) {
    return (
      <div className="mx-auto max-w-2xl p-8">
        <Header meta={null} />
        <div className="mt-6 rounded-lg border border-slate-300 bg-white">
          <Spinner label={`Loading /meta from ${API_BASE}`} />
        </div>
      </div>
    );
  }

  return (
    <div className="mx-auto flex h-screen max-w-[110rem] flex-col gap-4 p-4">
      <Header meta={meta} />

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

function Header({ meta }) {
  return (
    <header className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1 rounded-lg border border-slate-300 bg-white px-4 py-2.5">
      <div className="flex items-baseline gap-3">
        <h1 className="text-base font-semibold text-slate-900">FloatChat</h1>
        <span className="text-xs text-slate-500">
          the query catalogue, with the AI switched off
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

function Stat({ label, value }) {
  return (
    <div className="flex items-baseline gap-1.5">
      <dt className="text-slate-400">{label}</dt>
      <dd className="font-medium tabular-nums text-slate-800">{value}</dd>
    </div>
  );
}
