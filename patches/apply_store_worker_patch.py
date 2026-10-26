"""Idempotent patch for vLLM's Mooncake store worker on Ascend.

Why: vLLM-Ascend hands the store connector KV caches that are neither
``torch.Tensor`` nor ``list`` (tuples / nested containers), so the upstream
assert blows up and the engine cannot start:

    File ".../mooncake/store/worker.py", line 1132, in _repr_tensor
        assert isinstance(v, torch.Tensor | list)
    AssertionError  ->  Engine core initialization failed

What it does: replaces that helper with a recursive "first torch.Tensor wins"
scan and skips entries that contain no tensor at all.

Effect measured on the Ascend pod: both engines came up with
``--kv-transfer-config '{"kv_connector":"MooncakeStoreConnector","kv_role":"kv_both"}'``
and answered 200 within 90 s. It does NOT make the data path work: the Ascend
transport still refuses to register NPU device memory, so ``Keys`` stays 0.

Run:  python3 patches/apply_store_worker_patch.py [--path <worker.py>]
"""

from __future__ import annotations

import argparse
import pathlib
import sys

DEFAULT = "<vllm-src>"

OLD_HELPER = '''        def _repr_tensor(v: torch.Tensor | list[torch.Tensor]) -> torch.Tensor:
            assert isinstance(v, torch.Tensor | list)
            return v if isinstance(v, torch.Tensor) else v[0]'''

NEW_HELPER = '''        def _repr_tensor(v):
            """First torch.Tensor inside v (vLLM-Ascend hands over lists/tuples)."""
            if isinstance(v, torch.Tensor):
                return v
            if isinstance(v, (list, tuple)):
                for item in v:
                    found = _repr_tensor(item)
                    if found is not None:
                        return found
            return None'''

OLD_CALL = '''        for value in kv_caches.values():
            cache = _repr_tensor(value)
            cache_storage = cache.untyped_storage()'''

NEW_CALL = '''        for value in kv_caches.values():
            cache = _repr_tensor(value)
            if cache is None:
                continue
            cache_storage = cache.untyped_storage()'''


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--path", default=DEFAULT)
    args = ap.parse_args()

    path = pathlib.Path(args.path)
    if not path.exists():
        print(f"NOT FOUND: {path}", file=sys.stderr)
        return 2

    text = path.read_text(encoding="utf-8", errors="surrogateescape")

    if NEW_HELPER in text and NEW_CALL in text:
        print("already patched")
        return 0

    if OLD_HELPER not in text:
        print("HELPER anchor missing - upstream changed? abort", file=sys.stderr)
        return 3
    if OLD_CALL not in text:
        print("CALL anchor missing - upstream changed? abort", file=sys.stderr)
        return 3

    text = text.replace(OLD_HELPER, NEW_HELPER).replace(OLD_CALL, NEW_CALL)
    path.write_text(text, encoding="utf-8", errors="surrogateescape")
    print(f"PATCHED {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
