"""Stage 11b: the embedder seam.

Two embedders behind one interface, for the same reason Stage 8 put two model
providers behind one transport: the thing above must not learn which one it
got.  `retrieval.py` never asks.

    GeminiEmbedder   gemini-embedding-001 over the API.  Needs GEMINI_API_KEY.
    HashingEmbedder  deterministic, local, no key, no model download.
    ScriptedEmbedder replays fixed vectors, for the check suite.

**Which one is honest about what.**  The Gemini embedder is semantic: it puts
"how salty is the bay" near a document about salinity that never uses the word
"salty".  The hashing embedder is *lexical* -- a hashed n-gram bag with inverse
document frequency, which is a real retrieval method and a weak one.  It shares
no vocabulary knowledge and cannot match a synonym.  It exists so that Stage 11
runs, is measurable, and is demonstrable on a machine with no credentials at
all, which is the same promise the rest of this project makes.  Nothing in the
code or the README calls it semantic search.

Two properties everything here must hold, both asserted in the suite:

  1. **Vectors are L2-normalised**, always.  The index is `IndexFlatIP`, so an
     inner product is a cosine only if the vectors are unit length.  Gemini's
     embeddings are normalised at 3072 dimensions but *not* after truncation to
     a smaller `output_dimensionality`, which is exactly the case we use -- so
     we normalise rather than assume.
  2. **Embedding is deterministic across processes.**  Python's built-in
     `hash()` is salted per interpreter, so a hashed embedder built on it would
     produce a different index every run and match nothing after a restart.
     `blake2b` is used instead, and a check re-embeds in a subprocess and
     compares.

Document and query text are embedded *asymmetrically* where the model supports
it (RETRIEVAL_DOCUMENT vs RETRIEVAL_QUERY).  A question and the passage that
answers it are not the same kind of string, and the model is trained knowing
that.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

GEMINI_EMBED_MODEL = "gemini-embedding-001"
GEMINI_DIM = 768              # 3072 is the native size; 768 is a supported cut
GEMINI_BATCH = 32             # requests per call, kept well under the API cap

HASHING_DIM = 1024
WORD = re.compile(r"[a-z0-9]+")


def normalise(vectors: np.ndarray) -> np.ndarray:
    """Unit length, and a zero vector stays zero rather than becoming NaN.

    A document that produces no features at all is a corpus bug, not a division
    to paper over -- but it must not poison the whole matrix with NaN on the
    way to being caught.
    """
    v = np.asarray(vectors, dtype=np.float32)
    single = v.ndim == 1
    if single:
        v = v[None, :]
    norms = np.linalg.norm(v, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    out = (v / norms).astype(np.float32)
    return out[0] if single else out


class Embedder(Protocol):
    name: str
    dim: int

    def embed_documents(self, texts: list[str]) -> np.ndarray: ...
    def embed_query(self, text: str) -> np.ndarray: ...
    def state(self) -> dict: ...


# --------------------------------------------------------------------------
# Gemini
# --------------------------------------------------------------------------

@dataclass
class GeminiEmbedder:
    """gemini-embedding-001, asymmetric, batched, normalised after truncation."""
    model: str = GEMINI_EMBED_MODEL
    dim: int = GEMINI_DIM
    batch: int = GEMINI_BATCH
    client: Any = None
    calls: int = 0

    @property
    def name(self) -> str:
        return f"gemini:{self.model}:{self.dim}"

    def __post_init__(self):
        if self.client is None:
            from google import genai
            self.client = genai.Client()

    def _embed(self, texts: list[str], task_type: str) -> np.ndarray:
        from google.genai import types
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch):
            chunk = texts[i:i + self.batch]
            self.calls += 1
            resp = self.client.models.embed_content(
                model=self.model,
                contents=chunk,
                config=types.EmbedContentConfig(task_type=task_type,
                                                output_dimensionality=self.dim),
            )
            got = list(resp.embeddings or [])
            # Rule 1, applied to an API: a short reply is named, not padded.
            if len(got) != len(chunk):
                raise RuntimeError(
                    f"{self.model} returned {len(got)} embeddings for {len(chunk)} "
                    f"inputs; refusing to guess which document lost its vector")
            out.extend(list(e.values) for e in got)
        return normalise(np.asarray(out, dtype=np.float32))

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        return self._embed(texts, "RETRIEVAL_DOCUMENT")

    def embed_query(self, text: str) -> np.ndarray:
        return self._embed([text], "RETRIEVAL_QUERY")[0]

    def state(self) -> dict:
        return {"kind": "gemini", "model": self.model, "dim": self.dim}


# --------------------------------------------------------------------------
# the keyless one
# --------------------------------------------------------------------------

def features(text: str) -> Counter:
    """Word unigrams, word bigrams, and character 4-grams inside words.

    The character grams are what make `2902203`, `salinity`/`saline` and
    `Bengal`/`bengal` reachable from a query that spells them slightly
    differently.  They are not stemming and they are not synonyms.
    """
    words = WORD.findall(text.lower())
    f: Counter = Counter()
    for w in words:
        f[f"w:{w}"] += 1
        padded = f"^{w}$"
        for i in range(len(padded) - 3):
            f[f"c:{padded[i:i + 4]}"] += 1
    for a, b in zip(words, words[1:]):
        f[f"b:{a}_{b}"] += 1
    return f


def bucket(feature: str, dim: int) -> tuple[int, float]:
    """Hashing trick with a signed bucket, on a hash that is stable across
    processes.  `hash()` is not: PYTHONHASHSEED randomises it per interpreter,
    so an index built in one process would miss in the next."""
    digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
    n = int.from_bytes(digest, "big")
    return n % dim, (1.0 if (n >> 63) & 1 else -1.0)


@dataclass
class HashingEmbedder:
    """A lexical bag-of-n-grams vector.  Fitted on the corpus for IDF.

    `fit` is what makes it worth having: without inverse document frequency
    every document scores on the words that appear in all of them ("profiles",
    "database", "dbar"), and the ranking is close to useless.  The fitted
    weights are small (one float per bucket) and are stored *inside the index*,
    so a query embedded after a restart is weighted exactly as the documents
    were.
    """
    dim: int = HASHING_DIM
    idf: np.ndarray | None = None
    n_documents: int = 0

    @property
    def name(self) -> str:
        return f"hashing:{self.dim}" + ("" if self.idf is None else ":fitted")

    def fit(self, texts: list[str]) -> "HashingEmbedder":
        df = np.zeros(self.dim, dtype=np.float64)
        for text in texts:
            hit = np.zeros(self.dim, dtype=bool)
            for feature in features(text):
                hit[bucket(feature, self.dim)[0]] = True
            df += hit
        self.n_documents = len(texts)
        # Smoothed IDF. A bucket seen in every document contributes ~0.
        self.idf = np.log((1.0 + self.n_documents) / (1.0 + df)).astype(np.float32) + 1.0
        return self

    def _vector(self, text: str) -> np.ndarray:
        v = np.zeros(self.dim, dtype=np.float32)
        for feature, tf in features(text).items():
            b, sign = bucket(feature, self.dim)
            v[b] += sign * (1.0 + math.log(tf))       # sublinear term frequency
        if self.idf is not None:
            v *= self.idf
        return v

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return normalise(np.vstack([self._vector(t) for t in texts]))

    def embed_query(self, text: str) -> np.ndarray:
        return normalise(self._vector(text))

    def state(self) -> dict:
        return {"kind": "hashing", "dim": self.dim, "n_documents": self.n_documents,
                "idf": None if self.idf is None else self.idf.tolist()}

    @classmethod
    def from_state(cls, state: dict) -> "HashingEmbedder":
        e = cls(dim=int(state["dim"]), n_documents=int(state.get("n_documents", 0)))
        if state.get("idf") is not None:
            e.idf = np.asarray(state["idf"], dtype=np.float32)
        return e


# --------------------------------------------------------------------------
# the test double
# --------------------------------------------------------------------------

@dataclass
class ScriptedEmbedder:
    """Returns vectors it was handed, and records what it was asked to embed.

    The same shape as `chat.ScriptedTransport`, and for the same reason: the
    parts we own -- batching, normalisation, asymmetry, index wiring -- are
    testable with no key and no network.  What we do not own is whether the
    embedding is any good.
    """
    vectors: dict[str, list[float]]
    dim: int = 4
    documents_seen: list[str] = field(default_factory=list)
    queries_seen: list[str] = field(default_factory=list)

    name: str = "scripted"

    def _lookup(self, text: str) -> np.ndarray:
        if text not in self.vectors:
            raise AssertionError(f"ScriptedEmbedder has no vector for {text[:60]!r}")
        return np.asarray(self.vectors[text], dtype=np.float32)

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        self.documents_seen.extend(texts)
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        return normalise(np.vstack([self._lookup(t) for t in texts]))

    def embed_query(self, text: str) -> np.ndarray:
        self.queries_seen.append(text)
        return normalise(self._lookup(text))

    def state(self) -> dict:
        return {"kind": "scripted", "dim": self.dim}


# --------------------------------------------------------------------------
# choosing one
# --------------------------------------------------------------------------

def have_gemini() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))


def resolve(which: str = "auto") -> Embedder:
    """`auto` means the API embedder if a key exists, the local one otherwise.

    Same rule as `chat.make_transport`: the credential decides, and the caller
    can override.  A missing key is not an error here -- it selects the
    embedder that does not need one, and the index records which was used.
    """
    if which in ("auto", "gemini") and have_gemini():
        return GeminiEmbedder()
    if which == "gemini":
        raise RuntimeError(
            "gemini embeddings need GEMINI_API_KEY.\n"
            "  export GEMINI_API_KEY=...     (https://aistudio.google.com/apikey)\n"
            "  or build the keyless index:   python etl/build_index.py --embedder=hashing")
    return HashingEmbedder()


def from_state(state: dict) -> Embedder:
    """Rebuild the embedder an index was built with, so queries are embedded
    the same way the documents were.  Mixing two embedders across one index is
    silent nonsense -- every score would be meaningless but nothing would
    raise -- so the index stores this and `retrieval.load` uses it."""
    kind = state.get("kind")
    if kind == "hashing":
        return HashingEmbedder.from_state(state)
    if kind == "gemini":
        return GeminiEmbedder(model=state["model"], dim=int(state["dim"]))
    raise ValueError(f"cannot rebuild embedder of kind {kind!r}")
