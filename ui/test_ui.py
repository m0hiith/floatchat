#!/usr/bin/env python
"""Stage 13 checks: the dashboard's claims, re-checked on every run.

The UI was the one surface with nothing asserting anything about it, and it is
where the silent failures were: an axis colour that Plotly 4 drops without a
warning, two map markers that 404 only in a production build, a tab badge that
called a configured model "no model".  All three looked fine.  That is the
whole argument for this file (rule 5 -- a claim is only true if something
re-checks it).

These are source checks, not a browser.  They read `ui/src/*.js*` as text and
`api/catalog.py` as a module, so this suite needs **no Postgres, no network, no
API key and no npm** -- it runs on a fresh clone with nothing installed, which
is the same claim Stage 12's suite makes.  What it cannot see is whether a
chart looks right; what it can see is every invariant that has already been
broken once here.

    .venv/bin/python ui/test_ui.py
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "ui" / "src"
sys.path.insert(0, str(ROOT / "api"))

import catalog                                                   # noqa: E402

passed = failed = 0


def check(label: str, ok: bool, detail: str = ""):
    global passed, failed
    passed, failed = passed + ok, failed + (not ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail else ''}")


def read(name: str) -> str:
    return (SRC / name).read_text(encoding="utf-8")


def sources() -> dict[str, str]:
    return {str(p.relative_to(SRC)): p.read_text(encoding="utf-8")
            for p in sorted(SRC.rglob("*")) if p.suffix in {".js", ".jsx"}}


def strip_comments(text: str) -> str:
    """Code only.  A query name inside a `/* ... */` explaining why a table is
    flipped is documentation, not knowledge the component acts on, and the
    checks below are about what the code does."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"^\s*//.*$", "", text, flags=re.M)


def js_object_keys(text: str, name: str) -> list[str]:
    """Top-level keys of `export const <name> = { ... }`.

    Deliberately shallow: it counts brace depth from the opening brace and
    takes identifiers at depth 1 only, so nested specs do not leak in.
    """
    start = text.index(f"{name} = {{") + len(f"{name} = ")
    depth, i, keys = 0, start, []
    while i < len(text):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                break
        elif depth == 1:
            m = re.match(r'\s*["\']?([A-Za-z_][\w]*)["\']?\s*:', text[i:])
            if m and (text[i - 1] in "{,\n " or text[i - 1] == "\n"):
                keys.append(m.group(1))
                i += m.end() - 1
        i += 1
    return keys


def main():
    print(f"reading      : {SRC.relative_to(ROOT)}  ({len(sources())} files)")
    print(f"catalogue    : {len(catalog.QUERIES)} queries, read from api/catalog.py")
    print("needs        : no database, no network, no key, no npm\n")

    displays_src = read("displays.js")
    plotly_src = read("components/PlotlyChart.jsx")
    map_src = read("components/MapView.jsx")
    api_src = read("api.js")

    names = {q.name for q in catalog.QUERIES}
    drawn = set(js_object_keys(displays_src, "DISPLAYS"))

    # ---------------------------------------------------------------- displays
    print("every catalogue query has a declared display")
    check("DISPLAYS parsed", len(drawn) > 0, f"{len(drawn)} entries")
    check("no query is undrawable", not (names - drawn),
          f"missing: {sorted(names - drawn)}" if names - drawn else "all 11 drawn")
    check("no display for a query that does not exist", not (drawn - names),
          f"stale: {sorted(drawn - names)}" if drawn - names else "no stale entries")

    print("\ndisplays.js is the only file in the UI that knows a query name")
    for path, text in sources().items():
        if path == "displays.js":
            continue
        found = sorted(n for n in names if n in strip_comments(text))
        check(f"{path:<28}", not found, ", ".join(found) if found else "names none")

    # ------------------------------------------------------------------ plotly
    # Plotly 4 removed the top-level `titlefont`; the attribute is now dropped
    # in silence and the axis keeps its title without its colour.
    # Comments are stripped first: the comment explaining why `titlefont` is
    # forbidden has to be free to name it. Only code is being asserted about.
    print("\nchart axes use attributes this Plotly still has")
    code = "".join(strip_comments(t) for t in sources().values())
    check("no `titlefont` in any UI code", "titlefont" not in code,
          "removed in Plotly 4 -- axis title font lives in title.font")
    check("axis titles carry a font", "font: { color: series.colour }" in plotly_src)
    check("one place builds a coloured axis", plotly_src.count("tickfont") == 1,
          f"{plotly_src.count('tickfont')} occurrence(s) -- expected only seriesAxis()")
    for axis in ("xaxis: seriesAxis", "xaxis2: seriesAxis",
                 "yaxis: seriesAxis", "yaxis2: seriesAxis"):
        check(f"{axis:<26}", axis in plotly_src)

    pinned = (ROOT / "ui" / "package.json").read_text(encoding="utf-8")
    check("plotly is pinned exactly", '"plotly.js-dist-min": "4.' in pinned,
          "the titlefont checks above are a claim about version 4")

    # ------------------------------------------------------------------ leaflet
    # Leaflet's default icon resolves marker-icon.png from the document; under
    # Vite it resolves to nothing and 404s, and only in a production build.
    print("\nthe map depends on no image file")
    marker_calls = re.findall(r"L\.marker\([^;]*?\{[^}]*\}", map_src, flags=re.S)
    check("every L.marker sets its own icon",
          bool(marker_calls) and all("icon" in c for c in marker_calls),
          f"{len(marker_calls)} call(s)")
    check("the endpoint icon is DOM, not a file", "L.divIcon" in map_src)
    check("no marker image is named in code", "marker-icon" not in strip_comments(map_src),
          "the comment explaining the 404 is free to name it")

    # -------------------------------------------------------------------- units
    print("\nunits are declared, never inferred")
    for unit in ("dbar", "PSU", "°C"):
        check(f"{unit:<26}", unit in displays_src)
    check("pressure increases downward", "invert: true" in displays_src)
    check("DATA_MODE has three states", set(js_object_keys(displays_src, "DATA_MODES")) ==
          {"R", "A", "D"}, "R real-time, A adjusted, D delayed-mode")

    # --------------------------------------------------------------- api errors
    print("\napi.js documents every failure kind it throws")
    thrown = set(re.findall(r'new ApiError\(\s*"([\w-]+)"', api_src))
    documented = set(re.findall(r'"([\w-]+)"', api_src.split("this.kind = kind;")[1]
                                .split("\n")[0])) if "this.kind = kind;" in api_src else set()
    check("kinds are listed on the field", bool(documented), f"listed: {sorted(documented)}")
    check("no kind is thrown but undocumented", not (thrown - documented),
          f"undocumented: {sorted(thrown - documented)}" if thrown - documented
          else f"{len(thrown)} kinds, all listed")
    check("no kind is documented but never thrown", not (documented - thrown),
          f"stale: {sorted(documented - thrown)}" if documented - thrown else "none stale")

    # -------------------------------------------------------------- suggestions
    print("\nthe chat suggestions name real queries and invent no values")
    suggested = set(re.findall(r'from:\s*"(\w+)"', displays_src))
    check("every suggestion names a real query", not (suggested - names),
          f"unknown: {sorted(suggested - names)}" if suggested - names
          else f"{len(suggested)} suggestions")
    by_name = {q.name: q for q in catalog.QUERIES}
    check("every suggestion has an example to fill it from",
          all(by_name[s].example for s in suggested),
          "a query with example={} would render 'undefined' into a question")
    check("the deliberate BGC refusal is still offered",
          "BGC" in displays_src or "oxygen" in displays_src,
          "these floats carry none; the honest refusal is demonstrated on purpose")

    # ------------------------------------------------------------- one engine
    # Until Stage 16 this section checked that a THREE-state badge could not
    # lie about which of two engines was selected. There is one engine now
    # (D16.8), so the properties to hold are different: the dashboard must ask
    # for it by name on every call, and nothing in the UI may offer the path it
    # no longer takes.
    print("\nthe dashboard asks one engine, and names it")
    app_src = read("App.jsx")
    chat_src = read("components/ChatPanel.jsx")
    check('every question is sent with provider "lexical"',
          'provider: "lexical"' in chat_src,
          "sent explicitly, so a key in the API's environment cannot change the engine")
    check("and with retrieval switched off in the request",
          "retrieval: false" in chat_src)
    check("exactly one call site sends a question",
          chat_src.count("await ask(") == 1,
          f"{chat_src.count('await ask(')} call site(s) -- a second one is a fallback")
    named = sorted(p for p in ('"gemini"', '"anthropic"')
                   for t in sources().values() if p in strip_comments(t))
    check("no UI code names a model provider", not named,
          ", ".join(named) if named else "the model path is the API's and the CLI's")
    check("App holds no engine selection state",
          "chatPath" not in app_src and "usingModel" not in app_src,
          "a selector with one option is a choice the dashboard does not offer")
    check("the chat tab's availability reads the ROUTER",
          "ai.router?.available" in app_src,
          "`ai.available` is true when EITHER path can answer, model included")
    check("the reply badge reads the provider the API reported",
          'provider === "lexical"' in chat_src)

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
