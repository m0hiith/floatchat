/**
 * Leaflet, driven by the spec in displays.js.  Three modes:
 *
 *   points  a marker per profile
 *   path    markers joined in cycle order — a float's drift
 *   radius  markers plus the search centre and the circle that was searched
 *
 * The radius mode is drawn from the BOUND parameters rather than from the
 * rows, so a search that returns nothing still shows where you looked and how
 * far (requirement 4).  A blank panel would not tell you whether the query ran.
 *
 * Every marker carries the WMO id and the DAC, per the brief.
 */

import { useEffect, useRef } from "react";
import L from "leaflet";
import { DATA_MODES, COLUMN_LABELS } from "../displays";
import { cell } from "../format";

const TILES = "https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png";
const ATTRIBUTION =
  '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

function popupHtml(row, spec, ctx) {
  const title = spec.label ? spec.label(row, ctx) : "";
  const lines = (spec.tooltip ?? Object.keys(row))
    .filter((k) => k in row)
    .map((k) => {
      const { text, absent } = cell(row[k]);
      const label = COLUMN_LABELS[k] ?? k.replace(/_/g, " ");
      return `<tr><td style="color:#64748b;padding-right:.6em">${label}</td>
              <td style="font-variant-numeric:tabular-nums${absent ? ";color:#94a3b8" : ""}">${text}</td></tr>`;
    })
    .join("");
  return `<div style="font:12px ui-sans-serif,system-ui,sans-serif">
    ${title ? `<div style="font-weight:600;margin-bottom:.35em">${title}</div>` : ""}
    <table>${lines}</table></div>`;
}

/**
 * The two ends of a trajectory, drawn with no image file.
 *
 * `L.marker` with no `icon` uses Leaflet's default, which finds
 * `marker-icon.png` by reading the probe rule `.leaflet-default-icon-path` and,
 * failing that, by looking for a `<link href$="leaflet.css">`.  Under Vite
 * neither works: the stylesheet is emitted as `assets/index-<hash>.css`, and
 * the probe rule's background is inlined as a base64 data URI that Leaflet's
 * `/^(.*)marker-icon\.png$/` cannot strip.  The image path collapses to `""`,
 * the browser requests a bare `marker-icon.png`, and the only query that draws
 * these -- `float_trajectory` -- shows two broken images.  It survives
 * `npm run dev` and breaks in `npm run build`, which is the worse half.
 *
 * A `divIcon` is DOM, so no bundler and no file is involved, and it can say
 * "first" and "last" in words instead of dropping two identical pins.  Slate
 * chrome, deliberately: on this map colour already means `data_mode`, and a
 * marker in one of those colours would read as data.
 */
function endpointMarker(row, spec, label) {
  const icon = L.divIcon({
    className: "",
    html: `<div style="font:600 10px ui-sans-serif,system-ui,sans-serif;
                       background:#0f172a;color:#fff;border:1px solid #fff;
                       border-radius:9999px;padding:2px 7px;white-space:nowrap;
                       box-shadow:0 1px 3px rgba(0,0,0,.4)">${label}</div>`,
    iconSize: null,          // let the pill size itself to its text
    iconAnchor: [22, 24],    // sit above the point rather than on top of it
  });
  return L.marker([row[spec.lat], row[spec.lon]], { icon, title: `${label} cycle` })
    .bindPopup(`<b>${label} cycle</b><br>cycle ${row.cycle} · ${row.date}`);
}

function markerFor(row, spec, ctx) {
  const colour = spec.colorBy ? DATA_MODES[row[spec.colorBy]]?.colour ?? "#0ea5e9" : "#0ea5e9";
  return L.circleMarker([row[spec.lat], row[spec.lon]], {
    radius: 5,
    color: "#0f172a",
    weight: 1,
    fillColor: colour,
    fillOpacity: 0.85,
  }).bindPopup(popupHtml(row, spec, ctx));
}

export default function MapView({ rows, spec, bound, outlines, extent, context = {}, height = 460 }) {
  const node = useRef(null);
  const map = useRef(null);

  useEffect(() => {
    if (map.current || !node.current) return;
    map.current = L.map(node.current, { scrollWheelZoom: true, attributionControl: true });
    L.tileLayer(TILES, { attribution: ATTRIBUTION, maxZoom: 12 }).addTo(map.current);
    // The opening view comes from /meta's extent of the loaded profiles, not
    // from a coordinate typed into this file.
    if (extent) {
      map.current.fitBounds([
        [extent.min_lat, extent.min_lon],
        [extent.max_lat, extent.max_lon],
      ], { padding: [24, 24] });
    } else {
      map.current.setView([12, 72], 4);
    }
    return () => { map.current?.remove(); map.current = null; };
  }, [extent]);

  useEffect(() => {
    const m = map.current;
    if (!m) return;

    const layer = L.layerGroup().addTo(m);
    const focus = [];

    if (outlines) {
      L.geoJSON(outlines, {
        style: { color: "#475569", weight: 1, fillOpacity: 0.04, dashArray: "3 3" },
        onEachFeature: (f, l) => l.bindTooltip(f.properties.name, { sticky: true }),
      }).addTo(layer);
    }

    const points = rows
      .filter((r) => r[spec.lat] !== null && r[spec.lon] !== null)
      .map((r) => [r[spec.lat], r[spec.lon]]);

    if (spec.mode === "path" && points.length > 1) {
      const ordered = [...rows].sort((a, b) => a[spec.order] - b[spec.order]);
      L.polyline(ordered.map((r) => [r[spec.lat], r[spec.lon]]), {
        color: "#0f172a", weight: 1.5, opacity: 0.6,
      }).addTo(layer);
      endpointMarker(ordered[0], spec, "first").addTo(layer);
      endpointMarker(ordered[ordered.length - 1], spec, "last").addTo(layer);
    }

    // Drawn from the bound parameters, so it survives a zero-row result.
    if (spec.mode === "radius" && bound) {
      const c = spec.centreFrom;
      const lat = bound[c.lat];
      const lon = bound[c.lon];
      const km = bound[c.radiusKm];
      if (typeof lat === "number" && typeof lon === "number") {
        // Keep the circle we actually added: Leaflet projects a circle's
        // bounds through its map, so getBounds() on a detached one throws.
        const circle = L.circle([lat, lon], {
          radius: km * 1000,
          color: "#0369a1", weight: 1.5, fillColor: "#0ea5e9", fillOpacity: 0.06,
        }).addTo(layer);
        L.circleMarker([lat, lon], {
          radius: 6, color: "#0369a1", weight: 2, fillColor: "white", fillOpacity: 1,
        }).bindPopup(
          `<b>Search centre</b><br>${lat}°N, ${lon}°E<br>radius ${km} km`
        ).addTo(layer);
        focus.push(circle.getBounds());
      }
    }

    for (const row of rows) {
      if (row[spec.lat] === null || row[spec.lon] === null) continue;
      markerFor(row, spec, context).addTo(layer);
    }

    let bounds = points.length ? L.latLngBounds(points) : null;
    for (const b of focus) bounds = bounds ? bounds.extend(b) : b;
    if (bounds) m.fitBounds(bounds, { padding: [30, 30], maxZoom: 9 });

    m.invalidateSize();
    return () => layer.remove();
  }, [rows, spec, bound, outlines, context]);

  return (
    <div className="overflow-hidden rounded-lg border border-slate-200">
      <div ref={node} style={{ height }} className="w-full" />
      {spec.colorBy === "data_mode" && (
        <div className="flex flex-wrap items-center gap-4 border-t border-slate-200 bg-white px-3 py-2 text-xs text-slate-600">
          <span className="font-medium">Data mode</span>
          {Object.entries(DATA_MODES).map(([key, mode]) => (
            <span key={key} className="inline-flex items-center gap-1.5">
              <span className="h-2.5 w-2.5 rounded-full ring-1 ring-slate-900/40"
                    style={{ background: mode.colour }} />
              <span className="font-mono">{key}</span>
              <span className="text-slate-500">{mode.label}</span>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
