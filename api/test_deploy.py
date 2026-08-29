#!/usr/bin/env python
"""Stage 15 checks: the deployment configuration, re-checked on every run.

Deploying this project introduced four files and two environment seams, and
every one of them fails quietly if it is wrong.  A `.vercelignore` missing a
line uploads 380 MB.  A `.vercelignore` with one line too many ships an API
whose retrieval is off and which says so only in `/meta`.  A slim requirements
file that has drifted from the root freeze installs a different Plotly-shaped
problem on the server.  A `FLOATCHAT_ORIGINS` with a stray `*` is a permissive
API that looks configured.  None of those raise; all of them are properties;
so all of them are asserted here (rule 5).

Like Stage 12's and Stage 13's suites this needs **no Postgres, no network and
no API key** -- it reads configuration as text, computes the upload set from
the working directory, and inspects installed metadata.  What it cannot check
is anything Vercel decides on its own machine: the Python version the build
selects, whether a wheel exists for it, or what an environment variable
actually contains in production.  Those limits are D15.10.

    .venv/bin/python api/test_deploy.py
"""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "api"))

passed = failed = 0


def check(label: str, ok: bool, detail: str = ""):
    global passed, failed
    passed, failed = passed + ok, failed + (not ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail else ''}")


def pins(path: Path) -> dict[str, str]:
    """name -> version, from a pip freeze.  Comments and blanks ignored."""
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, _, version = line.partition("==")
        out[name.strip()] = version.strip()
    return out


def canon(name: str) -> str:
    return name.lower().replace("_", "-")


# --------------------------------------------------------------------------
# the upload set -- what `vercel` would actually send
# --------------------------------------------------------------------------

# The patterns this suite understands.  If .vercelignore ever grows a pattern
# outside this grammar the matcher would silently under-match and the size
# check would pass while shipping the thing it was meant to catch, so an
# unknown pattern is a FAILURE here rather than a quiet approximation.
def parse_ignore(text: str) -> tuple[list[str], list[str], list[str], list[str]]:
    """The same grammar for .vercelignore and for a .gitignore acting as one.

    It parses `ui/.gitignore` as well since Stage 17, because the dashboard is
    deployed from `ui/` and has deliberately no `.vercelignore` -- so the
    filter keeping a `VERCEL_OIDC_TOKEN` out of that bundle IS that .gitignore
    (D17.7).  Negations are honoured rather than treated as plain names: a
    `.env*` / `!.env.example` pair means something different from either line
    alone, and a parser that dropped the `!` would model the wrong upload.
    """
    plain, suffixes, prefixes, negated, unknown = [], [], [], [], []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("!"):
            negated.append(line[1:].rstrip("/"))
            continue
        if "/" in line.rstrip("/") or line.startswith("/"):
            unknown.append(line) if "*" in line else plain.append(line.rstrip("/"))
        elif line.startswith("*") and line.count("*") == 1:
            suffixes.append(line[1:])
        elif line.endswith("*") and line.count("*") == 1:
            prefixes.append(line[:-1])
        elif "*" not in line:
            plain.append(line.rstrip("/"))
        else:
            unknown.append(line)
    return plain + [f"?{u}" for u in unknown], suffixes, prefixes, negated


def upload_set(plain: list[str], suffixes: list[str], prefixes: list[str],
               negated: list[str] = (), root: Path = ROOT) -> list[Path]:
    """Every file the CLI would upload, as paths relative to the deploy root."""
    ignored, kept = set(plain), set(negated)
    files = []
    for path in root.rglob("*"):
        if path.is_dir():
            continue
        rel = path.relative_to(root)
        parts = set(rel.parts) | {str(Path(*rel.parts[:i + 1]))
                                  for i in range(len(rel.parts))}
        if parts & kept:                      # a `!` line wins over the rest
            files.append(rel)
            continue
        if parts & ignored:
            continue
        if any(n.endswith(s) for s in suffixes for n in rel.parts):
            continue
        if any(n.startswith(p) for p in prefixes for n in rel.parts):
            continue
        files.append(rel)
    return files


def git_visible(*paths: str) -> set[str]:
    """Every path a `git push` would carry: tracked, plus untracked-not-ignored.

    Computed from git rather than read off `.gitignore`, for D15.4's reason in
    a second setting -- a grep tests how the ignore file is written, this tests
    what would actually be sent (D17.2).
    """
    out = subprocess.run(["git", "ls-files", "--cached", "--others",
                          "--exclude-standard", "--", *paths],
                         capture_output=True, text=True, cwd=ROOT)
    return {line for line in out.stdout.splitlines() if line.strip()}


ENV_READ = re.compile(
    r"""os\.environ\.get\(\s*["']([A-Z][A-Z0-9_]*)["']"""
    r"""|os\.getenv\(\s*["']([A-Z][A-Z0-9_]*)["']"""
    r"""|os\.environ\[\s*["']([A-Z][A-Z0-9_]*)["']""")

VITE_READ = re.compile(r"import\.meta\.env\.(VITE_[A-Z0-9_]*)")

# A value that looks like a real credential rather than a placeholder.  An
# example file is committed, so this is the line between documentation and
# D15.12 happening again by hand.
SECRETISH = re.compile(
    r"sk-[A-Za-z0-9_\-]{8,}|AIza[0-9A-Za-z_\-]{10,}"
    r"|eyJ[A-Za-z0-9_\-]{10,}|\b[0-9a-f]{32,}\b")


def env_names(path: Path) -> dict[str, str]:
    """name -> value, from a dotenv-style file.  Comments and blanks ignored."""
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        out[name.strip()] = value.strip()
    return out


def main() -> None:
    # ------------------------------------------------- the two freeze files
    print("api/requirements.txt is a subset of the root freeze")
    root_pins = pins(ROOT / "requirements.txt")
    api_pins = pins(ROOT / "api" / "requirements.txt")
    root_by_key = {canon(n): (n, v) for n, v in root_pins.items()}

    missing = [n for n in api_pins if canon(n) not in root_by_key]
    check("every API pin exists in the root freeze", not missing,
          f"absent from requirements.txt: {missing}" if missing
          else f"{len(api_pins)} pins")
    drifted = [f"{n}: api={v} root={root_by_key[canon(n)][1]}"
               for n, v in api_pins.items()
               if canon(n) in root_by_key and root_by_key[canon(n)][1] != v]
    check("no version disagrees with the root freeze", not drifted, str(drifted))
    check("every API line is pinned with ==", all(api_pins.values()),
          str([n for n, v in api_pins.items() if not v]))
    check("the API freeze is smaller than the root one",
          len(api_pins) < len(root_pins),
          f"{len(api_pins)} of {len(root_pins)}")

    # The property, not the spelling: it is not that netCDF4 is absent by name,
    # it is that nothing the API imports has been dropped.
    print("\nnothing under api/ imports a package the API freeze drops")
    import importlib.metadata as md
    mod_to_dist = md.packages_distributions()
    local = {p.stem for p in (ROOT / "api").glob("*.py")}
    api_keys = {canon(n) for n in api_pins}

    unsatisfied: dict[str, set[str]] = {}
    for src in sorted((ROOT / "api").glob("*.py")):
        if src.name.startswith("test_"):
            continue
        for line in src.read_text().splitlines():
            line = line.strip()
            if line.startswith("import "):
                mod = line[len("import "):].split()[0].split(".")[0].rstrip(",")
            elif line.startswith("from ") and " import " in line:
                mod = line[len("from "):].split()[0].split(".")[0]
            else:
                continue
            if mod in local or mod in sys.stdlib_module_names or mod == "__future__":
                continue
            dists = {canon(d) for d in mod_to_dist.get(mod, [])}
            if dists and not (dists & api_keys):
                unsatisfied.setdefault(src.name, set()).add(f"{mod} ({sorted(dists)})")
    check("every third-party import in api/ is covered", not unsatisfied,
          str({k: sorted(v) for k, v in unsatisfied.items()}))

    # And the transitive closure, so pip is never left to resolve a version.
    from packaging.requirements import Requirement
    incomplete = []
    for name in api_pins:
        try:
            dist = md.distribution(name)
        except md.PackageNotFoundError:
            continue                      # not installed here; nothing to check
        for raw in dist.requires or []:
            req = Requirement(raw)
            if req.marker and not req.marker.evaluate({"extra": ""}):
                continue
            if canon(req.name) not in api_keys:
                incomplete.append(f"{name} -> {req.name}")
    check("the dependency closure is complete", not incomplete,
          str(incomplete[:6]))

    # ------------------------------------------------------- the upload set
    print("\n.vercelignore, applied to this working directory")
    ignore_path = ROOT / ".vercelignore"
    check(".vercelignore exists", ignore_path.exists(),
          "without it the CLI falls back to .gitignore, which excludes data/")
    plain, suffixes, prefixes, negated = parse_ignore(ignore_path.read_text())
    unknown = [p[1:] for p in plain if p.startswith("?")]
    check("every pattern is one this suite can evaluate", not unknown,
          f"unmatchable, so the size check below would under-report: {unknown}")
    plain = [p for p in plain if not p.startswith("?")]

    files = upload_set(plain, suffixes, prefixes, negated)
    total = sum((ROOT / f).stat().st_size for f in files)
    check("the upload is under 20 MB", total < 20 * 1024 * 1024,
          f"{total / 1e6:.1f} MB across {len(files)} files")

    shipped = {str(f) for f in files}
    for needed in ("data/rag/index.faiss", "data/rag/manifest.json"):
        check(f"{needed} ships", needed in shipped,
              "api/retrieval.py reads it; without it /ask reports no index")
    for excluded in (".venv", "ui/node_modules", "data/profiles", "data/index",
                     "data/parsed", "data/regions"):
        hit = [f for f in shipped if f.startswith(excluded + "/")]
        check(f"{excluded} does not ship", not hit, f"{len(hit)} files")

    # `vercel link` writes .env.local next to the code, and it holds a token.
    # Nothing warns that it is about to be uploaded (D15.12).
    secrets = sorted(f for f in shipped
                     if Path(f).name.startswith(".env") or Path(f).name == "project.json")
    check("no credential file is in the upload set", not secrets, str(secrets))

    # --------------------------------------------------------- vercel.json
    print("\nvercel.json publishes one endpoint, not thirteen")
    conf = json.loads((ROOT / "vercel.json").read_text())
    builds = conf.get("builds", [])
    check("a builds array is present", bool(builds),
          "without it Vercel makes a function of every .py under api/")
    check("it names exactly one entrypoint", len(builds) == 1,
          str([b.get("src") for b in builds]))
    check("and that entrypoint is api/index.py",
          bool(builds) and builds[0].get("src") == "api/index.py")
    include = builds[0].get("config", {}).get("includeFiles", "") if builds else ""
    check("the index directory is included in the bundle",
          "data/rag" in str(include), str(include))
    routes = conf.get("routes", [])
    check("every route reaches that entrypoint",
          bool(routes) and all(r.get("dest") == "api/index.py" for r in routes),
          str(routes))
    # The hazard the builds array exists to prevent is still real.
    tests = sorted(p.name for p in (ROOT / "api").glob("test_*.py"))
    check("the suites that would otherwise be published are still here",
          len(tests) >= 6, f"{len(tests)}: zero-config would route each one")

    # -------------------------------------------------------- api/index.py
    print("\nthe deployed app is the app the Stage 10 suite checks")
    import index
    import server
    check("api/index.py exposes server.app itself", index.app is server.app,
          "a second application object could drift from the checked one")

    # ------------------------------------------------------ the env seams
    print("\nthe two environment seams do what they appear to do (rule 7)")
    import catalog
    check("the shipped default still names floatchat_ro",
          "floatchat_ro" in catalog.DSN or "FLOATCHAT_DSN" in os.environ,
          catalog.DSN.split("@")[-1])

    probe = "import sys; sys.path.insert(0, 'api'); import catalog; print(catalog.DSN)"
    env = dict(os.environ, FLOATCHAT_DSN="postgresql://someone@example.invalid/x")
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                         text=True, cwd=ROOT, env=env)
    check("FLOATCHAT_DSN overrides the default",
          "example.invalid" in out.stdout, out.stdout.strip() or out.stderr.strip())

    def origins(value: str):
        """server.deployed_origins() under a given FLOATCHAT_ORIGINS."""
        before = os.environ.get("FLOATCHAT_ORIGINS")
        os.environ["FLOATCHAT_ORIGINS"] = value
        try:
            return server.deployed_origins(), None
        except ValueError as exc:
            return None, str(exc)
        finally:
            if before is None:
                os.environ.pop("FLOATCHAT_ORIGINS", None)
            else:
                os.environ["FLOATCHAT_ORIGINS"] = before

    got, err = origins("https://floatchat.vercel.app")
    check("a deployed origin is accepted", got == ["https://floatchat.vercel.app"],
          str(got or err))
    got, err = origins("https://a.vercel.app/, https://b.vercel.app")
    check("a comma-separated list is split and trimmed",
          got == ["https://a.vercel.app", "https://b.vercel.app"], str(got or err))
    got, err = origins("*")
    check("a wildcard is refused", got is None and "*" in (err or ""),
          str(got or err))
    got, err = origins("floatchat.vercel.app")
    check("an origin without a scheme is refused",
          got is None and "scheme" in (err or ""),
          "CORS matches origins by exact string; it would silently match nothing")
    check("the dev origins are never dropped",
          server.DEV_ORIGINS[0] in server.ALLOWED_ORIGINS,
          "a deployed API stays reachable from a local dashboard")

    # ------------------------------------------- the git tree, as a deploy path
    # Stage 15 had one deploy path: the CLI, which uploads the working
    # directory.  Accepting the GitHub integration added a second whose source
    # is the git tree, and `data/` was gitignored -- so a push deployed an API
    # whose retrieval was off, reporting "no index built" and answering anyway
    # (D15.11).  Stage 17 committed the index instead of disconnecting the
    # integration, which makes these the checks that keep the two paths equal.
    print("\na push carries what the CLI carries (D17.2)")
    index_files = {"data/rag/index.faiss", "data/rag/manifest.json"}
    under_data = git_visible("data")
    check("the retrieval index is in the git tree", index_files <= under_data,
          f"missing: {sorted(index_files - under_data)}"
          if index_files - under_data else "706 KB, deterministic, keyless")
    check("and nothing else under data/ is", under_data == index_files,
          f"extra: {sorted(under_data - index_files)[:3]}  "
          f"absent: {sorted(index_files - under_data)}")

    visible = git_visible()
    leaked = sorted(f for f in visible
                    if Path(f).name.startswith(".env") and Path(f).name != ".env.example")
    check("no .env but the example is visible to git", not leaked, str(leaked))
    check("both example files are", {".env.example", "ui/.env.example"} <= visible,
          "an ignore rule for .env* also hides the file documenting the names")

    # ---------------------------------------------- the dashboard's upload set
    print("\nthe dashboard is a second project with a second filter (D17.7)")
    ui_root = ROOT / "ui"
    check("ui/vercel.json exists", (ui_root / "vercel.json").exists(),
          "without it the project's build settings live only in a web form")
    ui_conf = json.loads((ui_root / "vercel.json").read_text()) \
        if (ui_root / "vercel.json").exists() else {}
    check("it builds the dashboard, not the API",
          "builds" not in ui_conf and ui_conf.get("outputDirectory") == "dist",
          str({k: v for k, v in ui_conf.items() if k != "$schema"}))
    check("there is no ui/.vercelignore", not (ui_root / ".vercelignore").exists(),
          "creating one REPLACES ui/.gitignore as the filter and lets "
          "node_modules and .env.local back into the bundle (D15.4)")

    ui_plain, ui_suf, ui_pre, ui_neg = parse_ignore((ui_root / ".gitignore").read_text())
    ui_files = upload_set(ui_plain, ui_suf, ui_pre, ui_neg, root=ui_root)
    ui_names = {str(f) for f in ui_files}
    ui_secrets = sorted(f for f in ui_names
                        if Path(f).name.startswith(".env") and Path(f).name != ".env.example")
    check("no credential file is in the dashboard's upload set", not ui_secrets,
          f"{ui_secrets} -- `vercel link` writes ui/.env.local with an OIDC token")
    for excluded in ("node_modules", "dist"):
        hit = [f for f in ui_names if f.startswith(excluded + "/")]
        check(f"ui/{excluded} does not ship", not hit, f"{len(hit)} files")
    ui_total = sum((ui_root / f).stat().st_size for f in ui_files)
    check("the dashboard upload is under 5 MB", ui_total < 5 * 1024 * 1024,
          f"{ui_total / 1e6:.2f} MB across {len(ui_files)} files")
    api_files = {str(f) for f in files}
    check("and the API upload carries none of the dashboard",
          not [f for f in api_files if f.startswith("ui/")],
          "two projects, two bundles")

    # ------------------------------------------------------- .env.example
    # The variables are the deployment.  A name that exists in the code and
    # not in the example is a setting nobody knows to configure until a demo
    # (D17.4) -- so this compares the file against the reads, both ways.
    print("\n.env.example names every variable the code reads, and no others")
    read: dict[str, list[str]] = {}
    sources = sorted((ROOT / "api").glob("*.py")) + sorted((ROOT / "etl").glob("*.py")) \
        + [ROOT / "run_pipeline.py"]
    for source in sources:
        if source.name.startswith("test_"):
            continue
        for match in ENV_READ.finditer(source.read_text()):
            name = next(g for g in match.groups() if g)
            read.setdefault(name, []).append(source.name)

    example = env_names(ROOT / ".env.example")
    undocumented = sorted(set(read) - set(example))
    check("every variable the code reads is in .env.example", not undocumented,
          str({n: sorted(set(read[n])) for n in undocumented}))
    stale = sorted(set(example) - set(read))
    check("and every variable in .env.example is read somewhere", not stale,
          f"documented but never read: {stale}")

    vite_read = set()
    for source in sorted((ROOT / "ui" / "src").rglob("*.js")) + \
            sorted((ROOT / "ui" / "src").rglob("*.jsx")):
        vite_read |= set(VITE_READ.findall(source.read_text()))
    ui_example = env_names(ROOT / "ui" / ".env.example")
    check("ui/.env.example names every VITE_ variable the dashboard reads",
          vite_read <= set(ui_example),
          f"missing: {sorted(vite_read - set(ui_example))}"
          if vite_read - set(ui_example) else f"{sorted(vite_read)}")
    check("and no VITE_ variable it does not read", set(ui_example) <= vite_read,
          f"documented but never read: {sorted(set(ui_example) - vite_read)}")

    # Committing an example file is committing a file people paste into.
    for path in (ROOT / ".env.example", ROOT / "ui" / ".env.example"):
        hits = sorted(n for n, v in env_names(path).items() if SECRETISH.search(v))
        check(f"{path.relative_to(ROOT)} holds no value shaped like a credential",
              not hits, f"{hits} -- placeholders only, this file is committed")

    # ------------------------------------ a key in the environment and the index
    # D15.5 warned that setting GEMINI_API_KEY on the deployment would leave the
    # query-time embedder mismatched against the shipped index -- retrieval
    # still working, quietly worse, saying nothing.  It does not: `load()`
    # reconstructs the embedder from the manifest (api/retrieval.py:183), which
    # is the whole reason the manifest carries it.  D17.10 records the
    # correction; this is what keeps it corrected.
    print("\na model key does not switch the embedder underneath the index (D17.10)")
    probe = ("import sys; sys.path.insert(0, 'api'); import embed, retrieval; "
             "print(embed.resolve().name); print(retrieval.load().embedder.name)")
    keyed = dict(os.environ, GEMINI_API_KEY="not-a-real-key-and-never-called")
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                         text=True, cwd=ROOT, env=keyed)
    lines = out.stdout.split()
    manifest = json.loads((ROOT / "data" / "rag" / "manifest.json").read_text())
    check("with a key set, resolve() would choose a different embedder",
          len(lines) == 2 and lines[0] != lines[1],
          f"{lines}  {out.stderr.strip()[:120]}")
    check("but the index is searched with the one that built it",
          len(lines) == 2 and lines[1] == manifest.get("embedder_name"),
          f"loaded {lines[1] if len(lines) > 1 else '?'}, "
          f"manifest {manifest.get('embedder_name')}")

    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
