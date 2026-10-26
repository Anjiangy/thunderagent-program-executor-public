"""Launch ThunderAgent with config set BEFORE app.py is imported.

`ThunderAgent/__init__.py` imports `.app`, and app.py builds a router at import
time from the global config. Importing the package therefore freezes the
default backends (http://localhost:8000) before any CLI flag is read. This
launcher registers a stub package in sys.modules, imports only
`ThunderAgent.config`, sets the config, then imports `ThunderAgent.app`.
"""
import argparse
import sys
import types
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backends", default="http://127.0.0.1:8022,http://127.0.0.1:8023")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=9001)
    ap.add_argument("--package-root", default=None)
    args = ap.parse_args()

    root = Path(args.package_root or Path(__file__).resolve().parent)
    pkg = types.ModuleType("ThunderAgent")
    pkg.__path__ = [str(root / "ThunderAgent")]
    sys.modules["ThunderAgent"] = pkg

    from ThunderAgent.config import Config, set_config

    backends = [b.strip() for b in args.backends.split(",") if b.strip()]
    set_config(Config(backends=backends, router_mode="tr", metrics_enabled=True,
                      metrics_interval=2.0, scheduler_interval=2.0))

    import uvicorn
    from ThunderAgent.app import app

    print(f"[ta_launch] backends={backends} port={args.port}", flush=True)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
