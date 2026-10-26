#!/usr/bin/env python3
"""Patch the vendored controller adapter to accept N backends (idempotent).

Default upstream entry (``thunderagent_controller_mod.__main__``) hard-refuses
more than one endpoint.  Path B needs the controller to see *each card* as its
own backend so that its pause/resume capacity checks match what the execution
side can actually place.  Only the adapter files are touched (the pinned
upstream core stays byte-identical, its manifest still verifies).
"""
from __future__ import annotations

import sys
from pathlib import Path

REPLACEMENTS = {
    "__main__.py": [
        (
            "    if len(args.endpoint) != 1:\n"
            "        parser.error('This verified thin adapter currently supports exactly one backend')\n"
            "    backend=args.endpoint[0]\n"
            "    if not backend.startswith(('http://','https://')):\n"
            "        backend='http://'+backend\n",
            "    backends=[e if e.startswith(('http://','https://')) else 'http://'+e\n"
            "              for e in args.endpoint]\n",
        ),
        (
            "    app=create_application(backend, scheduler_interval=args.scrape_interval,",
            "    app=create_application(backends, scheduler_interval=args.scrape_interval,",
        ),
    ],
    "upstream_adapter.py": [
        (
            "def create_application(backend, *, scheduler_interval=5.0,",
            "def create_application(backends, *, scheduler_interval=5.0,",
        ),
        (
            "    config = Config(backends=[backend], router_mode='tr' if scheduling_enabled else 'default',",
            "    backends = [backends] if isinstance(backends, str) else list(backends)\n"
            "    config = Config(backends=backends, router_mode='tr' if scheduling_enabled else 'default',",
        ),
        (
            "            assert list(router.backends)==[backend]",
            "            assert len(list(router.backends))==len(backends)",
        ),
    ],
}


def patch(root: Path) -> int:
    changed = 0
    for name, pairs in REPLACEMENTS.items():
        path = root / name
        text = path.read_text(encoding="utf-8")
        for old, new in pairs:
            if old in text:
                text = text.replace(old, new)
                changed += 1
            elif new not in text:
                raise SystemExit(f"{name}: anchor missing and replacement absent")
        path.write_text(text, encoding="utf-8")
    return changed


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    n = patch(target)
    print(f"multi-backend patch: applied={n} (0 means already patched)")
