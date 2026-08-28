/**
 * Rows as a table.  Also the "show the numbers" view under every chart, which
 * is why it takes a `dense` flag rather than being duplicated.
 *
 * `orient: "row"` flips a one-row result to label/value pairs down the page.
 * region_summary is one row of nine columns and reads as a horizontal scroll
 * otherwise (D10.4).
 */

import { COLUMN_LABELS, DATA_MODES } from "../displays";
import { cell } from "../format";

function header(key) {
  return COLUMN_LABELS[key] ?? key.replace(/_/g, " ");
}

function Cell({ column, value, wrap }) {
  const { text, absent } = cell(value);

  if (absent) {
    return (
      <span className="text-slate-400" title="null — no value in the database, not zero">
        {text}
      </span>
    );
  }
  if (column === "data_mode" && DATA_MODES[value]) {
    const mode = DATA_MODES[value];
    return (
      <span className="inline-flex items-center gap-1.5" title={mode.label}>
        <span className="h-2 w-2 rounded-full" style={{ background: mode.colour }} />
        <span className="font-mono">{value}</span>
        <span className="text-slate-500">{mode.label}</span>
      </span>
    );
  }
  return <span className={wrap ? "" : "whitespace-nowrap"}>{text}</span>;
}

export default function TableView({ rows, spec = {}, dense = false }) {
  if (!rows.length) return null;
  const columns = Object.keys(rows[0]);
  const wrapCols = new Set(spec.wrap ?? []);

  if (spec.orient === "row" && rows.length === 1) {
    const row = rows[0];
    return (
      <dl className="divide-y divide-slate-200 rounded-lg border border-slate-200 bg-white">
        {columns.map((column) => (
          <div key={column} className="grid grid-cols-[minmax(0,14rem)_1fr] gap-4 px-4 py-2.5">
            <dt className="text-sm text-slate-500">{header(column)}</dt>
            <dd className="text-sm font-medium tabular-nums">
              <Cell column={column} value={row[column]} wrap />
            </dd>
          </div>
        ))}
      </dl>
    );
  }

  return (
    <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
      <table className={`w-full ${dense ? "text-xs" : "text-sm"}`}>
        <thead className="sticky top-0 bg-slate-50 text-left">
          <tr className="border-b border-slate-200">
            {columns.map((column) => (
              <th key={column} className="px-3 py-2 font-medium whitespace-nowrap text-slate-600">
                {header(column)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-100">
          {rows.map((row, i) => (
            <tr key={i} className="hover:bg-slate-50">
              {columns.map((column) => (
                <td
                  key={column}
                  className={`px-3 py-1.5 tabular-nums align-top ${
                    wrapCols.has(column) ? "min-w-[22rem]" : ""
                  }`}
                >
                  <Cell column={column} value={row[column]} wrap={wrapCols.has(column)} />
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
