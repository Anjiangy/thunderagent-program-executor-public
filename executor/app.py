"""Program executor: a thin, program-aware front door in front of N vLLM instances.

It does five things and nothing else:

1. ``GET  /metrics``                  cluster-scoped view of all instances, in the
                                      metric names ThunderAgent's parser expects.
2. ``POST /v1/chat/completions``      OpenAI-compatible; routes each program to
   ``POST /v1/completions``           the instance it is pinned to (program
                                      affinity), and forwards anything else.
3. ``GET  /v1/programs[/{pid}]``      observability: where each program lives,
                                      how many requests it has served.
4. ``POST /v1/programs/{pid}/migrate`` move a program's pin. ``mode=recompute``
                                      just re-homes it; ``mode=kv`` first replays
                                      the program's prompt into the target so its
                                      prefix cache is already warm when the pin
                                      moves.
5. ``EXECUTOR_BALANCE=skew``          optional background balancer: when one
                                      instance is hot and another is cold, move
                                      the biggest program off the hot one.

It deliberately owns NO admission or throttling logic: that stays in the
controller. The executor only answers "which instance", never "may I".
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional, Tuple

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse

from .cluster_metrics import aggregate_backends, is_ready, render_prometheus
from .programs import ProgramTable
from .vllm_metrics import parse_metrics, unhealthy

SCRAPE_TIMEOUT = float(os.environ.get("EXECUTOR_SCRAPE_TIMEOUT", "2.0"))
SCRAPE_INTERVAL = float(os.environ.get("EXECUTOR_SCRAPE_INTERVAL", "2.0"))
MIGRATE_TIMEOUT = float(os.environ.get("EXECUTOR_MIGRATE_TIMEOUT", "60.0"))
WARM_TIMEOUT = float(os.environ.get("EXECUTOR_WARM_TIMEOUT", "180.0"))
BALANCE_INTERVAL = float(os.environ.get("EXECUTOR_BALANCE_INTERVAL", "10.0"))
BALANCE_HOT = float(os.environ.get("EXECUTOR_BALANCE_HOT", "0.90"))
BALANCE_COLD = float(os.environ.get("EXECUTOR_BALANCE_COLD", "0.65"))
BALANCE_MAX_MOVES = int(os.environ.get("EXECUTOR_BALANCE_MAX_MOVES", "200"))
BALANCE_MODE = os.environ.get("EXECUTOR_BALANCE_MODE", "kv").strip().lower()
# "transfer" pulls blocks over the peer channel; "recompute" re-prefills.
MIGRATE_CARRIER = os.environ.get("EXECUTOR_MIGRATE_CARRIER", "recompute").strip().lower()
# When the KV pull fails, "none" keeps the program on the source card and reports the error
# instead of silently replaying the prefix on the target (which hides the failure).
MIGRATE_FALLBACK = os.environ.get("EXECUTOR_MIGRATE_FALLBACK", "recompute").strip().lower()
DEFAULT_PROGRAM = "default"


def parse_backends(spec: str) -> Dict[str, str]:
    """``"a=http://127.0.0.1:8022,b=http://127.0.0.1:8023"`` -> ``{a: url, b: url}``."""
    out: Dict[str, str] = {}
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError(f"backend spec {part!r} must look like name=url")
        name, url = part.split("=", 1)
        out[name.strip()] = url.strip().rstrip("/")
    if not out:
        raise ValueError("no backends configured")
    return out


class State:
    """Everything the request path needs, in one place."""

    def __init__(self, backends: Dict[str, str]) -> None:
        self.backends = backends
        self.table = ProgramTable()
        self.client: Optional[httpx.AsyncClient] = None
        self.live: Dict[str, Dict[str, Any]] = {name: unhealthy("not scraped") for name in backends}
        self.scraped_at: float = 0.0
        self.balance = os.environ.get("EXECUTOR_BALANCE", "off").strip().lower()
        self.moves: int = 0
        self._stop = asyncio.Event()

    async def scrape_all(self) -> Dict[str, Dict[str, Any]]:
        assert self.client is not None

        async def one(name: str, base: str) -> Tuple[str, Dict[str, Any]]:
            try:
                r = await self.client.get(base + "/metrics", timeout=SCRAPE_TIMEOUT)
                r.raise_for_status()
                return name, parse_metrics(r.text)
            except Exception as exc:  # noqa: BLE001 - any failure means "unhealthy"
                return name, unhealthy(f"{type(exc).__name__}: {exc}"[:200])

        results = await asyncio.gather(*(one(n, u) for n, u in self.backends.items()))
        self.live = dict(results)
        self.scraped_at = time.time()
        return self.live

    async def refresh_loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.scrape_all()
            except Exception:  # noqa: BLE001 - a failed sweep must not kill the loop
                pass
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=SCRAPE_INTERVAL)
            except asyncio.TimeoutError:
                continue

    async def balance_loop(self) -> None:
        """Move the biggest program off a hot instance onto a cold one."""
        while not self._stop.is_set():
            try:
                if self.moves < BALANCE_MAX_MOVES:
                    self.moves += await self._balance_once()
            except Exception:  # noqa: BLE001 - balancing must never take the server down
                pass
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=BALANCE_INTERVAL)
            except asyncio.TimeoutError:
                continue

    def _demand_ratio(self, name: str) -> float:
        """Demand, not occupancy: what this card would hold if nothing evicted.

        ``kv_cache_usage_perc`` collapses back to ~0 as soon as the engine evicts,
        so it cannot tell a crowded card from an idle one. The prefixes a card is
        pinned to is the honest signal -- it is exactly the work that would be
        recomputed if the card stays over its budget.
        """
        info = self.live.get(name) or {}
        capacity = float(info.get("kv_capacity_tokens") or 0.0)
        if capacity <= 0:
            block = float(info.get("block_size") or 0.0)
            blocks = float(info.get("num_gpu_blocks") or 0.0)
            capacity = block * blocks
        if capacity <= 0:
            return 0.0
        demand = 0.0
        for entry in self.table.entries():
            if entry.instance == name:
                demand += float(entry.prompt_tokens or 0.0)
        return demand / capacity

    async def _balance_once(self) -> int:
        usable = {n: i for n, i in self.live.items() if i.get("healthy")}
        if len(usable) < 2:
            return 0
        usage = {n: self._demand_ratio(n) for n in usable}
        hot = max(usage, key=lambda n: usage[n])
        cold = min(usage, key=lambda n: usage[n])
        print(f"[bal] usage={ {n: round(v, 3) for n, v in usage.items()} } "
              f"hot={usage[hot]:.3f}/{(BALANCE_HOT)} cold={usage[cold]:.3f}/({BALANCE_COLD}) "
              f"moves={self.moves}", flush=True)
        if hot == cold or usage[hot] < BALANCE_HOT or usage[cold] > BALANCE_COLD:
            return 0
        candidates = self.table.migration_candidates(hot)
        if not candidates:
            return 0
        code, _ = await _do_migrate(candidates[0], cold, BALANCE_MODE,
                                    MIGRATE_TIMEOUT, allow_degraded=True)
        return 1 if code == 200 else 0


STATE: State


def extract_program_id(body: Dict[str, Any], headers: Any) -> str:
    """Same resolution order ThunderAgent uses: body, extra_body, session header."""
    hint = body.get("program_id_hint")
    if isinstance(hint, str) and hint.strip():
        return hint.strip()
    for key in ("program_id", "session_id"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    extra = body.get("extra_body")
    if isinstance(extra, dict):
        for key in ("program_id", "session_id"):
            value = extra.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    for header in ("x-program-id", "x-session-id"):
        value = headers.get(header)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return DEFAULT_PROGRAM


def strip_program_fields(body: Dict[str, Any]) -> Dict[str, Any]:
    """vLLM must not receive our routing keys."""
    payload = dict(body)
    payload.pop("program_id", None)
    payload.pop("program_id_hint", None)
    payload.pop("session_id", None)
    extra = payload.get("extra_body")
    if isinstance(extra, dict):
        cleaned = {k: v for k, v in extra.items() if k not in ("program_id", "session_id")}
        if cleaned:
            payload["extra_body"] = cleaned
        else:
            payload.pop("extra_body", None)
    return payload


@asynccontextmanager
async def lifespan(app: FastAPI):
    global STATE
    STATE = State(parse_backends(os.environ.get("EXECUTOR_BACKENDS", "a=http://127.0.0.1:8022,b=http://127.0.0.1:8023")))
    STATE.table.placement = os.environ.get("EXECUTOR_PLACEMENT", "affinity").strip().lower()
    STATE.client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=5.0))
    await STATE.scrape_all()
    tasks = [asyncio.create_task(STATE.refresh_loop())]
    if STATE.balance in ("skew", "on", "1", "true"):
        tasks.append(asyncio.create_task(STATE.balance_loop()))
    try:
        yield
    finally:
        STATE._stop.set()
        for task in tasks:
            task.cancel()
        await STATE.client.aclose()


app = FastAPI(title="program-executor", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> Dict[str, Any]:
    return {
        "ok": True,
        "placement": STATE.table.placement,
        "balance": STATE.balance,
        "balance_moves": STATE.moves,
        "balance_thresholds": {"hot": BALANCE_HOT, "cold": BALANCE_COLD, "mode": BALANCE_MODE},
        "backends": STATE.backends,
        "healthy": [n for n, i in STATE.live.items() if i.get("healthy")],
        "scraped_at": STATE.scraped_at,
        "programs": STATE.table.counts(),
    }


@app.get("/metrics")
async def metrics() -> Response:
    aggregate = aggregate_backends(STATE.live.values())
    aggregate["programs_pinned"] = STATE.table.pin_counts()
    if not is_ready(aggregate):
        return PlainTextResponse(render_prometheus(aggregate), status_code=503,
                                 media_type="text/plain; version=0.0.4")
    return PlainTextResponse(render_prometheus(aggregate), media_type="text/plain; version=0.0.4")


@app.get("/v1/models")
async def models() -> Response:
    for name, info in STATE.live.items():
        if not info.get("healthy"):
            continue
        assert STATE.client is not None
        try:
            r = await STATE.client.get(STATE.backends[name] + "/v1/models", timeout=SCRAPE_TIMEOUT)
            return Response(content=r.content, status_code=r.status_code,
                            media_type=r.headers.get("content-type", "application/json"))
        except Exception:  # noqa: BLE001 - try the next instance
            continue
    return JSONResponse({"error": "no healthy backend"}, status_code=503)


@app.get("/v1/programs")
async def list_programs() -> Dict[str, Any]:
    return {"counts": STATE.table.counts(), "programs": STATE.table.snapshot()}


@app.get("/v1/programs/{program_id}")
async def get_program(program_id: str) -> Response:
    entry = STATE.table.get(program_id)
    if entry is None:
        return JSONResponse({"error": f"unknown program {program_id!r}"}, status_code=404)
    return JSONResponse(entry.to_dict())


@app.post("/v1/programs/{program_id}/seed")
async def seed_program(program_id: str, request: Request) -> Response:
    """Pin a program without migrating it.

    The benchmark uses this to build a skewed layout on purpose: migrating a
    program that has no prompt yet would only burn a migration slot, while a
    seed is exactly "pretend this program already lives here".
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - an empty body is fine
        body = {}
    target = body.get("target") or body.get("instance")
    if not isinstance(target, str) or target not in STATE.backends:
        return JSONResponse({"error": f"unknown target {target!r}"}, status_code=400)
    entry = STATE.table.touch(program_id)
    entry.instance = target
    return JSONResponse({"ok": True, "program_id": program_id, "target": target})


@app.delete("/v1/programs/{program_id}")
async def release_program(program_id: str) -> Dict[str, Any]:
    return {"released": STATE.table.release(program_id)}


async def _warm_target(program_id: str, target: str, timeout_s: float) -> Dict[str, Any]:
    """Re-materialise the program's prefix on ``target`` before moving the pin.

    This is route A's migration. There is no block mover on this stack (M0: the
    connector path is dead on Ascend), so instead of transferring KV blocks we
    replay the program's own prompt into the target with ``max_tokens=1``. vLLM
    prefills it and keeps the blocks in the target's prefix cache, so the
    program's next real turn is a cache hit there instead of a cold prefill.

    Cost is one prefill of the context; the point is that it can happen while
    the program is paused or waiting on a tool, off the critical path.
    """
    entry = STATE.table.get(program_id)
    result: Dict[str, Any] = {"ok": False}
    if entry is None or not entry.prompt_body:
        result["error"] = "no recorded prompt for this program"
        return result
    payload = dict(entry.prompt_body)
    payload["stream"] = False
    payload.pop("stream_options", None)
    payload["max_tokens"] = 1
    payload["temperature"] = 0
    url = STATE.backends[target] + (entry.prompt_path or "/v1/chat/completions")
    client = STATE.client
    assert client is not None
    started = time.time()
    try:
        r = await client.post(url, json=payload, headers={"content-type": "application/json"},
                              timeout=timeout_s)
    except Exception as exc:  # noqa: BLE001 - a failed warm aborts the migration
        result["error"] = f"{type(exc).__name__}: {exc}"[:200]
        return result
    result["status"] = r.status_code
    result["warm_ms"] = int((time.time() - started) * 1000)
    if r.status_code != 200:
        result["error"] = r.text[:200]
        return result
    try:
        usage = r.json().get("usage") or {}
        result["warmed_prompt_tokens"] = int(usage.get("prompt_tokens") or 0)
    except Exception:  # noqa: BLE001 - usage is best effort
        result["warmed_prompt_tokens"] = 0
    STATE.table.record_warmed(program_id, int(result["warmed_prompt_tokens"]))
    result["ok"] = True
    return result


async def _capture_kvp(program_id: str, source: str, timeout_s: float) -> Dict[str, Any]:
    """Ask ``source`` for a handshake blob covering the program's prefix.

    This is the cheap half of a real move: the source already holds the blocks in
    its prefix cache, so this request is a cache hit, not a fresh prefill. The
    returned ``kv_transfer_params`` is what the target needs in order to pull.
    """
    entry = STATE.table.get(program_id)
    result: Dict[str, Any] = {"ok": False}
    if entry is None or not entry.prompt_body:
        result["error"] = "no recorded prompt for this program"
        return result
    payload = dict(entry.prompt_body)
    payload["stream"] = False
    payload.pop("stream_options", None)
    payload["max_tokens"] = 1
    payload["temperature"] = 0
    payload["kv_transfer_params"] = {
        "do_remote_decode": True,
        "do_remote_prefill": False,
        "transfer_id": f"kvp-{program_id}-{int(time.time() * 1000)}",
    }
    url = STATE.backends[source] + (entry.prompt_path or "/v1/chat/completions")
    client = STATE.client
    assert client is not None
    started = time.time()
    try:
        r = await client.post(url, json=payload, headers={"content-type": "application/json"},
                              timeout=timeout_s)
    except Exception as exc:  # noqa: BLE001 - a failed capture aborts the migration
        result["error"] = f"{type(exc).__name__}: {exc}"[:200]
        return result
    result["status"] = r.status_code
    result["capture_ms"] = int((time.time() - started) * 1000)
    if r.status_code != 200:
        result["error"] = r.text[:200]
        return result
    try:
        kvp = (r.json() or {}).get("kv_transfer_params") or {}
    except Exception:  # noqa: BLE001
        kvp = {}
    if not kvp.get("remote_block_ids"):
        result["error"] = "source did not return a handshake blob"
        return result
    STATE.table.set_kvp(program_id, kvp)
    result["ok"] = True
    result["blocks"] = len(kvp.get("remote_block_ids") or [])
    return result


async def _pull_target(program_id: str, target: str, timeout_s: float) -> Dict[str, Any]:
    """Make the target fetch the blocks while the program is still idle.

    Doing the pull here, inside the migration window, keeps a stalled peer from
    hanging the program's next real turn. If it fails we still hold a usable
    pin-move decision, so the caller can fall back to a warm replay.
    """
    entry = STATE.table.get(program_id)
    result: Dict[str, Any] = {"ok": False}
    if entry is None or not entry.prompt_body:
        result["error"] = "no recorded prompt for this program"
        return result
    kvp = STATE.table.take_kvp(program_id)
    if not kvp:
        result["error"] = "no handshake blob to inject"
        return result
    payload = dict(entry.prompt_body)
    payload["stream"] = False
    payload.pop("stream_options", None)
    payload["max_tokens"] = 1
    payload["temperature"] = 0
    blob = dict(kvp)
    blob.update({"do_remote_prefill": True, "do_remote_decode": False})
    payload["kv_transfer_params"] = blob
    url = STATE.backends[target] + (entry.prompt_path or "/v1/chat/completions")
    client = STATE.client
    assert client is not None
    started = time.time()
    try:
        r = await client.post(url, json=payload, headers={"content-type": "application/json"},
                              timeout=timeout_s)
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"[:200]
        return result
    result["status"] = r.status_code
    result["pull_ms"] = int((time.time() - started) * 1000)
    if r.status_code != 200:
        result["error"] = r.text[:200]
        return result
    result["ok"] = True
    return result


async def _do_migrate(program_id: str, target: str, mode: str, timeout_s: float,
                      allow_degraded: bool) -> Tuple[int, Dict[str, Any]]:
    """Drain the program, optionally warm the target, then move the pin."""
    try:
        entry = STATE.table.begin_migration(program_id, target, mode)
    except KeyError as exc:
        return 404, {"error": str(exc)}
    except (RuntimeError, ValueError) as exc:
        return 409, {"error": str(exc)}

    from_instance = entry.instance
    started = time.time()
    while True:
        current = STATE.table.get(program_id)
        if current is None:
            STATE.table.finish_migration(program_id, ok=False, waited_ms=0)
            return 404, {"error": "program vanished during migration"}
        if current.in_flight <= 0:
            break
        if time.time() - started > timeout_s:
            STATE.table.finish_migration(program_id, ok=False,
                                         waited_ms=int((time.time() - started) * 1000))
            return 409, {"error": "timed out waiting for in-flight requests to drain",
                         "in_flight": current.in_flight}
        await asyncio.sleep(0.05)

    waited_ms = int((time.time() - started) * 1000)
    warm: Dict[str, Any] = {"ok": False, "skipped": True}
    carrier = "recompute"
    if mode == "kv":
        budget = min(timeout_s, WARM_TIMEOUT) if timeout_s else WARM_TIMEOUT
        if MIGRATE_CARRIER == "transfer":
            carrier = "transfer"
            warm = await _capture_kvp(program_id, from_instance, budget)
            if warm.get("ok"):
                # Finish the pull here, while the program is idle, so the next
                # real turn cannot be held hostage by a slow peer.
                pull = await _pull_target(program_id, target, budget)
                warm["pull"] = pull
                if not pull.get("ok"):
                    warm["ok"] = False
            if not warm.get("ok"):
                # Falling back keeps the router usable on a stack whose peer
                # handshake is broken; the entry keeps its pin either way.
                if MIGRATE_FALLBACK == "none":
                    warm["fallback"] = "none"
                    # Leaving `migrating` set would reject every later request for this
                    # program with 503 and never let the window close (observed as a
                    # 5000-line wall of 503s in the step-3 run).
                    STATE.table.finish_migration(program_id, ok=False, waited_ms=waited_ms)
                    return 409, {"error": "kv pull failed and fallback disabled", "detail": warm}
                warm["fallback"] = "recompute"
                carrier = "recompute"
                warm2 = await _warm_target(program_id, target, budget)
                warm["fallback_result"] = warm2
                warm["ok"] = warm2.get("ok", False)
                if not warm["ok"]:
                    warm = warm2
        else:
            warm = await _warm_target(program_id, target, budget)
        if not warm.get("ok") and not allow_degraded:
            STATE.table.finish_migration(program_id, ok=False, waited_ms=waited_ms)
            return 409, {"error": "kv migration could not prepare the target, pin unchanged",
                         "warm": warm, "carrier": carrier,
                         "hint": "pass allow_degraded=true to move the pin anyway"}

    entry = STATE.table.finish_migration(program_id, ok=True, waited_ms=waited_ms)
    note = {
        "recompute": "pin moved; next request re-prefills on the target",
        "kv": "prefix re-materialised on the target before the pin moved",
    }[mode]
    if mode == "kv" and not warm.get("ok"):
        note = "warm failed but allow_degraded was set; target will prefill cold"
    return 200, {
        "ok": True,
        "program_id": program_id,
        "from": from_instance,
        "to": target,
        "mode": mode,
        "waited_ms": waited_ms,
        "warm": warm,
        "carrier": carrier,
        "note": note,
        "entry": entry.to_dict() if entry else None,
    }


@app.post("/v1/programs/{program_id}/migrate")
async def migrate(program_id: str, request: Request) -> Response:
    body = await request.json()
    target = str(body.get("target") or "")
    mode = str(body.get("mode") or "recompute")
    timeout_s = float(body.get("timeout_s") or MIGRATE_TIMEOUT)
    allow_degraded = bool(body.get("allow_degraded", False))

    if target not in STATE.backends:
        return JSONResponse({"error": f"unknown target {target!r}",
                             "known": list(STATE.backends)}, status_code=400)
    if not STATE.live.get(target, {}).get("healthy", False):
        return JSONResponse({"error": f"target {target!r} is not healthy"}, status_code=409)
    if mode not in ("recompute", "kv"):
        return JSONResponse({"error": f"unknown mode {mode!r}"}, status_code=400)

    code, payload = await _do_migrate(program_id, target, mode, timeout_s, allow_degraded)
    return JSONResponse(payload, status_code=code)


async def _route(program_id: str, body: Dict[str, Any], request: Request,
                 upstream_path: str, stream: bool) -> Response:
    entry = STATE.table.touch(program_id)
    if entry.migrating:
        return JSONResponse(
            {"error": "program is migrating", "program_id": program_id,
             "target": entry.migrate_target, "mode": entry.migrate_mode},
            status_code=503, headers={"Retry-After": "1"})

    target = STATE.table.assign(program_id, STATE.live)
    if target is None:
        return JSONResponse({"error": "no healthy backend"},
                            status_code=503, headers={"Retry-After": "1"})

    payload = strip_program_fields(body)
    STATE.table.record_prompt(program_id, upstream_path, payload)
    if entry.pending_kvp and target == entry.instance:
        # Stale blob from an earlier attempt: the pull happens at migration time
        # now, so anything left here would only slow the request down.
        STATE.table.take_kvp(program_id)
    url = STATE.backends[target] + upstream_path
    headers = {"content-type": "application/json"}
    auth = request.headers.get("authorization")
    if auth:
        headers["authorization"] = auth

    STATE.table.begin_request(program_id)
    client = STATE.client
    assert client is not None

    if not stream:
        try:
            r = await client.post(url, json=payload, headers=headers)
        finally:
            STATE.table.end_request(program_id)
        return Response(content=r.content, status_code=r.status_code,
                        media_type=r.headers.get("content-type", "application/json"),
                        headers={"x-executor-instance": target})

    try:
        upstream = await client.send(
            client.build_request("POST", url, json=payload, headers=headers),
            stream=True)
    except Exception as exc:  # noqa: BLE001
        STATE.table.end_request(program_id)
        return JSONResponse({"error": f"upstream {target} failed: {exc}"}, status_code=502)

    async def body_iter():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            STATE.table.end_request(program_id)

    return StreamingResponse(
        body_iter(),
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "text/event-stream"),
        headers={"x-executor-instance": target})


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> Response:
    body: Dict[str, Any] = await request.json()
    program_id = extract_program_id(body, request.headers)
    return await _route(program_id, body, request, "/v1/chat/completions",
                        bool(body.get("stream")))


@app.post("/v1/completions")
async def completions(request: Request) -> Response:
    body: Dict[str, Any] = await request.json()
    program_id = extract_program_id(body, request.headers)
    return await _route(program_id, body, request, "/v1/completions",
                        bool(body.get("stream")))
