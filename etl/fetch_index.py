"""Stage 1a: download the ARGO global profile index, once, and cache it.

The index is a directory listing of EVERY individual profile file on the GDAC
(~3 million rows).  We download the gzipped form (~58 MB instead of ~315 MB)
and keep it on disk; re-running this script does nothing unless --force.

Nothing is filtered here.  Fetch and filter are separate scripts so that
re-tuning the filter never re-downloads 58 MB.
"""

import argparse
import gzip
import sys
from pathlib import Path

import requests

INDEX_URL = "https://data-argo.ifremer.fr/ar_index_global_prof.txt.gz"
DEST = Path(__file__).resolve().parent.parent / "data" / "index" / "ar_index_global_prof.txt.gz"
CHUNK_BYTES = 1 << 20  # 1 MiB


def verify_gzip(path: Path) -> int:
    """Read the whole file through gzip so a truncated download is caught here,
    not three steps later inside pandas.  Returns the uncompressed byte count."""
    total = 0
    with gzip.open(path, "rb") as fh:
        while True:
            block = fh.read(CHUNK_BYTES)
            if not block:
                return total
            total += len(block)


def fetch(force: bool = False) -> Path:
    DEST.parent.mkdir(parents=True, exist_ok=True)

    if DEST.exists() and not force:
        print(f"cached      : {DEST}")
        print(f"compressed  : {DEST.stat().st_size:,} bytes")
        print("(pass --force to re-download)")
        return DEST

    # Download to a .part file and rename only on success, so an interrupted
    # run can never leave a corrupt file that looks like a valid cache.
    part = DEST.with_suffix(DEST.suffix + ".part")
    with requests.get(INDEX_URL, stream=True, timeout=120) as resp:
        resp.raise_for_status()
        expected = int(resp.headers.get("content-length", 0))
        print(f"source      : {INDEX_URL}")
        print(f"gdac update : {resp.headers.get('last-modified', 'unknown')}")
        print(f"expecting   : {expected:,} bytes")

        written = 0
        with open(part, "wb") as out:
            for block in resp.iter_content(chunk_size=CHUNK_BYTES):
                out.write(block)
                written += len(block)
                if expected:
                    pct = 100.0 * written / expected
                    print(f"\rdownloading : {written:,} / {expected:,} bytes ({pct:5.1f}%)",
                          end="", file=sys.stderr)
        print("", file=sys.stderr)

    if expected and written != expected:
        part.unlink(missing_ok=True)
        raise IOError(f"short read: got {written:,} bytes, expected {expected:,}")

    part.rename(DEST)
    raw = verify_gzip(DEST)
    print(f"compressed  : {DEST.stat().st_size:,} bytes")
    print(f"uncompressed: {raw:,} bytes")
    print(f"saved to    : {DEST}")
    return DEST


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true", help="re-download even if cached")
    fetch(**vars(ap.parse_args()))
