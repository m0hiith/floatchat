"""Stage 6 tests: every query runs, and every hostile input is refused.

No pytest -- one file, run it, read the funnel.  Same reason the ETL scripts
print their own reports: the check has to be runnable by a judge in one command.

    .venv/bin/python api/test_catalog.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import psycopg

import catalog
from catalog import QueryError

passed = failed = 0


def check(label: str, ok: bool, detail: str = ""):
    global passed, failed
    passed, failed = passed + ok, failed + (not ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('  -- ' + detail) if detail else ''}")


def refuses(label: str, fn, expect_in: str = ""):
    """The catalogue must reject this, and say why."""
    try:
        fn()
        check(label, False, "accepted, should have been refused")
    except QueryError as exc:
        ok = expect_in.lower() in str(exc).lower() if expect_in else True
        check(label, ok, str(exc)[:90] if ok else f"wrong message: {exc}")
    except Exception as exc:                                    # noqa: BLE001
        check(label, False, f"raised {type(exc).__name__} instead of QueryError: {exc}")


def main():
    live = catalog.LiveValues.load()
    conn = catalog.connect()
    print(f"catalogue   : {len(catalog.QUERIES)} queries")
    print(f"regions     : {len(live.regions)}   floats: {len(live.wmos)}   "
          f"window: {live.window[0]} .. {live.window[1]}")
    print(f"connection  : floatchat_ro (SELECT only), statement_timeout "
          f"{catalog.STATEMENT_TIMEOUT_MS} ms\n")

    print("every query runs with its documented example")
    for q in catalog.QUERIES:
        try:
            out = catalog.run(q.name, dict(q.example), live=live, conn=conn)
            check(f"{q.name:<24}", True, f"{out['row_count']} row(s)")
        except Exception as exc:                                # noqa: BLE001
            check(f"{q.name:<24}", False, f"{type(exc).__name__}: {exc}")

    print("\nbad parameters are refused, with the valid values named")
    refuses("unknown query name",
            lambda: catalog.run("drop_everything", {}, live=live, conn=conn), "no query named")
    refuses("region that does not exist",
            lambda: catalog.run("region_summary",
                                {"region": "Atlantic Ocean", "start": "2023-01-01",
                                 "end": "2023-12-31"}, live=live, conn=conn), "valid regions")
    refuses("SQL injection in a region name",
            lambda: catalog.run("region_summary",
                                {"region": "Arabian Sea'; DROP TABLE levels; --",
                                 "start": "2023-01-01", "end": "2023-12-31"},
                                live=live, conn=conn), "not a region")
    refuses("float that does not exist",
            lambda: catalog.run("float_trajectory", {"wmo": "9999999"}, live=live, conn=conn),
            "valid floats")
    refuses("malformed date",
            lambda: catalog.run("monthly_profile_counts",
                                {"region": "Arabian Sea", "start": "last tuesday",
                                 "end": "2023-12-31"}, live=live, conn=conn), "yyyy-mm-dd")
    refuses("limit above the row cap",
            lambda: catalog.run("profiles_in_region",
                                {"region": "Arabian Sea", "start": "2023-01-01",
                                 "end": "2023-12-31", "limit": 10_000_000},
                                live=live, conn=conn), "must be <=")
    refuses("invented parameter",
            lambda: catalog.run("region_summary",
                                {"region": "Arabian Sea", "start": "2023-01-01",
                                 "end": "2023-12-31", "table": "levels"},
                                live=live, conn=conn), "unknown parameter")
    refuses("missing required parameter",
            lambda: catalog.run("region_summary", {"region": "Arabian Sea"},
                                live=live, conn=conn), "missing required")

    print("\nthe database itself refuses to be written to")
    for stmt, label in [("DELETE FROM profiles WHERE false", "DELETE"),
                        ("UPDATE levels SET temp = 0 WHERE false", "UPDATE"),
                        ("CREATE TABLE evil (x int)", "CREATE TABLE"),
                        ("DROP TABLE levels", "DROP TABLE")]:
        try:
            conn.execute(stmt)
            check(f"{label:<14} rejected", False, "the read-only role executed it")
        except psycopg.errors.InsufficientPrivilege as exc:
            check(f"{label:<14} rejected", True, str(exc).splitlines()[0][:60])
        except psycopg.Error as exc:
            check(f"{label:<14} rejected", True, f"{type(exc).__name__}")

    print("\nresults are auditable")
    out = catalog.run("compare_regions",
                      {"region_a": "Bay of Bengal", "region_b": "Arabian Sea",
                       "start": "2023-01-01", "end": "2024-12-31"}, live=live, conn=conn)
    check("run() returns the query name and bound parameters",
          out["query"] == "compare_regions" and out["params"]["max_dbar"] == 10,
          f"{out['query']} {out['params']}")
    bengal = next(r for r in out["rows"] if r["region"] == "Bay of Bengal")
    arabian = next(r for r in out["rows"] if r["region"] == "Arabian Sea")
    check("Bay of Bengal is fresher than the Arabian Sea (physics)",
          bengal["mean_psal_psu"] < arabian["mean_psal_psu"],
          f"{bengal['mean_psal_psu']} < {arabian['mean_psal_psu']}")

    print("\ntool schemas")
    schemas = catalog.tool_schemas(live)
    check("one schema per query", len(schemas) == len(catalog.QUERIES))
    check("all strict, no free-form properties",
          all(s["strict"] and s["input_schema"]["additionalProperties"] is False
              for s in schemas))
    check("region enums come from the database",
          all(set(p["enum"]) == set(live.regions)
              for s in schemas for p in s["input_schema"]["properties"].values()
              if p.get("enum") and "region" in str(p.get("description", "")).lower()))

    conn.close()
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
