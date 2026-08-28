/**
 * How each of the eleven catalogue queries is drawn.
 *
 * This is the ONLY file in the UI that knows a query name.  Every component
 * below it takes a spec and some rows; none of them contains an `if (query ===
 * "depth_profile")`.  That is the point of writing the mapping down as data:
 * adding a twelfth query is an entry here, not a change to five components.
 *
 * Four display types, matching the four ways this data is worth looking at:
 *
 *   map    a row is a position          3 queries
 *   line   a row is a point on an axis  2 queries
 *   bar    a row is a category          2 queries
 *   table  a row is a record            4 queries
 *
 * Units are declared here, per series, and rendered onto the axis.  They are
 * never inferred from a column name at draw time -- `mean_psal_psu` is PSU
 * because this file says so, not because it ends in `_psu`.
 */

export const DISPLAYS = {
  // ---------------------------------------------------------------- maps
  profiles_in_region: {
    display: "map",
    mode: "points",
    lat: "lat",
    lon: "lon",
    id: "profile_id",
    // Required by the brief: a position on the map carries its WMO id and DAC.
    label: (r) => `${r.wmo} · ${r.dac}`,
    tooltip: ["profile_id", "wmo", "dac", "cycle", "date", "data_mode", "n_levels",
              "deepest_dbar"],
    colorBy: "data_mode",
    empty: "No profiles in that region and date range.",
  },

  float_trajectory: {
    display: "map",
    mode: "path",
    lat: "lat",
    lon: "lon",
    order: "cycle",
    label: (r, ctx) => `${ctx.wmo ?? ""} · cycle ${r.cycle}`,
    tooltip: ["cycle", "date", "lat", "lon", "region", "data_mode", "in_study_box"],
    colorBy: "data_mode",
    empty: "That float has no profiles in the database.",
  },

  nearest_profiles: {
    display: "map",
    mode: "radius",
    lat: "lat",
    lon: "lon",
    // The centre and the circle come from the BOUND parameters, so they are
    // drawn even when the search returns nothing -- an empty search still shows
    // you where you looked and how far.
    centreFrom: { lat: "lat", lon: "lon", radiusKm: "radius_km" },
    label: (r) => `${r.wmo} · ${r.km} km`,
    tooltip: ["profile_id", "wmo", "date", "region", "lat", "lon", "km"],
    empty: "No profiles within that radius.",
  },

  // --------------------------------------------------------------- lines
  depth_profile: {
    display: "line",
    orientation: "depth",
    // invert is what makes depth increase downward.  Declared once, here.
    axis: { key: "depth_bin_dbar", label: "Pressure", unit: "dbar", invert: true },
    series: [
      { key: "mean_temp_c", label: "Temperature", unit: "°C", axis: "x1", colour: "#dc2626" },
      { key: "mean_psal_psu", label: "Salinity", unit: "PSU", axis: "x2", colour: "#2563eb" },
    ],
    empty: "No levels in that region, date range and depth limit.",
  },

  monthly_profile_counts: {
    display: "line",
    orientation: "time",
    axis: { key: "month", label: "Month", unit: null, type: "date" },
    series: [
      { key: "profiles", label: "Profiles", unit: "count", axis: "y1", colour: "#0f766e" },
      { key: "floats", label: "Floats reporting", unit: "count", axis: "y2", colour: "#a16207" },
    ],
    empty: "No profiles in that region and date range.",
  },

  // ---------------------------------------------------------------- bars
  surface_conditions: {
    display: "bar",
    category: { key: "region", label: "Region" },
    series: [
      { key: "mean_temp_c", label: "Mean temperature", unit: "°C", colour: "#dc2626" },
      { key: "mean_psal_psu", label: "Mean salinity", unit: "PSU", colour: "#2563eb" },
    ],
    // Two quantities on wildly different scales; one panel each rather than
    // one axis that flattens temperature against salinity.
    facet: true,
    empty: "No near-surface levels in that date range.",
  },

  compare_regions: {
    display: "bar",
    category: { key: "region", label: "Region" },
    series: [
      { key: "mean_temp_c", label: "Mean temperature", unit: "°C", colour: "#dc2626" },
      { key: "mean_psal_psu", label: "Mean salinity", unit: "PSU", colour: "#2563eb" },
    ],
    facet: true,
    empty: "Neither region has levels in that depth band and date range.",
  },

  // -------------------------------------------------------------- tables
  region_summary: {
    display: "table",
    // One row of nine columns reads as a wide horizontal scroll.  Flipped to
    // label/value pairs down the page instead (D10.4).
    orient: "row",
    empty: "No profiles in that region and date range.",
  },

  float_inventory: {
    display: "table",
    wrap: ["selection_reason"],
    empty: "No floats in the database.",
  },

  data_provenance: {
    display: "table",
    empty: "That float has no profiles in the database.",
  },

  missing_profiles: {
    display: "table",
    wrap: ["detail"],
    empty: "No profiles were dropped — nothing was refused.",
  },
};

/** Column headers. Units belong in the header, not repeated in every cell. */
export const COLUMN_LABELS = {
  profile_id: "Profile", wmo: "WMO", dac: "DAC", cycle: "Cycle", date: "Date",
  lat: "Lat °N", lon: "Lon °E", data_mode: "Mode", n_levels: "Levels",
  deepest_dbar: "Deepest (dbar)", region: "Region", profiles: "Profiles",
  floats: "Floats", first_profile: "First", last_profile: "Last",
  delayed_mode: "Delayed (D)", realtime_adjusted: "Adjusted (A)", realtime: "Real-time (R)",
  month: "Month", depth_bin_dbar: "Pressure (dbar)", levels: "Levels",
  mean_temp_c: "Mean temp (°C)", mean_psal_psu: "Mean salinity (PSU)",
  sd_temp_c: "SD temp (°C)", km: "Distance (km)", in_study_box: "In study box",
  profiler_type: "Profiler", dm_status: "DM status", profiles_loaded: "Loaded",
  profiles_indexed: "Indexed", first: "First", last: "Last",
  selection_reason: "Why this float", psal_source: "Salinity source",
  mean_levels: "Mean levels", reason: "Reason", detail: "Detail",
  was_indexed: "Was indexed",
};

/** ARGO data modes, spelled out. R/A/D is not self-explanatory to a judge. */
export const DATA_MODES = {
  R: { label: "real-time", colour: "#f59e0b" },
  A: { label: "real-time, adjusted", colour: "#8b5cf6" },
  D: { label: "delayed-mode", colour: "#059669" },
};

export function displayFor(queryName) {
  return DISPLAYS[queryName] ?? null;
}

/**
 * Example questions for the chat box.
 *
 * The wording is presentation, which is why it lives here — but the VALUES are
 * not.  Each suggestion is filled from the example the catalogue publishes for
 * that query in /meta, so no region name, float id or date is written into the
 * browser.  Point this dashboard at a different database and the suggestions
 * name that database's regions.  It also means a suggestion cannot propose a
 * question the data cannot answer: the example it is built from is the one the
 * query is tested against.
 *
 * The last one is deliberate and it is meant to fail.  The problem this
 * project answers asks for biogeochemical comparisons; these ten floats carry
 * none, and the honest refusal is worth demonstrating on purpose rather than
 * hoping nobody asks.
 */
export const SUGGESTIONS = [
  { from: "compare_regions", ask: (e) => `Is the ${e.region_a} fresher than the ${e.region_b}?` },
  { from: "profiles_in_region", ask: (e, f) => `Show me salinity profiles in the ${e.region} in ${f.monthYear(e.start)}` },
  { from: "nearest_profiles", ask: (e) => `Which ARGO floats are nearest to ${e.lat}°N, ${e.lon}°E?` },
  { from: "depth_profile", ask: (e) => `Plot temperature against depth in the ${e.region}` },
  { from: "missing_profiles", ask: (e) => `Why does float ${e.wmo} have fewer profiles than the index promised?` },
  { from: "region_summary", ask: (e) => `Show me the BGC oxygen profiles for the ${e.region}` },
];

export function suggestionsFor(meta, helpers) {
  return SUGGESTIONS.map(({ from, ask }) => {
    const query = meta?.queries?.find((q) => q.name === from);
    if (!query?.example || Object.keys(query.example).length === 0) return null;
    try {
      return { from, text: ask(query.example, helpers) };
    } catch {
      // A catalogue whose example lost a key drops that suggestion rather than
      // rendering "undefined" into a question someone might click.
      return null;
    }
  }).filter(Boolean);
}
