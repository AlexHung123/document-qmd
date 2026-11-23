from __future__ import annotations

import argparse
import os
import sys
import threading

from .config import Settings
from .httpapi import App, serve_http
from .layout import ensure_user_tree, list_user_ids
from .log import log
from .mcp import run_mcp
from .paths import parse_user_id
from .qmd import QmdRunner
from .queue import ControlStore
from .worker import Worker, worker_loop


def build_settings(args: argparse.Namespace) -> Settings:
    if getattr(args, "data_root", None):
        os.environ["DOCHUB_DATA_ROOT"] = args.data_root
    if getattr(args, "bind", None):
        os.environ["DOCHUB_BIND"] = args.bind
    if getattr(args, "port", None):
        os.environ["DOCHUB_PORT"] = str(args.port)
    if getattr(args, "qmd_bin", None):
        os.environ["DOCHUB_QMD_BIN"] = args.qmd_bin
    return Settings.from_env()


def build_app(settings: Settings) -> tuple[App, Worker]:
    settings.data_root.mkdir(parents=True, exist_ok=True)
    store = ControlStore(settings.queue_db)
    qmd = QmdRunner(settings)
    app = App(settings, store, qmd)
    worker = Worker(settings, store, qmd)
    return app, worker


def cmd_serve(args: argparse.Namespace) -> int:
    settings = build_settings(args)
    app, worker = build_app(settings)
    stop = threading.Event()
    worker_thread = None
    if not args.no_worker:
        worker_thread = threading.Thread(target=worker_loop, args=(worker, stop), name="dochub-worker", daemon=True)
        worker_thread.start()
    server = serve_http(app)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("shutting down")
    finally:
        stop.set()
        server.shutdown()
        if worker_thread:
            worker_thread.join(timeout=5)
        app.store.close()
    return 0


def cmd_worker(args: argparse.Namespace) -> int:
    settings = build_settings(args)
    _, worker = build_app(settings)
    stop = threading.Event()
    try:
        worker_loop(worker, stop)
    except KeyboardInterrupt:
        stop.set()
        log("worker interrupted")
    return 0


def cmd_ensure_user(args: argparse.Namespace) -> int:
    settings = build_settings(args)
    qmd = QmdRunner(settings)
    root = ensure_user_tree(settings.data_root, parse_user_id(args.user), qmd)
    print(root)
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    return run_mcp(url=args.url, user_id=args.user, folder=args.folder)


def cmd_check(args: argparse.Namespace) -> int:
    settings = build_settings(args)
    store = ControlStore(settings.queue_db)
    qmd = QmdRunner(settings)
    print(f"data_root={settings.data_root}")
    print(f"queue_db={settings.queue_db}")
    print(f"bind={settings.bind}:{settings.port}")
    print(f"qmd_bin={settings.qmd_bin} available={qmd.available()}")
    print(f"queue={store.counts()}")
    users = list_user_ids(settings.data_root)
    print(f"users={len(users)}")
    for uid in users:
        print(f"  {uid} {settings.users_root / uid}")
    store.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dochub",
        description="Mac file and index hub. Frontend authenticates users and forwards original plus markdown; this service stores, indexes, and searches.",
    )
    parser.add_argument("--data-root", help="Override DOCHUB_DATA_ROOT")
    parser.add_argument("--bind", help="Override DOCHUB_BIND")
    parser.add_argument("--port", type=int, help="Override DOCHUB_PORT")
    parser.add_argument("--qmd-bin", help="Override DOCHUB_QMD_BIN")
    sub = parser.add_subparsers(dest="cmd")

    serve = sub.add_parser("serve", help="HTTP API plus qmd index worker")
    serve.add_argument("--no-worker", action="store_true", help="Do not start the ingest worker in this process")
    serve.set_defaults(func=cmd_serve)

    worker = sub.add_parser("worker", help="Ingest worker only")
    worker.set_defaults(func=cmd_worker)

    ensure = sub.add_parser("ensure-user", help="Create a user root and local QMD index")
    ensure.add_argument("--user", required=True)
    ensure.set_defaults(func=cmd_ensure_user)

    check = sub.add_parser("check", help="Print data root, queue counts, users")
    check.set_defaults(func=cmd_check)

    mcp = sub.add_parser("mcp", help="Per-user MCP stdio server for pi-agent-core")
    mcp.add_argument("--url", help="Dochub HTTP URL (default DOCHUB_URL or http://127.0.0.1:8787)")
    mcp.add_argument("--user", help="User id (DOCHUB_USER_ID). Set by the host, never by the model.")
    mcp.add_argument("--folder", help="Optional folder pin under markdown/ (DOCHUB_FOLDER)")
    mcp.set_defaults(func=cmd_mcp)

    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
