"""Program table: the pin between a program and the instance holding its KV.

This is the only piece of state the executor owns. It exists because a
multi-turn agent program keeps growing its context on one instance; moving the
program means moving that pin, and everything else (admission, throttling)
stays in the controller.

It also remembers the largest prompt each program has been given. A migration
has to re-materialise that context somewhere else, and on this stack (no block
mover, see M0) the only way to do it is to replay the prompt into the target so
vLLM prefills it there.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ProgramEntry:
    program_id: str
    instance: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    last_seen: float = field(default_factory=time.time)
    requests: int = 0
    in_flight: int = 0
    migrations: int = 0
    migrating: bool = False
    migrating_since: Optional[float] = None
    migrate_target: Optional[str] = None
    migrate_mode: Optional[str] = None
    last_move: Optional[Dict[str, Any]] = None
    # Largest prompt seen, kept only so a migration can replay it. Never sent
    # anywhere except back to an instance.
    prompt_path: Optional[str] = None
    prompt_body: Optional[Dict[str, Any]] = None
    prompt_tokens: int = 0
    warmed_tokens: int = 0
    # Handshake blob captured on the source engine; the target injects it on the
    # program's next request so it pulls the blocks instead of re-prefilling.
    pending_kvp: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "program_id": self.program_id,
            "instance": self.instance,
            "requests": self.requests,
            "in_flight": self.in_flight,
            "migrations": self.migrations,
            "migrating": self.migrating,
            "migrate_target": self.migrate_target,
            "migrate_mode": self.migrate_mode,
            "created_at": self.created_at,
            "last_seen": self.last_seen,
            "last_move": self.last_move,
            "prompt_path": self.prompt_path,
            "prompt_tokens": self.prompt_tokens,
            "warmed_tokens": self.warmed_tokens,
            "kvp_ready": self.pending_kvp is not None,
            "warm_ready": self.prompt_body is not None,
        }



def _PLACEMENT_CAP() -> float:
    """`EXECUTOR_PLACEMENT_CAP=1.0` (default) keeps the pin-count ordering."""
    import os

    try:
        return float(os.environ.get("EXECUTOR_PLACEMENT_CAP", "1.0"))
    except ValueError:
        return 1.0

class ProgramTable:
    """Thread-safe program -> instance pin table."""

    def __init__(self, placement: str = "affinity") -> None:
        self._lock = threading.RLock()
        self._programs: Dict[str, ProgramEntry] = {}
        # Placement policy. "affinity" keeps a program's KV on one instance;
        # "roundrobin" ignores the pin and alternates, which is the naive
        # baseline M3 measures against. Only this switch differs between arms.
        self.placement = placement if placement in ("affinity", "roundrobin") else "affinity"
        self._rr = 0

    # -- lookup ---------------------------------------------------------

    def touch(self, program_id: str) -> ProgramEntry:
        with self._lock:
            entry = self._programs.get(program_id)
            if entry is None:
                entry = ProgramEntry(program_id=program_id)
                self._programs[program_id] = entry
            entry.last_seen = time.time()
            entry.requests += 1
            return entry

    def entries(self) -> List[ProgramEntry]:
        """Snapshot of every known program (the balancer needs them all)."""
        with self._lock:
            return list(self._programs.values())

    def get(self, program_id: str) -> Optional[ProgramEntry]:
        with self._lock:
            return self._programs.get(program_id)

    # -- prompt bookkeeping ---------------------------------------------

    def record_prompt(self, program_id: str, path: str, payload: Dict[str, Any]) -> None:
        """Remember the request body this program most recently sent.

        That body is what a migration replays into the target: the next turn
        appends to this same context, so the newest body is the block prefix the
        program is actually going to send. Keeping the largest body instead lets
        a stale, longer prompt from an earlier session survive, and the replay
        then warms bytes the program no longer sends - a migration that reports
        success and still lands cold. Memory only: a restarted executor has
        nothing to replay.
        """
        if not payload:
            return
        try:
            estimate = int(len(json.dumps(payload, ensure_ascii=False)) / 3.6)
        except (TypeError, ValueError):
            return
        with self._lock:
            entry = self._programs.get(program_id)
            if entry is None:
                return
            entry.prompt_path = path
            entry.prompt_body = payload
            entry.prompt_tokens = estimate

    def set_kvp(self, program_id: str, kvp: Dict[str, Any]) -> bool:
        """Park a source-side handshake blob for the program's next request."""
        with self._lock:
            entry = self._programs.get(program_id)
            if entry is None:
                return False
            entry.pending_kvp = dict(kvp)
            return True

    def take_kvp(self, program_id: str) -> Optional[Dict[str, Any]]:
        """Pop the parked blob; a handshake is only good for one consumer."""
        with self._lock:
            entry = self._programs.get(program_id)
            if entry is None or not entry.pending_kvp:
                return None
            kvp = entry.pending_kvp
            entry.pending_kvp = None
            return dict(kvp)

    def record_warmed(self, program_id: str, tokens: int) -> None:
        with self._lock:
            entry = self._programs.get(program_id)
            if entry is not None:
                entry.warmed_tokens = int(tokens)

    def migration_candidates(self, instance: str) -> List[str]:
        """Programs pinned here that are safe to move, biggest context first.

        Biggest first because moving a large context off a hot instance is what
        actually buys room; moving a small one churns for nothing.
        """
        with self._lock:
            rows = [
                (e.prompt_tokens, e.program_id)
                for e in self._programs.values()
                if e.instance == instance
                and not e.migrating
                and e.in_flight == 0
                and e.prompt_body is not None
            ]
        rows.sort(reverse=True)
        return [program_id for _, program_id in rows]

    # -- placement ------------------------------------------------------

    @staticmethod
    def best_instance(
        live: Dict[str, Dict[str, Any]],
        exclude: Optional[str] = None,
        pin_counts: Optional[Dict[str, int]] = None,
        token_load: Optional[Dict[str, int]] = None,
        cap: float = 1.0,
    ) -> Optional[str]:
        """Least loaded healthy instance.

        Ordering matters: vLLM's own counters do not know how many programs are
        pinned to an instance, so two idle instances look identical and every new
        program piles onto the same one. Preferring the instance with the fewest
        pinned programs spreads programs before either instance saturates.
        """
        counts = pin_counts or {}
        candidates = [
            (name, info)
            for name, info in live.items()
            if info.get("healthy", False) and name != exclude
        ]
        if not candidates:
            return None
        load = token_load or {}

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

        candidates.sort(key=_key)
        return candidates[0][0]

    # --- token-aware placement (2x2 factor A) ---
    def token_load(self) -> Dict[str, int]:
        """Bytes each instance would have to hold: the sum of its pinned prompts."""
        with self._lock:
            load: Dict[str, int] = {}
            for entry in self._programs.values():
                if entry.instance:
                    load[entry.instance] = load.get(entry.instance, 0) + int(entry.prompt_tokens or 0)
            return load

    def pin_counts(self) -> Dict[str, int]:
        with self._lock:
            counts: Dict[str, int] = {}
            for entry in self._programs.values():
                if entry.instance:
                    counts[entry.instance] = counts.get(entry.instance, 0) + 1
            return counts

    def assign(self, program_id: str, live: Dict[str, Dict[str, Any]]) -> Optional[str]:
        """Pin the program if it has no home, or re-home it if its home died."""
        with self._lock:
            entry = self._programs[program_id]
            healthy = [name for name, info in live.items() if info.get("healthy", False)]
            if not healthy:
                return None
            if self.placement == "roundrobin":
                target = healthy[self._rr % len(healthy)]
                self._rr += 1
                if entry.instance and entry.instance != target:
                    entry.last_move = {
                        "from": entry.instance,
                        "to": target,
                        "mode": "roundrobin",
                        "at": time.time(),
                    }
                entry.instance = target
                return target
            home = entry.instance
            if home and live.get(home, {}).get("healthy", False):
                return home
            target = self.best_instance(live, pin_counts=self.pin_counts(),
                                        token_load=self.token_load(), cap=_PLACEMENT_CAP())
            if target is None:
                return None
            if home and home != target:
                entry.last_move = {
                    "from": home,
                    "to": target,
                    "mode": "failover",
                    "at": time.time(),
                }
            entry.instance = target
            return target

    # -- in-flight accounting -------------------------------------------

    def begin_request(self, program_id: str) -> None:
        with self._lock:
            entry = self._programs.get(program_id)
            if entry is not None:
                entry.in_flight += 1

    def end_request(self, program_id: str) -> None:
        with self._lock:
            entry = self._programs.get(program_id)
            if entry is not None:
                entry.in_flight = max(0, entry.in_flight - 1)

    # -- migration ------------------------------------------------------

    def begin_migration(self, program_id: str, target: str, mode: str) -> ProgramEntry:
        with self._lock:
            entry = self._programs.get(program_id)
            if entry is None:
                raise KeyError(f"unknown program {program_id!r}")
            if entry.migrating:
                raise RuntimeError(f"program {program_id!r} is already migrating")
            if entry.instance == target:
                raise ValueError(f"program {program_id!r} already lives on {target!r}")
            entry.migrating = True
            entry.migrating_since = time.time()
            entry.migrate_target = target
            entry.migrate_mode = mode
            return entry

    def finish_migration(self, program_id: str, ok: bool, waited_ms: int) -> Optional[ProgramEntry]:
        with self._lock:
            entry = self._programs.get(program_id)
            if entry is None:
                return None
            previous = entry.instance
            if ok and entry.migrate_target:
                entry.instance = entry.migrate_target
                entry.migrations += 1
            entry.last_move = {
                "from": previous,
                "to": entry.instance,
                "mode": entry.migrate_mode,
                "ok": ok,
                "waited_ms": waited_ms,
                "at": time.time(),
            }
            entry.migrating = False
            entry.migrating_since = None
            entry.migrate_target = None
            entry.migrate_mode = None
            return entry

    # -- release --------------------------------------------------------

    def release(self, program_id: str) -> bool:
        with self._lock:
            return self._programs.pop(program_id, None) is not None

    def snapshot(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [e.to_dict() for e in self._programs.values()]

    def counts(self) -> Dict[str, int]:
        with self._lock:
            return {
                "programs": len(self._programs),
                "migrating": sum(1 for e in self._programs.values() if e.migrating),
                "in_flight": sum(e.in_flight for e in self._programs.values()),
                "pinned": sum(1 for e in self._programs.values() if e.instance),
                "warm_ready": sum(1 for e in self._programs.values() if e.prompt_body is not None),
            }
