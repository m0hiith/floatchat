#!/usr/bin/env python
"""Stage 11: build the vector index over the database's own summaries.

    python etl/build_index.py                     # whichever embedder the keys allow
    python etl/build_index.py --embedder=hashing  # keyless, deterministic
    python etl/build_index.py --embedder=gemini   # needs GEMINI_API_KEY, fails without
    python etl/build_index.py --force             # rebuild even if one exists

`auto` (the default) tries the API embedder when a key variable is set and
falls back to the keyless one when the API rejects it -- because a bad key must
not break `run_pipeline.py`, whose whole claim is that one command builds
everything without credentials.  The fallback is **loud**: it prints what was
rejected, what was used instead, and the command to redo it.  A quiet
substitution here would be the exact failure mode this project is organised
against -- the index would be built by a different embedder than the report
implied, and only the manifest would know.  `--embedder=gemini` never falls
back; asking for a specific embedder and getting another one is a lie.

The stage prints its own report, like every other stage: what went into the
corpus, what came out, and how well retrieval does against a fixed question
set.  That last number is the point.  "We added a vector database" is a claim;
recall@3 over eighteen questions with the misses printed is a measurement.

The index is derived data and lives under `data/`, which is not in version
control -- it is rebuilt from Postgres in about a second on the keyless
embedder, and re-embedded from the API when a key is present.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "api"))

import corpus                                            # noqa: E402
import embed                                             # noqa: E402
import retrieval                                         # noqa: E402


def diagnose(exc: Exception) -> str | None:
    """A bad key is a setup problem, not a stack trace (D7.7's rule again)."""
    text = str(exc)
    if "API_KEY_INVALID" in text or "API key not valid" in text:
        return ("the key in $GEMINI_API_KEY was rejected by the API "
                "(new key: https://aistudio.google.com/apikey)")
    if "RESOURCE_EXHAUSTED" in text or "429" in text:
        return "rate limited, or this key's tier has no quota for the embedding model"
    if "NOT_FOUND" in text or "was not found" in text:
        return "that embedding model id does not exist for this key"
    return None


def fallback_reason(which: str, exc: Exception) -> str | None:
    """Why we would fall back to the keyless embedder, or None to fail instead.

    Three rules in one function so the suite can check them without a network:
    only `auto` ever falls back; only a *recognised* provider failure is
    grounds for it; and an unrecognised exception is never swallowed, because
    substituting an embedder over a bug we do not understand would produce a
    working index for the wrong reason.
    """
    reason = diagnose(exc)
    return reason if (reason and which == "auto") else None


def main(argv: list[str]) -> int:
    which, force = "auto", False
    for arg in argv:
        if arg.startswith("--embedder="):
            which = arg.split("=", 1)[1]
        elif arg in ("--force", "--fresh"):
            force = True
        else:
            print(f"unknown flag {arg}")
            return 2

    if retrieval.exists() and not force:
        index = retrieval.load()
        print(f"index already built: {len(index.documents)} documents, "
              f"embedder {index.embedder.name}, built {index.built_at}")
        print("  rebuild with:  python etl/build_index.py --force")
        return 0

    print("stage 11 -- vector index over the ARGO summaries\n")

    started = time.time()
    documents = corpus.build()
    counts = corpus.by_kind(documents)
    chars = sum(len(d.embedding_text()) for d in documents)
    print("corpus, every document generated from a query it carries")
    for kind in corpus.KINDS:
        print(f"  {kind:<16}{counts[kind]:>5}")
    print(f"  {'total':<16}{len(documents):>5}   {chars:,} characters, "
          f"mean {chars // len(documents)} per document")

    try:
        embedder = embed.resolve(which)
    except RuntimeError as exc:
        print(f"\n{exc}")
        return 2

    print(f"\nembedding with {embedder.name}")
    try:
        index = retrieval.build(documents, embedder)
    except Exception as exc:
        reason = fallback_reason(which, exc)
        if reason is None and diagnose(exc) is None:
            raise
        if reason is None:
            # An explicit request is not negotiable. Asking for one embedder
            # and silently getting another would make every number below a
            # claim about something the caller did not ask for.
            print(f"\ngemini: {diagnose(exc)}")
            print("  keyless fallback:  python etl/build_index.py --embedder=hashing")
            return 2
        print(f"\n  !! gemini: {reason}")
        print("  !! FALLING BACK to the keyless embedder. This index is LEXICAL,")
        print("  !!   not semantic -- it cannot match a synonym. Fix the key and")
        print("  !!   re-run to replace it:")
        print("  !!     python etl/build_index.py --embedder=gemini --force")
        embedder = embed.HashingEmbedder()
        print(f"\nembedding with {embedder.name}")
        index = retrieval.build(documents, embedder)

    if isinstance(index.embedder, embed.HashingEmbedder):
        print("  NOTE: this embedder is lexical (hashed n-grams + IDF), not semantic.")
        print("        It needs no key and no download. It cannot match a synonym.")

    directory = index.save()
    took = time.time() - started
    size = sum(f.stat().st_size for f in directory.iterdir() if f.is_file())
    print(f"  {len(index.documents)} vectors, {index.dim} dimensions, exact "
          f"inner-product index (IndexFlatIP)")
    print(f"  written to {directory.relative_to(ROOT)}  ({size / 1024:.0f} KB)")
    if isinstance(index.embedder, embed.GeminiEmbedder):
        print(f"  {index.embedder.calls} API call(s)")

    result = retrieval.evaluate(index)
    print(f"\nretrieval measured over {result['n']} fixed questions")
    for k, v in result["recall"].items():
        print(f"  recall@{k}              {v:6.1%}")
    print(f"  MRR                   {result['mrr']:6.3f}")
    missed = [r for r in result["rows"] if r["rank"] is None]
    if missed:
        print(f"\n  {len(missed)} question(s) whose expected document was not in the "
              f"top {max(result['recall'])} -- printed, not hidden:")
        for r in missed:
            print(f"    {r['question']}")
            print(f"      best instead: {r['top']} ({r['score']})")
    else:
        print("\n  every question found an expected document in the top "
              f"{max(result['recall'])}")

    print(f"\nstage 11 complete in {took:.1f}s")
    print("  ask something with retrieval on:")
    print('    python api/chat.py "how salty is the Bay of Bengal?"')
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
