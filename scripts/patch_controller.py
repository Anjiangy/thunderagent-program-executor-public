"""Inject a downstream program hint into the controller's forwarded payload.

Why this exists
---------------
ThunderAgent decides "which instance" itself, so it strips ``program_id`` from
the body before proxying (``scheduler/vllm_request_processor.remove_program_id``)
and forwards the httpx call with no headers at all. That is correct for its own
design, but it erases the one thing the program executor needs: the identity of
the program whose KV is pinned.

So we add a *hint* that upstream does not know about and therefore does not
strip. It travels in the JSON body to the executor, which removes it before the
request reaches vLLM. No pinned upstream file is touched (the SHA256 manifest
still verifies), only the thin adapter wrapper that lives in this repo.

The patch is idempotent: running it twice changes nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path

ANCHOR = "        if identified:\n            return await official_completion(request)\n"

REPLACEMENT = '''        if identified:
            return await official_completion(_with_program_hint(request, payload))
'''

HELPER = '''
def _with_program_hint(request, payload):
    """Return a Request whose body also carries ``program_id_hint``.

    Upstream strips ``program_id`` before forwarding and sends no headers, so the
    executor would see every request as the same default program. The hint rides
    along in the body instead; the executor deletes it before vLLM sees it.
    """
    import json as _json
    from starlette.requests import Request as _Request

    pid = payload.get('program_id')
    if pid is None:
        extra = payload.get('extra_body')
        if isinstance(extra, dict):
            pid = extra.get('program_id')
    if pid is None:
        pid = request.headers.get('x-session-id')
    pid = 'default' if pid is None else str(pid)

    forwarded = dict(payload)
    forwarded['program_id_hint'] = pid
    body = _json.dumps(forwarded).encode()

    async def receive():
        return {'type': 'http.request', 'body': body, 'more_body': False}

    scope = dict(request.scope)
    return _Request(scope, receive)

'''


def patch(adapter_path: Path) -> str:
    text = adapter_path.read_text(encoding="utf-8")
    if "_with_program_hint" in text:
        return "already patched"
    if ANCHOR not in text:
        raise SystemExit(f"anchor not found in {adapter_path}")

    text = text.replace(ANCHOR, REPLACEMENT)
    # Insert the helper just before create_application (module level).
    marker = "def create_application("
    idx = text.index(marker)
    text = text[:idx] + HELPER.lstrip("\n") + "\n" + text[idx:]
    adapter_path.write_text(text, encoding="utf-8")
    return "patched"


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: patch_controller.py <path to upstream_adapter.py>")
    print(patch(Path(sys.argv[1])))
