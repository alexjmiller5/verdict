"""The `verdict` command: the server (`serve`) and the agent-facing client."""

import argparse
import json
import os
import re
import signal
import socket
import subprocess
import sys
import time
from base64 import b64encode
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from .config import Settings

WAIT_SLICE = 30.0  # one long-poll request; also the waiter heartbeat interval


def client(settings: Settings) -> httpx.Client:
    return httpx.Client(base_url=settings.url.rstrip("/"), timeout=WAIT_SLICE + 10)


def collect(path: Path) -> dict[str, str]:
    """A directory becomes the review's files; a single file becomes index.html."""
    if path.is_file():
        return {"index.html": b64encode(path.read_bytes()).decode()}
    if not (path / "index.html").is_file():
        raise SystemExit(f"{path} has no index.html")
    return {
        str(file.relative_to(path)): b64encode(file.read_bytes()).decode()
        for file in path.rglob("*")
        if file.is_file() and not file.name.startswith(".")
    }


def guess_title(files: dict[str, str], fallback: str) -> str:
    import base64

    page = base64.b64decode(files["index.html"]).decode("utf-8", errors="replace")
    match = re.search(r"<title>(.*?)</title>", page, re.IGNORECASE | re.DOTALL)
    return " ".join(match.group(1).split()) if match else fallback


def age(iso: str) -> str:
    delta = datetime.now(UTC) - datetime.fromisoformat(iso)
    hours = delta.total_seconds() / 3600
    return f"{delta.days}d" if hours >= 48 else f"{int(hours)}h" if hours >= 1 else "<1h"


def stale_reviews(reviews: list[dict], days: int) -> list[dict]:
    cutoff = datetime.now(UTC) - timedelta(days=days)
    return [
        r
        for r in reviews
        if r["status"] != "closed" and datetime.fromisoformat(r["updated"]) < cutoff
    ]


def warn_stale(http: httpx.Client, settings: Settings) -> None:
    stale = stale_reviews(http.get("/api/reviews").json()["reviews"], settings.stale_days)
    if stale:
        ids = ", ".join(f"{r['id']} ({r['title']})" for r in stale)
        print(
            f"warning: {len(stale)} stale review(s) still open: {ids}. Run `verdict gc`.",
            file=sys.stderr,
        )


# --- commands -------------------------------------------------------------


def publish(args, settings: Settings) -> int:
    files = collect(Path(args.path))
    title = args.title or guess_title(files, Path(args.path).stem)
    with client(settings) as http:
        r = http.post("/api/reviews", json={"title": title, "task": args.task, "files": files})
        r.raise_for_status()
        review = r.json()
        print(review["url"])
        print(f"id: {review['id']}", file=sys.stderr)
        warn_stale(http, settings)
    return 0


def wait(args, settings: Settings) -> int:
    deadline = time.monotonic() + args.timeout
    version = -1
    host = socket.gethostname()
    with client(settings) as http:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print(f"review {args.id} still open after {args.timeout:g}s", file=sys.stderr)
                return 3
            try:
                http.post(
                    f"/api/reviews/{args.id}/waiters", json={"pid": os.getpid(), "host": host}
                )
                r = http.get(
                    f"/api/reviews/{args.id}",
                    params={"wait": min(WAIT_SLICE, remaining), "version": version},
                )
            except httpx.TransportError as err:
                if version == -1:
                    raise  # never reached it: a wrong VERDICT_URL fails fast (exit 5)
                # it answered before, so this is a restart (a deploy): keep waiting
                print(f"verdict: server unreachable ({err}), retrying", file=sys.stderr)
                time.sleep(min(2.0, remaining))
                continue
            if r.status_code == 404:
                print(f"review {args.id} no longer exists", file=sys.stderr)
                return 4
            state = r.json()
            version = state["version"]
            if state["status"] in ("done", "closed"):
                print(json.dumps(state, indent=1))
                return 0


def show(args, settings: Settings) -> int:
    with client(settings) as http:
        r = http.get(f"/api/reviews/{args.id}")
        if r.status_code == 404:
            print(f"review {args.id} not found", file=sys.stderr)
            return 4
        print(json.dumps(r.json(), indent=1))
    return 0


def list_reviews(args, settings: Settings) -> int:
    with client(settings) as http:
        reviews = http.get("/api/reviews").json()["reviews"]
    stale = {r["id"] for r in stale_reviews(reviews, settings.stale_days)}
    for r in reviews:
        if args.all or r["status"] != "closed":
            flag = " STALE" if r["id"] in stale else ""
            waiters = f" waiters={len(r['waiters'])}" if r["waiters"] else ""
            print(
                f"{r['id']}  {r['status']:<6} {age(r['updated']):>4}  {r['title']}{flag}{waiters}"
            )
    return 0


def close(args, settings: Settings) -> int:
    with client(settings) as http:
        r = http.post(f"/api/reviews/{args.id}/close")
        if r.status_code == 404:
            print(f"review {args.id} not found", file=sys.stderr)
            return 4
        r.raise_for_status()
    print(f"closed {args.id}")
    return 0


def local_wait_pids() -> dict[int, str]:
    """`verdict wait <id>` processes on this machine, by pid."""
    out = subprocess.run(
        ["ps", "-axo", "pid=,command="], check=False, capture_output=True, text=True
    ).stdout
    found = {}
    for line in out.splitlines():
        match = re.match(r"\s*(\d+)\s+(.*)$", line)
        if match and re.search(r"\bverdict\s+wait\s+(\S+)", match.group(2)):
            pid = int(match.group(1))
            if pid != os.getpid():
                found[pid] = re.search(r"\bverdict\s+wait\s+(\S+)", match.group(2)).group(1)
    return found


def kill(pid: int) -> None:
    os.kill(pid, signal.SIGTERM)


def gc(args, settings: Settings) -> int:
    with client(settings) as http:
        report = http.post(
            "/api/gc", json={"stale_days": settings.stale_days, "purge_days": settings.purge_days}
        ).json()
        reviews = {r["id"]: r for r in http.get("/api/reviews").json()["reviews"]}
    killed = []
    for pid, rid in local_wait_pids().items():
        review = reviews.get(rid)
        if review is not None and review["status"] in ("done", "closed"):
            kill(pid)
            killed.append(pid)
    print(f"purged closed reviews: {', '.join(report['purged']) or 'none'}")
    print(f"dead waiters forgotten: {len(report['dead_waiters'])}")
    print(f"orphan wait processes killed here: {', '.join(map(str, killed)) or 'none'}")
    if report["stale_open"]:
        print("STALE (open longer than the stale window, close or finish them):")
        for rid in report["stale_open"]:
            r = reviews.get(rid, {})
            print(
                f"  {rid}  {r.get('status', '?'):<6} {age(r['updated']) if r else '':>4}  {r.get('title', '')}"
            )
    return 0


def serve(args, settings: Settings) -> int:
    from .server import serve as run_server
    from .store import Store

    settings.state_dir.mkdir(parents=True, exist_ok=True)
    run_server(Store(settings.state_dir), settings.bind, settings.port)
    return 0


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="verdict", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "publish", help="publish a review page (a directory with index.html, or one HTML file)"
    )
    p.add_argument("path")
    p.add_argument("--title", help="defaults to the page <title>")
    p.add_argument("--task", help="the task this review belongs to (any id)")
    p.set_defaults(fn=publish)

    p = sub.add_parser("wait", help="block until the review is done, then print its state as JSON")
    p.add_argument("id")
    p.add_argument(
        "--timeout", type=float, default=8 * 3600, help="seconds (default 8h); exit 3 on timeout"
    )
    p.set_defaults(fn=wait)

    p = sub.add_parser("show", help="print the review state as JSON")
    p.add_argument("id")
    p.set_defaults(fn=show)

    p = sub.add_parser("list", help="list reviews (open and done; --all includes closed)")
    p.add_argument("--all", action="store_true")
    p.set_defaults(fn=list_reviews)

    p = sub.add_parser("close", help="mark a review consumed; it is purged after the purge window")
    p.add_argument("id")
    p.set_defaults(fn=close)

    p = sub.add_parser(
        "gc",
        help="purge old closed reviews, forget dead waiters, kill orphan wait processes, list stale reviews",
    )
    p.set_defaults(fn=gc)

    p = sub.add_parser(
        "serve", help="run the server (VERDICT_BIND, VERDICT_PORT, VERDICT_STATE_DIR)"
    )
    p.set_defaults(fn=serve)

    args = parser.parse_args(argv)
    try:
        return args.fn(args, Settings())
    except httpx.HTTPError as err:
        print(f"verdict: cannot reach the server ({err})", file=sys.stderr)
        return 5


if __name__ == "__main__":
    sys.exit(cli())
