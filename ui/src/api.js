/**
 * The only place the UI talks to the network.
 *
 * Four failure modes, kept apart on purpose, because the dashboard has to
 * render them differently (there were three until the second 503 arrived):
 *
 *   unreachable  the fetch itself failed -- wrong port, server not started.
 *                We name the URL we tried.  This must never look like an
 *                empty database (requirement 3).
 *   unavailable  503: the API is up, Postgres is not.  Carries psycopg's own
 *                reason and the host it tried.
 *   refused      400: the catalogue rejected a parameter.  The message always
 *                names the valid values, so the UI shows it verbatim and
 *                composes nothing of its own.
 *   no-model     503, but a different 503: the API and the database are both
 *                up and there is no usable model, or the key was rejected.
 *                Kept apart from `unavailable` because they are fixed in
 *                different places -- one is `uvicorn`, the other is an
 *                environment variable -- and telling a user to restart
 *                Postgres because their API key expired is a wrong answer
 *                delivered confidently.
 */

export const API_BASE =
  import.meta.env.VITE_API_BASE ?? "http://localhost:8000";

export class ApiError extends Error {
  constructor(kind, message, extra = {}) {
    super(message);
    // Every kind this module throws. `no-model` was added with the second 503
    // and never reached this list; ui/test_ui.py now compares the two.
    this.kind = kind;  // "unreachable" | "unavailable" | "no-model" | "refused" | "bad"
    Object.assign(this, extra);
  }
}

async function request(path, options) {
  const url = `${API_BASE}${path}`;
  let response;
  try {
    response = await fetch(url, options);
  } catch (cause) {
    throw new ApiError(
      "unreachable",
      `Cannot reach the FloatChat API at ${API_BASE}`,
      { url, detail: String(cause?.message ?? cause), cause },
    );
  }

  let body = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }

  if (response.ok) return body;

  if (response.status === 503) {
    // The server labels its own 503s. Two platform outages, two messages.
    if (body?.error === "model unavailable") {
      throw new ApiError("no-model", "The API is up. The language model is not.", {
        url,
        detail: body?.detail ?? `HTTP ${response.status}`,
      });
    }
    throw new ApiError("unavailable", "The API is running, but the database is not.", {
      url,
      detail: body?.detail ?? `HTTP ${response.status}`,
      dsn: body?.dsn ?? null,
    });
  }
  if (response.status === 400) {
    // The catalogue's message, unedited.  See api/catalog.py Param.coerce.
    throw new ApiError("refused", body?.detail ?? "The query was refused.", { url });
  }
  throw new ApiError("bad", `The API returned HTTP ${response.status}.`, {
    url,
    detail: body?.detail ?? response.statusText,
  });
}

export const getMeta = () => request("/meta");
export const getRegionOutlines = () => request("/regions.geojson");

export const runQuery = (name, params) =>
  request("/query", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, params }),
  });

/**
 * A question in English.  Returns the answer, what was retrieved, and the
 * audit trail WITH the rows -- so the chat panel draws its charts from the
 * same rows and the same displays.js mapping the manual panel uses.  There is
 * no second path into the database and no second way to draw one.
 */
export const ask = (question, { retrieval = true, provider = null } = {}) =>
  request("/ask", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, retrieval, provider }),
  });
