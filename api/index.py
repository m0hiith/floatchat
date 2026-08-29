#!/usr/bin/env python
"""Stage 15: the one module a Vercel Python function imports.

It exists to be the *only* endpoint.  Vercel's zero-configuration Python
runtime turns every `.py` file under `api/` into its own serverless function,
which for this repository would publish `catalog`, `corpus`, `embed`, `chat`,
`router` and all six `test_*` suites as public routes -- `api/test_router.py`
answering HTTP is not a thing anyone decided (D15.7).  Naming a `builds` array
in `vercel.json` disables that detection, so the deployment has exactly the
routes `api/server.py` declares and no others.

Two further things the platform will not infer:

  * `includeFiles: "data/rag/**"`.  A function bundle carries its own directory;
    `api/retrieval.py` resolves the index at REPO ROOT / data / rag, which is
    outside it.  Without the include, /ask deploys and reports "no index built"
    -- a working API with retrieval silently off, which is the shape of failure
    rule 7 exists to refuse.
  * The Python version.  `requirements.txt` is frozen against 3.13 and Vercel's
    runtime is its own; if `faiss-cpu` or `numpy` has no wheel for the version
    the build selects, the build fails there rather than here.

And one that will bite locally: do NOT `vercel build && vercel deploy
--prebuilt` for this project from macOS.  The Python builder pip-installs at
build time, so a local build resolves darwin wheels for numpy and faiss-cpu,
uploads them, and the function raises ImportError on Linux at cold start.  Let
it build remotely -- plain `vercel --prod` (D15.8).

The `sys.path` line mirrors `api/server.py`: the modules in this package import
each other by bare name, so their own directory has to be importable.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from server import app  # noqa: E402  -- after the path insert, deliberately

# Vercel's Python runtime looks for an ASGI callable named `app`.  There is no
# adapter and no second application object: this is the same `app` that
# `uvicorn api.server:app` serves locally, so the deployed API cannot drift
# from the one the Stage 10 suite checks.
__all__ = ["app"]
