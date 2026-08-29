/**
 * Line and bar charts, driven by the spec in displays.js.
 *
 * The two rules from the brief that live in this file:
 *   - depth increases downward  (spec.axis.invert, applied as autorange
 *     "reversed" on the depth axis)
 *   - units on every axis       (spec.series[].unit, appended to the title)
 *
 * A row whose value is null is passed to Plotly as null, not 0.  Plotly leaves
 * a gap; a zero would draw a line through the seabed.
 */

import { useEffect, useRef } from "react";
import Plotly from "plotly.js-dist-min";

const FONT = { family: "ui-sans-serif, system-ui, sans-serif", size: 12, color: "#334155" };
const GRID = "#e2e8f0";

const axisTitle = (label, unit) => (unit ? `${label} (${unit})` : label);

/**
 * An axis labelled and coloured for one series.
 *
 * Both charts below put two series on two axes, and the axis colour is the only
 * thing besides the legend that says which trace an axis belongs to.  The
 * colour has to go in `title.font`: Plotly's older top-level `titlefont` was
 * removed in v4 and is now dropped without a warning, so the axis kept its
 * title and quietly lost its colour.  A quiet no-op is the failure mode this
 * project is organised against (rule 7), so the shape is written once here and
 * `ui/test_ui.py` asserts the removed spelling never comes back.
 */
function seriesAxis(series, rest = {}) {
  return {
    title: {
      text: axisTitle(series.label, series.unit),
      font: { color: series.colour },
    },
    tickfont: { color: series.colour },
    ...rest,
  };
}

function baseLayout() {
  return {
    font: FONT,
    margin: { l: 70, r: 70, t: 30, b: 55 },
    paper_bgcolor: "white",
    plot_bgcolor: "white",
    hovermode: "closest",
    showlegend: true,
    legend: { orientation: "h", y: -0.18, x: 0 },
  };
}

/** Depth profiles: pressure down the y axis, one x axis per unit. */
function depthLine(rows, spec) {
  const depth = rows.map((r) => r[spec.axis.key]);
  const data = spec.series.map((s, i) => ({
    type: "scatter",
    mode: "lines+markers",
    name: axisTitle(s.label, s.unit),
    x: rows.map((r) => (r[s.key] === null || r[s.key] === undefined ? null : r[s.key])),
    y: depth,
    xaxis: i === 0 ? "x" : "x2",
    line: { color: s.colour, width: 2 },
    marker: { size: 5, color: s.colour },
    connectgaps: false,
    hovertemplate:
      `%{x} ${s.unit} at %{y} ${spec.axis.unit}<extra>${s.label}</extra>`,
  }));

  const layout = {
    ...baseLayout(),
    margin: { l: 75, r: 40, t: 60, b: 50 },
    yaxis: {
      title: { text: axisTitle(spec.axis.label, spec.axis.unit) },
      // This is the "depth increases downward" requirement.
      autorange: spec.axis.invert ? "reversed" : true,
      gridcolor: GRID, zeroline: false,
    },
    xaxis: seriesAxis(spec.series[0], {
      gridcolor: GRID, zeroline: false, side: "bottom",
    }),
    xaxis2: seriesAxis(spec.series[1], {
      overlaying: "x", side: "top", showgrid: false, zeroline: false,
    }),
    legend: { orientation: "h", y: -0.14, x: 0 },
  };
  return { data, layout };
}

/** Counts over time: shared x, one y axis per series. */
function timeLine(rows, spec) {
  const x = rows.map((r) => r[spec.axis.key]);
  const data = spec.series.map((s, i) => ({
    type: "scatter",
    mode: "lines+markers",
    name: s.label,
    x,
    y: rows.map((r) => (r[s.key] === null || r[s.key] === undefined ? null : r[s.key])),
    yaxis: i === 0 ? "y" : "y2",
    line: { color: s.colour, width: 2 },
    marker: { size: 5, color: s.colour },
    connectgaps: false,
    hovertemplate: `%{y} ${s.unit} · %{x|%b %Y}<extra>${s.label}</extra>`,
  }));

  const layout = {
    ...baseLayout(),
    xaxis: { title: { text: spec.axis.label }, type: "date", gridcolor: GRID },
    yaxis: seriesAxis(spec.series[0], { gridcolor: GRID, rangemode: "tozero" }),
    yaxis2: seriesAxis(spec.series[1], {
      overlaying: "y", side: "right", showgrid: false, rangemode: "tozero",
    }),
  };
  return { data, layout };
}

/**
 * Categories.  Temperature and salinity get a panel each: on one axis a 3 PSU
 * difference next to a 28 °C value is invisible, and the chart would hide the
 * comparison it exists to make.
 */
function bars(rows, spec) {
  const categories = rows.map((r) => r[spec.category.key]);
  const data = spec.series.map((s, i) => ({
    type: "bar",
    name: axisTitle(s.label, s.unit),
    x: categories,
    y: rows.map((r) => (r[s.key] === null || r[s.key] === undefined ? null : r[s.key])),
    xaxis: `x${i + 1}`,
    yaxis: `y${i + 1}`,
    marker: { color: s.colour },
    // The value is printed on the bar because the axis starts at zero and
    // stays there.  Salinity varies between 32 and 36 PSU, so zero-based bars
    // for two regions look identical -- which hides the exact comparison this
    // query exists to make.  Truncating the axis would make a 2.5 PSU gap look
    // enormous instead; a zero baseline plus the number is the honest pair.
    text: rows.map((r) => (r[s.key] === null || r[s.key] === undefined ? "" : r[s.key])),
    texttemplate: `%{text} ${s.unit}`,
    textposition: "outside",
    cliponaxis: false,
    hovertemplate: `%{y} ${s.unit}<br>%{x}<extra>${s.label}</extra>`,
    showlegend: false,
  }));

  const layout = {
    ...baseLayout(),
    showlegend: false,
    grid: { rows: 1, columns: spec.series.length, pattern: "independent" },
    margin: { l: 65, r: 30, t: 40, b: 90 },
  };
  spec.series.forEach((s, i) => {
    layout[`xaxis${i + 1}`] = { tickangle: -30, automargin: true };
    layout[`yaxis${i + 1}`] = {
      title: { text: axisTitle(s.label, s.unit) },
      gridcolor: GRID,
      // Zero baseline, always. Framing each panel around its own data would
      // make a 2.5 PSU gap look enormous; the printed value on each bar is
      // what carries the comparison instead. See the note on the labels above.
      rangemode: "tozero",
      automargin: true,
    };
  });
  return { data, layout };
}

function build(rows, spec) {
  if (spec.display === "bar") return bars(rows, spec);
  return spec.orientation === "depth" ? depthLine(rows, spec) : timeLine(rows, spec);
}

export default function PlotlyChart({ rows, spec, height = 460 }) {
  const node = useRef(null);

  useEffect(() => {
    const el = node.current;
    if (!el) return;
    const { data, layout } = build(rows, spec);
    Plotly.newPlot(el, data, { ...layout, height }, {
      displaylogo: false,
      responsive: true,
      modeBarButtonsToRemove: ["lasso2d", "select2d"],
    });
    return () => Plotly.purge(el);
  }, [rows, spec, height]);

  return <div ref={node} className="w-full rounded-lg border border-slate-200 bg-white" />;
}
