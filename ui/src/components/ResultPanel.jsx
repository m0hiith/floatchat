/**
 * Picks the display for a result and renders it.
 *
 * The only `switch` on display type in the app, and it switches on the spec's
 * `display` field — never on a query name.  A twelfth query with
 * `display: "map"` needs no change here.
 */

import { useState } from "react";
import MapView from "./MapView";
import PlotlyChart from "./PlotlyChart";
import TableView from "./TableView";
import { NoRows } from "./States";
import { rowCount } from "../format";

export default function ResultPanel({ result, spec, meta, outlines }) {
  const [showRows, setShowRows] = useState(false);
  const { rows, params, row_count: count, query } = result;
  const empty = rows.length === 0;

  // A map whose search area is known can draw itself with no rows at all: the
  // centre and the circle come from the bound parameters (requirement 4).
  const drawsWhenEmpty = spec.display === "map" && spec.mode === "radius";

  if (empty && !drawsWhenEmpty) {
    return <NoRows note={spec.empty} />;
  }

  const chart = (() => {
    switch (spec.display) {
      case "map":
        return (
          <MapView
            rows={rows}
            spec={spec}
            bound={params}
            outlines={outlines}
            extent={meta.extent}
            context={params}
          />
        );
      case "line":
      case "bar":
        return <PlotlyChart rows={rows} spec={spec} />;
      case "table":
        return <TableView rows={rows} spec={spec} />;
      default:
        return null;
    }
  })();

  return (
    <div className="space-y-3">
      {empty && drawsWhenEmpty && (
        <div className="rounded-lg border border-slate-300 bg-slate-50 px-4 py-3">
          <p className="text-sm font-semibold text-slate-700">
            no rows — 0 profiles within {params.radius_km} km
          </p>
          <p className="mt-1 text-xs text-slate-500">
            The map below shows the centre and the radius that were searched. The query ran;
            the database holds nothing inside that circle.
          </p>
        </div>
      )}

      {chart}

      {spec.display !== "table" && count > 0 && (
        <div>
          <button
            onClick={() => setShowRows((v) => !v)}
            className="rounded border border-slate-300 bg-white px-2.5 py-1 text-xs text-slate-600 hover:bg-slate-50"
          >
            {showRows ? "Hide" : "Show"} the {rowCount(count)} behind this
          </button>
          {showRows && (
            <div className="mt-2 max-h-96 overflow-y-auto">
              <TableView rows={rows} spec={spec} dense />
            </div>
          )}
        </div>
      )}

      <p className="text-[11px] text-slate-400">
        <code className="font-mono">{query}</code> · {rowCount(count)} · every value above came
        from this query. See the audit panel for the bound parameters.
      </p>
    </div>
  );
}
