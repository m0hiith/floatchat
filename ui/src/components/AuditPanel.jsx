/**
 * Every query that ran, with its bound parameters and row count.
 *
 * Expanded by default, and it stays on screen.  This is not debug output; it
 * is the evidence that the number on the chart came out of the database, and
 * evidence behind a disclosure triangle is evidence nobody reads (D10.5).
 *
 * The parameters shown are the ones the SERVER bound, taken from the query
 * response — not the ones typed into the form.  That distinction is the whole
 * point: leave `bin_dbar` blank and this panel reports `bin_dbar=50`, which is
 * the catalogue's default made visible rather than assumed.
 */

import { useState } from "react";
import { formatParams, rowCount } from "../format";

function Entry({ entry }) {
  const refused = entry.status === "refused";
  const failed = entry.status === "failed";
  return (
    <li className="border-b border-slate-200 px-3 py-2.5 last:border-b-0">
      <div className="flex items-baseline justify-between gap-3">
        <span className="font-mono text-xs font-semibold text-slate-800">{entry.query}</span>
        <div className="flex shrink-0 items-baseline gap-1.5">
          {/* Who chose this query. The two are the same eleven queries, run the
              same way, which is the point -- but which of them picked it is a
              fact the trail should not lose. */}
          {entry.via === "chat" && (
            <span className="rounded bg-indigo-100 px-1.5 py-px text-[10px] font-medium text-indigo-800">
              model
            </span>
          )}
          <span className="font-mono text-[11px] text-slate-400">{entry.at}</span>
        </div>
      </div>

      <div className="mt-1 font-mono text-[11px] leading-relaxed break-words text-slate-600">
        {formatParams(entry.params)}
      </div>

      {entry.defaulted?.length > 0 && (
        <div className="mt-1 text-[11px] text-slate-500">
          bound by the catalogue, not typed:{" "}
          {entry.defaulted.map((k) => (
            <code key={k} className="mr-1 rounded bg-amber-100 px-1 py-px font-mono text-amber-900">
              {k}={String(entry.params[k])}
            </code>
          ))}
        </div>
      )}

      <div className="mt-1.5">
        {refused ? (
          <span className="inline-block rounded bg-amber-100 px-1.5 py-0.5 text-[11px] font-medium text-amber-900">
            refused — no SQL was bound
          </span>
        ) : failed ? (
          <span className="inline-block rounded bg-red-100 px-1.5 py-0.5 text-[11px] font-medium text-red-900">
            {entry.error}
          </span>
        ) : (
          <span
            className={`inline-block rounded px-1.5 py-0.5 text-[11px] font-medium ${
              entry.rows === 0
                ? "bg-slate-200 text-slate-700"
                : "bg-emerald-100 text-emerald-900"
            }`}
          >
            {rowCount(entry.rows)}
          </span>
        )}
      </div>

      {refused && (
        <p className="mt-1 text-[11px] leading-snug break-words text-amber-800">{entry.error}</p>
      )}
    </li>
  );
}

export default function AuditPanel({ entries }) {
  // Expanded by default. Collapsing is available; it is not the initial state.
  const [open, setOpen] = useState(true);
  const ran = entries.filter((e) => e.status === "ok").length;
  const refused = entries.filter((e) => e.status === "refused").length;

  return (
    <section className="flex min-h-0 flex-col rounded-lg border border-slate-300 bg-white">
      <header className="flex items-baseline justify-between gap-2 border-b border-slate-200 px-3 py-2">
        <div>
          <h2 className="text-sm font-semibold text-slate-800">Audit trail</h2>
          <p className="text-[11px] text-slate-500">
            {entries.length === 0
              ? "Nothing has run yet."
              : `${ran} ${ran === 1 ? "query" : "queries"} ran${refused ? `, ${refused} refused` : ""}.`}
          </p>
        </div>
        <button
          onClick={() => setOpen((v) => !v)}
          className="shrink-0 rounded border border-slate-300 px-2 py-0.5 text-[11px] text-slate-600 hover:bg-slate-50"
        >
          {open ? "Collapse" : "Expand"}
        </button>
      </header>

      {open && (
        <div className="min-h-0 flex-1 overflow-y-auto">
          {entries.length === 0 ? (
            <p className="px-3 py-6 text-center text-xs text-slate-400">
              Run a query — from the catalogue or by asking a question — and every one
              will be listed here, with the parameters the catalogue bound and how many
              rows came back.
            </p>
          ) : (
            <ol className="divide-y divide-slate-100">
              {entries.map((entry) => (
                <Entry key={entry.seq} entry={entry} />
              ))}
            </ol>
          )}
        </div>
      )}

      <footer className="border-t border-slate-200 bg-slate-50 px-3 py-2 text-[11px] leading-snug text-slate-500">
        Parameters are read from the API response, so defaults the form left blank appear
        here with the value the catalogue chose. Nothing on this panel is composed by the
        browser.
      </footer>
    </section>
  );
}
