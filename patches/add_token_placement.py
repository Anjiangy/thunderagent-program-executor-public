"""Add byte-aware placement (EXECUTOR_PLACEMENT_CAP) to the executor.

Idempotent: re-running is a no-op. Fails loudly if an anchor is missing, so a
diverged deployment is caught here rather than silently measuring the wrong arm.

Factor A of the 2x2 is "what does placement order by":
  cap >= 1.0 (default)  -> pin counts (the original, weak signal)
  cap <  1.0            -> projected KV bytes, capped at `cap` of capacity
"""
from __future__ import annotations

import pathlib
import re
import sys

MARK = "# --- token-aware placement (2x2 factor A) ---"

METHOD = '''

    # --- token-aware placement (2x2 factor A) ---
    def token_load(self) -> Dict[str, int]:
        """Bytes each instance would have to hold: the sum of its pinned prompts."""
        with self._lock:
            load: Dict[str, int] = {}
            for entry in self._programs.values():
                if entry.instance:
                    load[entry.instance] = load.get(entry.instance, 0) + int(entry.prompt_tokens or 0)
            return load
'''


def patch_programs(path: pathlib.Path) -> bool:
    src = path.read_text(encoding="utf-8")
    if MARK in src:
        return False
    anchor = "    def pin_counts(self) -> Dict[str, int]:"
    if anchor not in src:
        raise SystemExit(f"anchor missing in {path}: {anchor!r}")
    src = src.replace(anchor, METHOD.strip("\n") + "\n\n" + anchor, 1)

    old_sig = """    @staticmethod
    def best_instance(
        live: Dict[str, Dict[str, Any]],
        exclude: Optional[str] = None,
        pin_counts: Optional[Dict[str, int]] = None,
    ) -> Optional[str]:"""
    new_sig = """    @staticmethod
    def best_instance(
        live: Dict[str, Dict[str, Any]],
        exclude: Optional[str] = None,
        pin_counts: Optional[Dict[str, int]] = None,
        token_load: Optional[Dict[str, int]] = None,
        cap: float = 1.0,
    ) -> Optional[str]:"""
    if old_sig not in src:
        raise SystemExit(f"anchor missing in {path}: best_instance signature")
    src = src.replace(old_sig, new_sig, 1)

    old_sort = """        candidates.sort(
            key=lambda kv: (
                int(counts.get(kv[0], 0)),
                int(kv[1].get("num_requests_running") or 0),
                float(kv[1].get("kv_cache_usage_perc") or 0.0),
                kv[0],
            )
        )"""
    new_sort = """        load = token_load or {}

        def _projected(name: str, info: Dict[str, Any]) -> float:
            capacity = float(info.get("num_gpu_blocks") or 0) * float(info.get("block_size") or 0)
            return (load.get(name, 0) / capacity) if capacity else 0.0

        def _key(kv):
            name, info = kv
            if cap < 1.0:
                # Bytes first: a 200-token session and a 4000-token one must not weigh the same.
                # Anything already past the cap goes last instead of being refused outright,
                # so a degraded fleet still places somewhere.
                # Balance first, cap second. Sorting by `over` ahead of the load
                # turned this into a "fill one card up to the cap, then overflow"
                # policy: the first card took everything until 85% and the second
                # card got the leftovers, which is the opposite of balancing.
                over = 1 if _projected(name, info) >= cap else 0
                # A brand-new program has no pinned bytes yet, so every instance
                # projects to 0 and the name tie-break would pile everything onto
                # `a`. Pin counts are the only signal that changes at placement
                # time, so they break the tie; bytes take over as load builds up.
                return (_projected(name, info), int(counts.get(name, 0)), over,
                        int(info.get("num_requests_running") or 0), name)
            return (
                int(counts.get(kv[0], 0)),
                int(kv[1].get("num_requests_running") or 0),
                float(kv[1].get("kv_cache_usage_perc") or 0.0),
                kv[0],
            )

        candidates.sort(key=_key)"""
    if old_sort not in src:
        raise SystemExit(f"anchor missing in {path}: candidate sort")
    src = src.replace(old_sort, new_sort, 1)

    old_call = "target = self.best_instance(live, pin_counts=self.pin_counts())"
    new_call = (
        "target = self.best_instance(live, pin_counts=self.pin_counts(),\n"
        "                                        token_load=self.token_load(), cap=_PLACEMENT_CAP())"
    )
    if old_call not in src:
        raise SystemExit(f"anchor missing in {path}: best_instance call site")
    src = src.replace(old_call, new_call, 1)

    helper = '''

def _PLACEMENT_CAP() -> float:
    """`EXECUTOR_PLACEMENT_CAP=1.0` (default) keeps the pin-count ordering."""
    import os

    try:
        return float(os.environ.get("EXECUTOR_PLACEMENT_CAP", "1.0"))
    except ValueError:
        return 1.0
'''
    if "_PLACEMENT_CAP" not in src.split(METHOD)[0]:
        # put the helper right before the ProgramTable class
        anchor_cls = "\nclass ProgramTable"
        if anchor_cls not in src:
            raise SystemExit(f"anchor missing in {path}: ProgramTable class")
        src = src.replace(anchor_cls, helper + anchor_cls, 1)

    path.write_text(src, encoding="utf-8")
    return True


def main() -> int:
    root = pathlib.Path(__file__).resolve().parents[1]
    target = root / "executor" / "programs.py"
    changed = patch_programs(target)
    print(("patched " if changed else "already patched ") + str(target))
    return 0


if __name__ == "__main__":
    sys.exit(main())
