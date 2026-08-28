/**
 * Every way this dashboard can fail to show a chart, rendered as itself.
 *
 * The brief: "A refusal renders as a refusal: the message, and what IS
 * available. Never an empty chart, never a spinner that stops."
 */

export function Spinner({ label = "Loading" }) {
  return (
    <div className="flex items-center gap-3 p-6 text-slate-500">
      <span className="h-4 w-4 animate-spin rounded-full border-2 border-slate-300 border-t-slate-600" />
      <span className="text-sm">{label}…</span>
    </div>
  );
}

function Panel({ tone, title, children }) {
  const tones = {
    red: "border-red-300 bg-red-50 text-red-900",
    amber: "border-amber-300 bg-amber-50 text-amber-900",
    slate: "border-slate-300 bg-slate-50 text-slate-700",
  };
  return (
    <div className={`rounded-lg border p-4 ${tones[tone]}`}>
      <div className="text-sm font-semibold">{title}</div>
      <div className="mt-2 space-y-2 text-sm">{children}</div>
    </div>
  );
}

/**
 * The API could not be reached at all, or reached but its database is down.
 * These are shown instead of the dropdowns, never alongside empty ones -- a
 * database with no regions and an API that is down must not look the same.
 */
export function ApiFailure({ error, onRetry }) {
  const unreachable = error.kind === "unreachable";
  return (
    <Panel tone="red" title={unreachable ? "The API is not reachable" : "The database is not reachable"}>
      <p>{error.message}</p>
      {error.detail && (
        <pre className="overflow-x-auto rounded border border-red-200 bg-white/70 p-2 font-mono text-xs whitespace-pre-wrap">
          {error.detail}
        </pre>
      )}
      {error.dsn && <p className="text-xs">Tried: <code className="font-mono">{error.dsn}</code></p>}
      <div className="rounded border border-red-200 bg-white/70 p-2 text-xs">
        <p className="font-semibold">This is not an empty database.</p>
        <p className="mt-1">
          No dropdowns are shown because nothing was loaded — not because the
          database holds no regions or floats.
        </p>
      </div>
      {unreachable && (
        <div className="text-xs">
          <p className="font-semibold">Start it with:</p>
          <pre className="mt-1 overflow-x-auto rounded border border-red-200 bg-white/70 p-2 font-mono">
            .venv/bin/uvicorn api.server:app --port 8000
          </pre>
        </div>
      )}
      {onRetry && (
        <button
          onClick={onRetry}
          className="rounded border border-red-400 bg-white px-3 py-1 text-xs font-medium hover:bg-red-100"
        >
          Try again
        </button>
      )}
    </Panel>
  );
}

/**
 * The catalogue refused a parameter.  `error.message` is the catalogue's own
 * sentence and it already names the valid values -- it is shown verbatim and
 * split out below so the alternatives are readable rather than a long line.
 */
export function Refusal({ error }) {
  const message = error.message ?? "";
  const [head, tail] = splitOnAlternatives(message);
  return (
    <Panel tone="amber" title="The query was refused">
      <p className="font-mono text-xs">{head}</p>
      {tail && (
        <div>
          <div className="text-xs font-semibold uppercase tracking-wide">What is available</div>
          <ul className="mt-1 flex flex-wrap gap-1.5">
            {tail.map((v) => (
              <li key={v} className="rounded bg-white px-2 py-0.5 font-mono text-xs ring-1 ring-amber-300">
                {v}
              </li>
            ))}
          </ul>
        </div>
      )}
      <p className="text-xs opacity-80">
        No query ran. The refusal came from the catalogue before any SQL was bound.
      </p>
    </Panel>
  );
}

/**
 * The catalogue's refusals list alternatives after a known phrase.  Splitting
 * on it is presentation only: if the phrase is absent the whole message is
 * still shown, so a new refusal wording degrades to plain text, never to a
 * blank panel.
 */
function splitOnAlternatives(message) {
  const markers = ["Valid regions:", "Valid floats:", "Available:", "Accepted:", "expected one of"];
  for (const marker of markers) {
    const at = message.indexOf(marker);
    if (at !== -1) {
      const list = message
        .slice(at + marker.length)
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
      if (list.length) return [message.slice(0, at + marker.length), list];
    }
  }
  return [message, null];
}

/** The query ran and returned nothing. A success, and it says so. */
export function NoRows({ note, children }) {
  return (
    <div className="rounded-lg border border-slate-300 bg-white p-6">
      <div className="text-sm font-semibold text-slate-700">no rows</div>
      <p className="mt-1 text-sm text-slate-500">{note}</p>
      <p className="mt-3 text-xs text-slate-400">
        The query ran and the database returned nothing. This is a result, not an error —
        see the audit panel for what was bound.
      </p>
      {children}
    </div>
  );
}

export function Idle({ children }) {
  return (
    <div className="rounded-lg border border-dashed border-slate-300 bg-white p-10 text-center">
      <p className="text-sm text-slate-500">{children}</p>
    </div>
  );
}

/**
 * A rendering bug in one display must not blank the page.
 *
 * Found the hard way: Leaflet throws if you ask a detached circle for its
 * bounds, and that exception unmounted the entire dashboard — audit panel,
 * dropdowns and all.  A white screen is the one thing this UI must never do,
 * because it is indistinguishable from every other kind of failure.
 */
import { Component } from "react";

export class DisplayBoundary extends Component {
  state = { error: null };

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidUpdate(prev) {
    // A new result gets a fresh attempt; the old error must not stick.
    if (prev.resetKey !== this.props.resetKey && this.state.error) {
      this.setState({ error: null });
    }
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <Panel tone="red" title="This result could not be drawn">
        <p>
          The query ran and returned rows — the failure is in this browser, not in the
          database. The audit panel still shows what was bound and how many rows came back.
        </p>
        <pre className="overflow-x-auto rounded border border-red-200 bg-white/70 p-2 font-mono text-xs whitespace-pre-wrap">
          {String(this.state.error?.message ?? this.state.error)}
        </pre>
      </Panel>
    );
  }
}

/**
 * There is no model to answer with.
 *
 * A separate state from `ApiFailure` on purpose.  The API is up, the database
 * is up, and the eleven queries in the other tab all still work — telling
 * someone their database is down because their API key expired would send
 * them to fix the wrong thing.  The server's own diagnosis is shown verbatim;
 * it already names the variable to set (api/chat.py diagnose_provider_error).
 */
export function ModelFailure({ error, onSwitchToCatalogue, onRetryLexical }) {
  return (
    <Panel tone="amber" title="No model answered">
      <p>{error.message}</p>
      {error.detail && (
        <pre className="overflow-x-auto rounded border border-amber-300 bg-white/70 p-2 font-mono text-xs whitespace-pre-wrap">
          {error.detail}
        </pre>
      )}
      <div className="rounded border border-amber-300 bg-white/70 p-2 text-xs">
        <p className="font-semibold">The data is fine. The model is not.</p>
        <p className="mt-1">
          Nothing on this dashboard needs a key. The lexical router answers the same
          question by matching your wording against written examples — no model — and every
          catalogue query still runs from the other tab.
        </p>
      </div>
      <div className="flex flex-wrap gap-2">
        {/* The primary action is the path that works, not a consolation prize.
            Asking again is one explicit click: the request above is NOT
            re-answered behind the reader's back (D12.12). */}
        {onRetryLexical && (
          <button
            onClick={onRetryLexical}
            className="rounded bg-slate-800 px-3 py-1 text-xs font-medium text-white hover:bg-slate-700"
          >
            Ask this again without a model
          </button>
        )}
        {onSwitchToCatalogue && (
          <button
            onClick={onSwitchToCatalogue}
            className="rounded border border-amber-400 bg-white px-3 py-1 text-xs font-medium hover:bg-amber-100"
          >
            Use the query catalogue instead
          </button>
        )}
      </div>
    </Panel>
  );
}
