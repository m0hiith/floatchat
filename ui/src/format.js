/**
 * Rendering values without inventing any.
 *
 * The rule this file exists to enforce: a missing value is rendered as missing.
 * `null` never becomes 0, or 0.0, or "N/A", or an empty string that a reader
 * might mistake for a measurement of nothing.  An aggregate over no rows
 * returns NULL from Postgres, and NULL is what the reader sees.
 */

export const ABSENT = "—";

export function isAbsent(v) {
  return v === null || v === undefined;
}

/** One cell. Returns { text, absent } so the caller can style, not guess. */
export function cell(value) {
  if (isAbsent(value)) return { text: ABSENT, absent: true };
  if (typeof value === "boolean") return { text: value ? "yes" : "no", absent: false };
  if (typeof value === "number") {
    const text = Number.isInteger(value)
      ? value.toLocaleString("en")
      : String(Number(value.toFixed(4)));
    return { text, absent: false };
  }
  const text = String(value);
  return { text, absent: text.length === 0 };
}

/** "1 row" / "412 rows" / "no rows". Never "0 rows". */
export function rowCount(n) {
  if (n === 0) return "no rows";
  return `${n.toLocaleString("en")} ${n === 1 ? "row" : "rows"}`;
}

/** Bound parameters, for the audit panel: `region="Arabian Sea", bin_dbar=50`. */
export function formatParams(params) {
  const entries = Object.entries(params ?? {});
  if (entries.length === 0) return "(no parameters)";
  return entries
    .map(([k, v]) => `${k}=${v === null ? "null" : typeof v === "string" ? `"${v}"` : v}`)
    .join(", ");
}

/** "2023-03-01" -> "March 2023". Returns the input unchanged if it is not a
 *  date, because a suggestion that reads "Invalid Date" is worse than one that
 *  reads the raw string. */
export function monthYear(iso) {
  const d = new Date(`${iso}T00:00:00Z`);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("en-GB", { month: "long", year: "numeric", timeZone: "UTC" });
}

export function timeOfDay(date = new Date()) {
  return date.toLocaleTimeString("en-GB", { hour12: false });
}
