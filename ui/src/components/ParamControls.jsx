/**
 * Parameter controls, built entirely from /meta.
 *
 * Nothing here knows what a region is, which floats exist, or what the date
 * window is.  It knows six parameter *kinds*, and /meta tells it which kind
 * each parameter has and what values that kind allows.  Change the database
 * and these controls change with it.
 *
 * The one rule worth stating: an optional parameter left blank is NOT SENT.
 * It is not sent as its default, and the form does not pre-fill it.  The
 * catalogue binds the default server-side and reports it back, and that bound
 * value is what the audit panel displays.  If the form filled the default in,
 * the audit panel would only be echoing the form.
 */

import { cell } from "../format";

function Label({ param, children }) {
  return (
    <label className="block">
      <div className="flex items-baseline justify-between gap-2">
        <span className="font-mono text-xs font-medium text-slate-700">
          {param.name}
          {param.required && <span className="ml-0.5 text-red-500">*</span>}
        </span>
        {!param.required && (
          <span className="text-[11px] text-slate-400">
            default {param.default === null ? "none" : cell(param.default).text}
          </span>
        )}
      </div>
      {children}
      <p className="mt-1 text-[11px] leading-snug text-slate-500">{param.description}</p>
    </label>
  );
}

const inputClass =
  "mt-1 w-full rounded border border-slate-300 bg-white px-2 py-1.5 text-sm " +
  "focus:border-slate-500 focus:outline-none focus:ring-1 focus:ring-slate-400";

export default function ParamControls({ params, values, onChange, meta }) {
  if (params.length === 0) {
    return (
      <p className="rounded border border-dashed border-slate-300 px-3 py-4 text-center text-xs text-slate-500">
        This query takes no parameters.
      </p>
    );
  }

  return (
    <div className="space-y-4">
      {params.map((param) => (
        <Control
          key={param.name}
          param={param}
          value={values[param.name] ?? ""}
          onChange={(v) => onChange(param.name, v)}
          meta={meta}
        />
      ))}
      <p className="text-[11px] leading-snug text-slate-500">
        <span className="text-red-500">*</span> required. Leave anything else blank to let the
        catalogue apply its default — the audit panel reports the value it chose.
      </p>
    </div>
  );
}

function Control({ param, value, onChange, meta }) {
  // A choice list. /meta resolves region and wmo params to their live values,
  // so this branch does not need to know which kind produced the list.
  if (param.choices) {
    const isFloat = param.kind === "wmo";
    const byWmo = Object.fromEntries((meta?.floats ?? []).map((f) => [f.wmo, f]));
    const byRegion = Object.fromEntries((meta?.regions ?? []).map((r) => [r.name, r]));
    return (
      <Label param={param}>
        <select className={inputClass} value={value} onChange={(e) => onChange(e.target.value)}>
          <option value="">
            {param.required ? "— select —" : "— default —"}
          </option>
          {param.choices.map((choice) => {
            const f = byWmo[choice];
            const r = byRegion[choice];
            const suffix = isFloat && f
              ? ` · ${f.dac} · ${f.profiles} profiles`
              : r
                ? ` · ${r.profiles} profiles`
                : "";
            return (
              <option key={choice} value={choice}>
                {choice}{suffix}
              </option>
            );
          })}
        </select>
      </Label>
    );
  }

  if (param.kind === "date") {
    return (
      <Label param={param}>
        <input
          type="date"
          className={inputClass}
          value={value}
          min={param.minimum ?? undefined}
          max={param.maximum ?? undefined}
          onChange={(e) => onChange(e.target.value)}
        />
        {param.minimum && (
          <p className="mt-1 text-[11px] text-slate-400">
            The database covers {param.minimum} to {param.maximum}.
          </p>
        )}
      </Label>
    );
  }

  const isInt = param.kind === "int";
  return (
    <Label param={param}>
      <input
        type="number"
        className={inputClass}
        value={value}
        step={isInt ? 1 : "any"}
        min={param.minimum ?? undefined}
        max={param.maximum ?? undefined}
        placeholder={
          param.required
            ? `${param.minimum} to ${param.maximum}`
            : `${param.default} (default)`
        }
        onChange={(e) => onChange(e.target.value)}
      />
      {param.minimum !== null && (
        <p className="mt-1 text-[11px] text-slate-400">
          Accepted range {cell(param.minimum).text} to {cell(param.maximum).text}.
        </p>
      )}
    </Label>
  );
}

/**
 * Turn the form into a request body.
 *
 * Blank means absent, so the catalogue applies its own default.  This is the
 * mechanism the audit panel proves: `bin_dbar` left blank never appears here,
 * and comes back in the response's bound parameters as 50.
 */
export function toRequestParams(params, values) {
  const out = {};
  for (const param of params) {
    const raw = values[param.name];
    if (raw === undefined || raw === null || String(raw).trim() === "") continue;
    if (param.kind === "int") {
      const n = Number.parseInt(raw, 10);
      out[param.name] = Number.isNaN(n) ? raw : n;
    } else if (param.kind === "number") {
      const n = Number.parseFloat(raw);
      out[param.name] = Number.isNaN(n) ? raw : n;
    } else {
      out[param.name] = raw;
    }
  }
  return out;
}

/** Which required parameters are still blank. Used to disable Run. */
export function missingRequired(params, values) {
  return params
    .filter((p) => p.required)
    .filter((p) => {
      const v = values[p.name];
      return v === undefined || v === null || String(v).trim() === "";
    })
    .map((p) => p.name);
}
