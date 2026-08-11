"""
Token counter for token-telemetry.

Default: **Grok-2 tokenizer, fully local**
  Files vendored at `vendor/grok-2-tokenizer/` (from
  HuggingFace `alvarobartt/grok-2-tokenizer` / xai-org/grok-2).
  No network after the files are in the repo.

Fallback: grok-build `xai-token-estimation` bytes/4

Optional env `GROK_TOKENIZER`:
  - grok2 | grok-2 | hf   → local Grok-2 (default)
  - bytes4 | build        → len(utf-8)//4
  - tiktoken | cl100k     → OpenAI cl100k_base (not Grok)

Official $ still comes from turn_completed.usage; this module only supplies
*weights* for pro-rata attribution.
"""

from __future__ import annotations

import json
import os
import warnings
from pathlib import Path
from typing import Any, Optional

# --- xai-token-estimation (fallback) ---
BYTES_PER_TOKEN = 4
IMAGE_TOKEN_ESTIMATE = 765

HF_GROK2_ID = "alvarobartt/grok-2-tokenizer"

# Project-local copy (offline). Relative to this file: ../vendor/grok-2-tokenizer
_VENDOR_DIR = Path(__file__).resolve().parent.parent / "vendor" / "grok-2-tokenizer"

_hf_tok = None  # lazy AutoTokenizer
_tiktoken_enc = None
_mode: Optional[str] = None
_load_error: Optional[str] = None
_load_source: Optional[str] = None  # vendor path | hub id


def _candidate_paths() -> list[str]:
    """Ordered local dirs to try (no network)."""
    out: list[str] = []
    env = os.environ.get("GROK_TOKENIZER_PATH") or os.environ.get("GROK2_TOKENIZER_PATH")
    if env and Path(env).is_dir():
        out.append(str(Path(env).resolve()))
    if _VENDOR_DIR.is_dir() and (_VENDOR_DIR / "tokenizer.json").is_file():
        out.append(str(_VENDOR_DIR))
    # HF hub cache snapshot (if present from a previous download)
    hub = Path.home() / ".cache" / "huggingface" / "hub" / "models--alvarobartt--grok-2-tokenizer" / "snapshots"
    if hub.is_dir():
        for snap in sorted(hub.iterdir(), reverse=True):
            if (snap / "tokenizer.json").is_file():
                out.append(str(snap))
                break
    return out


def _load_hf_grok2() -> bool:
    global _hf_tok, _load_error, _load_source
    if _hf_tok is not None:
        return True
    try:
        # Tokenizer-only: no torch. Prefer offline local paths.
        os.environ.setdefault("TRANSFORMERS_NO_ADVISORY_WARNINGS", "1")
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from transformers import AutoTokenizer  # type: ignore
            from transformers.utils import logging as hf_logging  # type: ignore

            try:
                hf_logging.set_verbosity_error()
            except Exception:
                pass

            errors: list[str] = []
            # 1) Local vendor / explicit path / hub snapshot — always offline
            for path in _candidate_paths():
                try:
                    _hf_tok = AutoTokenizer.from_pretrained(
                        path,
                        use_fast=True,
                        local_files_only=True,
                    )
                    _load_source = path
                    _load_error = None
                    return True
                except Exception as e:
                    errors.append(f"{path}: {type(e).__name__}: {e}")

            # 2) Optional online fetch (only if nothing local worked)
            allow_hub = (os.environ.get("GROK_TOKENIZER_ALLOW_HUB") or "").strip() in (
                "1",
                "true",
                "yes",
            )
            if allow_hub:
                try:
                    _hf_tok = AutoTokenizer.from_pretrained(
                        HF_GROK2_ID,
                        use_fast=True,
                    )
                    _load_source = HF_GROK2_ID
                    _load_error = None
                    return True
                except Exception as e:
                    errors.append(f"hub:{HF_GROK2_ID}: {type(e).__name__}: {e}")

            _load_error = "; ".join(errors) if errors else "no local tokenizer found"
            _hf_tok = None
            return False
    except Exception as e:
        _load_error = f"{type(e).__name__}: {e}"
        _hf_tok = None
        return False


def _resolved_mode() -> str:
    global _mode, _tiktoken_enc
    if _mode is not None:
        return _mode

    pref = (os.environ.get("GROK_TOKENIZER") or "grok2").strip().lower()

    if pref in ("bytes4", "build", "xai-token-estimation", "chars4"):
        _mode = "bytes4"
        return _mode

    if pref in ("tiktoken", "cl100k", "cl100k_base"):
        try:
            import tiktoken  # type: ignore

            _tiktoken_enc = tiktoken.get_encoding("cl100k_base")
            _mode = "tiktoken"
            return _mode
        except Exception as e:
            _load_error = f"tiktoken: {e}"

    # Default + explicit grok2 / hf
    if pref in ("grok2", "grok-2", "hf", "huggingface", "default", ""):
        if _load_hf_grok2():
            _mode = "grok2"
            return _mode
        # fall through to bytes4

    # Unknown pref or HF failed → try grok2 once more if not already attempted
    if pref not in ("bytes4", "build") and _load_hf_grok2():
        _mode = "grok2"
        return _mode

    _mode = "bytes4"
    return _mode


def tokenizer_mode() -> str:
    """Active counter: 'grok2' | 'bytes4' | 'tiktoken'."""
    return _resolved_mode()


def tokenizer_info() -> dict[str, Any]:
    """Debug: mode, local path / hub id, load error if any."""
    m = _resolved_mode()
    return {
        "mode": m,
        "source": _load_source if m == "grok2" else None,
        "hf_id": HF_GROK2_ID if m == "grok2" else None,
        "vendor_dir": str(_VENDOR_DIR),
        "vendor_present": (_VENDOR_DIR / "tokenizer.json").is_file(),
        "load_error": _load_error,
        "env": os.environ.get("GROK_TOKENIZER"),
        "offline": True,  # default path is local-only
    }


def estimate_tokens(s: str) -> int:
    """
    Bytes/4 estimate — exact port of xai_token_estimation::estimate_tokens.
    Empty / short strings: floor division (len 0..3 → 0).
    """
    if not s:
        return 0
    if not isinstance(s, str):
        s = str(s)
    return len(s.encode("utf-8")) // BYTES_PER_TOKEN


def estimate_chars(tokens: int) -> int:
    """Inverse of estimate_tokens (tokens * 4). Rough only for grok2 mode."""
    return max(0, int(tokens)) * BYTES_PER_TOKEN


def estimate_image_tokens(image_count: int) -> int:
    return max(0, int(image_count)) * IMAGE_TOKEN_ESTIMATE


def count_tokens(text: Any) -> int:
    """
    Count tokens for a text payload with the active tokenizer.
    """
    if text is None:
        return 0
    if not isinstance(text, str):
        try:
            text = json.dumps(text, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(text)
    if not text:
        return 0

    mode = _resolved_mode()

    if mode == "grok2" and _hf_tok is not None:
        try:
            # add_special_tokens=False: pure text weight for pro-rata (no BOS/EOS)
            ids = _hf_tok.encode(text, add_special_tokens=False)
            return len(ids)
        except Exception:
            pass

    if mode == "tiktoken" and _tiktoken_enc is not None:
        try:
            return len(_tiktoken_enc.encode(text))
        except Exception:
            pass

    return estimate_tokens(text)


def count_json_tokens(obj: Any) -> int:
    """Tokenize a JSON-serializable object (full bracket size)."""
    if obj is None:
        return 0
    if isinstance(obj, str):
        return count_tokens(obj)
    try:
        return count_tokens(json.dumps(obj, ensure_ascii=False))
    except (TypeError, ValueError):
        return count_tokens(str(obj))


def count_chars_as_tokens(chars: int) -> int:
    """
    When only a char length is known (no text body), approximate with bytes/4.
    Real BPE needs the string — callers should prefer count_tokens(text).
    """
    return max(0, int(chars)) // BYTES_PER_TOKEN


def scale_weights_to_target(weights: list[float], target: int) -> list[int]:
    """Distribute target tokens proportional to non-negative weights (exact sum)."""
    n = len(weights)
    if n == 0:
        return []
    target = max(0, int(target))
    if target == 0:
        return [0] * n
    w = [max(0.0, float(x)) for x in weights]
    s = sum(w)
    if s <= 0:
        base = target // n
        out = [base] * n
        for i in range(target - base * n):
            out[i] += 1
        return out
    floats = [x / s * target for x in w]
    ints = [int(x) for x in floats]
    rem = target - sum(ints)
    order = sorted(range(n), key=lambda i: floats[i] - ints[i], reverse=True)
    for i in order:
        if rem <= 0:
            break
        ints[i] += 1
        rem -= 1
    return ints


def preload() -> str:
    """Force-load the active tokenizer (startup). Returns mode string."""
    return _resolved_mode()


if __name__ == "__main__":
    # Smoke test
    print("mode:", preload())
    print("info:", tokenizer_info())
    samples = [
        "hello world",
        "Human: What is Deep Learning?<|separator|>\n\n",
        json.dumps({"type": "tool_result", "content": "x" * 100}),
    ]
    for s in samples:
        print(f"  {count_tokens(s):5d}  {s[:60]!r}")
