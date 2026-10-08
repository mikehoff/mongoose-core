"""Optional multilingual sentence embeddings: local, tiny, and honest about absence.

Purpose: fuzzy matching. "the QR thing is refused" should find enable-phone; "make the
website regenerate" should find the plugin refresh; a reworded prompt should still be
recognised as the same prompt. Multilingual coverage (incl. PL) comes free with the model.
Model: sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 (Apache-2.0, 118M params,
50+ languages including Polish), run via ONNX on CPU. Chosen deliberately for this household:
the rule-engagement layer's "same gist reworded" signal was token-overlap only, which goes
blind the moment the rewording crosses languages ("how is the RTM build going?" then "jak idzie
budowa RTM?"). Cross-lingual embeddings see through that. The same embedder is exposed
generically so search can use it later.

Local-first rules, strictly kept:
  - NEVER auto-downloads anything. The model lives on disk only if the owner put it there.
  - Everything degrades gracefully: no files or no libraries means embed() returns None and
    every caller falls back to its previous behaviour (Jaccard for rule-engagement).
  - Runtime deps (onnxruntime, tokenizers) are small, CPU-only, no torch, declared as the
    optional [embeddings] extra.

One-time setup on the Mac (the owner's explicit choice, ~470MB fp32):
    pip install onnxruntime tokenizers
    mkdir -p data/models/minilm-multilingual
    curl -L -o data/models/minilm-multilingual/model.onnx \
      https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2/resolve/main/onnx/model.onnx
    curl -L -o data/models/minilm-multilingual/tokenizer.json \
      https://huggingface.co/sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2/resolve/main/tokenizer.json
"""
from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any

_SESSION: Any = None
_TOKENIZER: Any = None
_TRIED = False


def model_dir() -> Path:
    base = (os.getenv("MONGOOSE_DATA_DIR") or "").strip() or os.path.expanduser("~/.mongoose/data")
    return Path(base) / "models" / "minilm-multilingual"


_MODEL_URLS = {
    "model.onnx": ("https://huggingface.co/sentence-transformers/"
                   "paraphrase-multilingual-MiniLM-L12-v2/resolve/main/onnx/model.onnx"),
    "tokenizer.json": ("https://huggingface.co/sentence-transformers/"
                       "paraphrase-multilingual-MiniLM-L12-v2/resolve/main/tokenizer.json"),
}


def ensure_model(quiet: bool = False) -> dict[str, Any]:
    """Make the fuzzy-matching model present: download what is missing, skip what exists.
    Idempotent and safe to call from install, update, and server startup. Controlled by
    MONGOOSE_AUTO_EMBEDDINGS (default on; set 0/false to keep installs fully offline).
    Never raises: returns {ok, downloaded, skipped, error}."""
    if str(os.getenv("MONGOOSE_AUTO_EMBEDDINGS", "1")).strip().lower() in ("0", "false", "no"):
        return {"ok": False, "downloaded": [], "skipped": [], "error": "disabled by MONGOOSE_AUTO_EMBEDDINGS"}
    d = model_dir()
    d.mkdir(parents=True, exist_ok=True)
    downloaded, skipped = [], []
    for name, url in _MODEL_URLS.items():
        fp = d / name
        if fp.exists() and fp.stat().st_size > 0:
            skipped.append(name)
            continue
        if not quiet:
            print(f"  embeddings: downloading {name}...", flush=True)
        try:
            import urllib.request as _ur
            tmp = fp.with_suffix(fp.suffix + ".part")
            _ur.urlretrieve(url, tmp)
            tmp.replace(fp)  # atomic-ish: never leave a half file under the real name
            downloaded.append(name)
            if not quiet:
                print(f"  embeddings: {name} ok ({fp.stat().st_size / 1e6:.0f}MB)", flush=True)
        except Exception as exc:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            return {"ok": False, "downloaded": downloaded, "skipped": skipped, "error": str(exc)}
    return {"ok": True, "downloaded": downloaded, "skipped": skipped, "error": None}


def available() -> dict[str, Any]:
    """Truthful availability report for readiness/status surfaces."""
    d = model_dir()
    files_ok = (d / "model.onnx").exists() and (d / "tokenizer.json").exists()
    import importlib.util
    libs_ok = (importlib.util.find_spec("onnxruntime") is not None
               and importlib.util.find_spec("tokenizers") is not None)
    # Honour the opt-out in the diagnostics, not just in ensure_model (external audit A4): an
    # owner who set MONGOOSE_AUTO_EMBEDDINGS=0 must not be told a 500MB download is coming.
    auto_off = str(os.getenv("MONGOOSE_AUTO_EMBEDDINGS", "1")).strip().lower() in ("0", "false", "no")
    if not (files_ok and libs_ok):
        if auto_off:
            hint = ("auto-download disabled by MONGOOSE_AUTO_EMBEDDINGS; using word-overlap "
                    "search until you re-enable it or run 'mongoose setup-embeddings' by hand.")
        else:
            hint = ("downloads automatically at next startup or update (~500MB once); or run "
                    "'mongoose setup-embeddings' now. Until then matching stays word-overlap "
                    "based (loose phrasing matches less well).")
    else:
        hint = ""
    return {"ok": files_ok and libs_ok, "files": files_ok, "libraries": libs_ok,
            "path": str(d), "auto_download": not auto_off, "hint": hint}


def benchmark(samples: list[str] | None = None) -> dict[str, Any]:
    """An honest embeddings benchmark for the cockpit. This is one local ONNX model, not a
    bake-off, so the useful questions are: is semantic search available here, how fast is it on
    this Mac, and does the cross-lingual matching (this model's whole reason for existing)
    actually work. Measures embed latency on a small batch and a PL/EN gist similarity. Cheap:
    the model is loaded once and cached, so repeat calls only pay the inference."""
    av = available()
    out: dict[str, Any] = {"available": bool(av.get("ok")), "path": av.get("path"),
                           "hint": av.get("hint", ""), "files": av.get("files"),
                           "libraries": av.get("libraries")}
    if not av.get("ok") or not _load():
        out["available"] = False
        return out
    import time
    texts = samples or ["the QR thing is refused", "make the website regenerate",
                        "reset my password please", "how is the RTM build going"]
    embed(texts[:1])  # warm (graph is already loaded; this is negligible)
    t0 = time.perf_counter()
    vecs = embed(texts)
    dt = time.perf_counter() - t0
    if not vecs:
        out["available"] = False
        return out
    out["dimensions"] = len(vecs[0])
    out["batch_size"] = len(texts)
    out["ms_total"] = round(dt * 1000, 1)
    out["ms_per_text"] = round(dt * 1000 / len(texts), 1)
    try:
        sim = semantic_similarity("how is the RTM build going", "jak idzie budowa RTM")
        if sim is not None:
            out["crosslingual_similarity"] = round(sim, 3)  # EN vs PL, same gist
    except Exception:
        pass
    return out


def _load() -> bool:
    global _SESSION, _TOKENIZER, _TRIED
    if _SESSION is not None:
        return True
    if _TRIED:
        return False
    _TRIED = True
    try:
        d = model_dir()
        if not ((d / "model.onnx").exists() and (d / "tokenizer.json").exists()):
            return False
        import os
        import onnxruntime as ort
        from tokenizers import Tokenizer
        _TOKENIZER = Tokenizer.from_file(str(d / "tokenizer.json"))
        _TOKENIZER.enable_truncation(max_length=128)
        # THROTTLE: ONNX's CPU provider defaults to one intra-op thread PER CORE, so a burst of
        # embeddings (routing, capability search, correlation) could peg every core and starve the
        # event loop , which is exactly what took the server down. Cap it hard (default 2 threads,
        # overridable via MONGOOSE_EMBED_THREADS) so embeddings can never consume more than a couple of
        # cores no matter how many are on the machine.
        try:
            _cap = int(os.getenv("MONGOOSE_EMBED_THREADS", "2") or "2")
        except Exception:
            _cap = 2
        _so = ort.SessionOptions()
        _so.intra_op_num_threads = max(1, _cap)
        _so.inter_op_num_threads = 1
        _SESSION = ort.InferenceSession(str(d / "model.onnx"), sess_options=_so,
                                        providers=["CPUExecutionProvider"])
        return True
    except Exception:
        _SESSION = None
        _TOKENIZER = None
        return False


def embed(texts: list[str]) -> list[list[float]] | None:
    """Mean-pooled, L2-normalised sentence vectors, or None when the model isn't present.
    Callers MUST handle None: absence is a supported state, not an error."""
    if not texts or not _load():
        return None
    try:
        import numpy as np
        encs = [_TOKENIZER.encode(t or "") for t in texts]
        maxlen = max(len(e.ids) for e in encs)
        ids = np.zeros((len(encs), maxlen), dtype="int64")
        mask = np.zeros((len(encs), maxlen), dtype="int64")
        for i, e in enumerate(encs):
            ids[i, :len(e.ids)] = e.ids
            mask[i, :len(e.ids)] = e.attention_mask
        # This ONNX export requires token_type_ids (all zeros for single sentences). Omitting it made
        # every embed() call raise "Required inputs (['token_type_ids']) are missing" and return None,
        # so semantic search silently ran keyword-only forever. Provide it only when the model wants it.
        feed = {"input_ids": ids, "attention_mask": mask}
        if any(inp.name == "token_type_ids" for inp in _SESSION.get_inputs()):
            feed["token_type_ids"] = np.zeros((len(encs), maxlen), dtype="int64")
        out = _SESSION.run(None, feed)[0]
        m = mask[..., None].astype("float32")
        pooled = (out * m).sum(axis=1) / m.sum(axis=1).clip(min=1e-9)
        norms = np.linalg.norm(pooled, axis=1, keepdims=True).clip(min=1e-9)
        return (pooled / norms).tolist()
    except Exception:
        return None


def cosine(a: list[float], b: list[float]) -> float:
    num = sum(x * y for x, y in zip(a, b))
    da = math.sqrt(sum(x * x for x in a)) or 1e-9
    db = math.sqrt(sum(y * y for y in b)) or 1e-9
    return num / (da * db)


def semantic_similarity(a: str, b: str) -> float | None:
    """Similarity of two texts in [approx -1, 1], or None when embeddings are unavailable."""
    vecs = embed([a or "", b or ""])
    if not vecs or len(vecs) != 2:
        return None
    return cosine(vecs[0], vecs[1])
