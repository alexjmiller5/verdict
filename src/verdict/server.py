"""The HTTP server: review pages with the injected client, and the JSON API.

Routes
  GET  /                              index of reviews
  GET  /r/<id>/                       the published page (+ injected client)
  GET  /r/<id>/<file>                 published static files
  GET  /_verdict/<asset>              the client script and stylesheet
  GET  /api/reviews                   list
  POST /api/reviews                   create {title, task, files: {name: base64}}
  GET  /api/reviews/<id>[?wait=s&version=n]   state, long-polling on version
  PUT  /api/reviews/<id>/state        merge {answers, comments, general, status}
  PUT  /api/reviews/<id>/files        replace the page {title?, files}; keeps answers, reopens
  POST /api/reviews/<id>/close        close the review
  POST /api/reviews/<id>/reopen       reopen, preserving answers and files
  POST /api/reviews/<id>/waiters      {pid, host} heartbeat from `verdict wait`
  POST /api/reviews/<id>/touch        backdate timestamps (gc rehearsal)
  POST /api/gc                        {stale_days, purge_days} -> report
"""

import base64
import html
import json
import mimetypes
import sys
from html.parser import HTMLParser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, urlsplit

import structlog

from .store import NotFound, Store, parse

# server logs go to stderr so a CLI sharing the process keeps stdout for data
structlog.configure(logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))
log = structlog.get_logger()
MAX_BODY = 64 * 1024 * 1024
INJECT = (
    '<link rel="stylesheet" href="/_verdict/verdict.css">'
    '<script defer src="/_verdict/verdict.js"></script>'
)


HEAD_DEFAULTS = {
    "viewport": '<meta name="viewport" content="width=device-width, initial-scale=1">',
    "icon": '<link rel="icon" type="image/svg+xml" href="/_verdict/favicon.svg">',
    "apple-touch-icon": '<link rel="apple-touch-icon" sizes="180x180" href="/_verdict/apple-touch-icon.png">',
    "manifest": '<link rel="manifest" href="/_verdict/manifest.webmanifest">',
    "theme-color": '<meta name="theme-color" content="#10213d">',
    "apple-mobile-web-app-capable": '<meta name="apple-mobile-web-app-capable" content="yes">',
    "apple-mobile-web-app-title": '<meta name="apple-mobile-web-app-title" content="Verdict">',
}


class PageMetadata(HTMLParser):
    """Locate actual tags without treating script strings or comments as markup."""

    def __init__(self, text: str):
        super().__init__()
        self.seen = set()
        self.head_end = self.head_after = self.body_start = self.body_end = None
        self.text = text
        self.doctype_end = 0
        self.lines = text.splitlines(keepends=True)
        self.feed(text)

    def position(self):
        line, column = self.getpos()
        return sum(len(part) for part in self.lines[: line - 1]) + column

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "meta":
            name = (attrs.get("name") or "").lower()
            if name in {
                "viewport",
                "theme-color",
                "apple-mobile-web-app-capable",
                "apple-mobile-web-app-title",
            }:
                self.seen.add(name)
        elif tag == "link":
            rel = (attrs.get("rel") or "").lower().split()
            self.seen.update(set(rel) & {"icon", "apple-touch-icon", "manifest"})
            if "apple-touch-icon-precomposed" in rel:
                self.seen.add("apple-touch-icon")
        elif tag == "body" and self.body_start is None:
            self.body_start = self.position()
            self.body_end = self.body_start + len(self.get_starttag_text())

    def handle_endtag(self, tag):
        if tag == "head" and self.head_end is None:
            self.head_end = self.position()
            self.head_after = self.text.find(">", self.head_end) + 1

    def handle_decl(self, decl):
        if decl.lower().startswith("doctype"):
            self.doctype_end = self.position() + len(decl) + 3


def inject(page: bytes) -> bytes:
    text = page.decode("utf-8", errors="replace")
    metadata = PageMetadata(text)
    header = (
        "".join(value for key, value in HEAD_DEFAULTS.items() if key not in metadata.seen) + INJECT
    )
    positions = [p for p in [metadata.head_end, metadata.body_start] if p is not None]
    head_at = min(positions) if positions else metadata.doctype_end
    body_at = metadata.body_end if metadata.body_end is not None else metadata.head_after or head_at
    navigation = '<nav class="vd-home"><a href="/">All reviews</a></nav>'
    return (text[:head_at] + header + text[head_at:body_at] + navigation + text[body_at:]).encode()


def decode_files(data: dict) -> dict[str, bytes]:
    return {name: base64.b64decode(content) for name, content in (data.get("files") or {}).items()}


def index_page(reviews: list[dict]) -> bytes:
    rows = "".join(
        f'<li data-review="{r["id"]}" data-status="{r["status"]}" class="{"vd-finished" if r["status"] != "open" else "vd-open"}">'
        f'<a href="/r/{r["id"]}/">#{r["number"]} {html.escape(r["title"])}</a>'
        f" <small>{r['status']} · {r['created'][:10]}</small> "
        f'<button type="button" data-action="{"reopen" if r["status"] == "closed" else "close"}" '
        f'aria-label="{"Reopen" if r["status"] == "closed" else "Close"} review #{r["number"]}">'
        f"{'Reopen' if r['status'] == 'closed' else 'Close'}</button></li>"
        for r in sorted(reviews, key=lambda r: (r["status"] != "open", r["created"], r["number"]))
    )
    return (
        "<!doctype html><meta charset=utf-8><title>Verdict</title>"
        + "".join(HEAD_DEFAULTS.values())
        + INJECT
        + '<main class=vd-index><h1>Reviews</h1><div class="vd-view-switch" role="group" aria-label="Review view">'
        + '<button type="button" data-view="list" aria-pressed="true" aria-controls="vd-list">List</button>'
        + '<button type="button" data-view="feed" aria-pressed="false" aria-controls="vd-feed">Feed</button></div>'
        + f'<ul id="vd-list">{rows or "<li>No reviews yet.</li>"}</ul>'
        + '<section id="vd-feed" aria-label="Open reviews" hidden><p class="vd-feed-empty">No open reviews.</p></section></main>'
    ).encode()


def make_server(store: Store, host: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # quiet; structlog below for API writes
            pass

        # --- plumbing ---------------------------------------------------

        def send(self, status: int, body: bytes, content_type: str = "application/json") -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def send_json(self, status: int, payload) -> None:
            self.send(status, json.dumps(payload).encode())

        def error(self, status: int, message: str) -> None:
            self.send_json(status, {"error": message})

        def body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ValueError("body too large")
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw or b"{}")
            if not isinstance(data, dict):
                raise TypeError("JSON object expected")
            return data

        def base_url(self) -> str:
            return f"http://{self.headers.get('Host') or f'{host}:{port}'}"

        # --- GET ---------------------------------------------------------

        def do_GET(self):
            url = urlsplit(self.path)
            parts = url.path.split("/")
            if url.path == "/":
                return self.send(200, index_page(store.list()), "text/html; charset=utf-8")
            if parts[1] == "_verdict" and len(parts) == 3:
                return self.asset(parts[2])
            if parts[1] == "r" and len(parts) >= 3:
                return self.page(parts[2], "/".join(parts[3:]))
            if url.path == "/api/reviews":
                return self.send_json(200, {"reviews": store.list()})
            if parts[1:3] == ["api", "reviews"] and len(parts) == 4:
                return self.state(parts[3], parse_qs(url.query))
            self.error(404, "not found")

        def asset(self, name: str) -> None:
            if name not in (
                "verdict.js",
                "verdict.css",
                "chevron-right.svg",
                "action-comment.svg",
                "action-accept.svg",
                "action-submit.svg",
                "action-reopen.svg",
                "action-close.svg",
                "favicon.svg",
                "apple-touch-icon.png",
                "icon-192.png",
                "icon-512.png",
                "manifest.webmanifest",
            ):
                return self.error(404, "not found")
            content = resources.files("verdict").joinpath("static", name).read_bytes()
            mime = {
                ".js": "text/javascript",
                ".css": "text/css",
                ".svg": "image/svg+xml",
                ".png": "image/png",
                ".webmanifest": "application/manifest+json",
            }["." + name.rsplit(".", 1)[1]]
            self.send(200, content, mime if name.endswith(".png") else f"{mime}; charset=utf-8")

        def page(self, rid: str, name: str) -> None:
            if name == "":
                if not self.path.split("?")[0].endswith("/"):
                    self.send_response(HTTPStatus.MOVED_PERMANENTLY)
                    self.send_header("Location", f"/r/{rid}/")
                    self.send_header("Content-Length", "0")
                    return self.end_headers()
                name = "index.html"
            path = store.file_path(rid, name)
            if path is None:
                return self.error(404, "not found")
            content = path.read_bytes()
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
            if name == "index.html":
                content, mime = inject(content), "text/html"
            charset = "; charset=utf-8" if mime.startswith("text/") else ""
            self.send(200, content, mime + charset)

        def state(self, rid: str, query: dict) -> None:
            wait = min(float(query.get("wait", ["0"])[0]), 30.0)
            version = int(query.get("version", ["-1"])[0])
            state = store.wait_for(rid, version, wait) if wait > 0 else store.get(rid)
            if state is None:
                return self.error(404, "no such review")
            self.send_json(200, state)

        # --- writes ------------------------------------------------------

        def do_POST(self):
            parts = urlsplit(self.path).path.split("/")
            try:
                data = self.body()
                if self.path == "/api/reviews":
                    return self.create(data)
                if self.path == "/api/gc":
                    report = store.gc(
                        int(data.get("stale_days", 7)), int(data.get("purge_days", 30))
                    )
                    log.info("gc", **{k: len(v) for k, v in report.items()})
                    return self.send_json(200, report)
                if parts[1:3] == ["api", "reviews"] and len(parts) == 5:
                    rid, action = parts[3], parts[4]
                    if action == "close":
                        log.info("closed", review=rid)
                        return self.send_json(200, store.close(rid))
                    if action == "reopen":
                        log.info("reopened", review=rid)
                        return self.send_json(200, store.reopen(rid))
                    if action == "waiters":
                        store.waiter(rid, int(data["pid"]), str(data.get("host", "")))
                        return self.send_json(200, {"ok": True})
                    if action == "touch":
                        store.touch(
                            rid,
                            created=parse(data["created"]) if data.get("created") else None,
                            updated=parse(data["updated"]) if data.get("updated") else None,
                            closed=parse(data["closed"]) if data.get("closed") else None,
                        )
                        return self.send_json(200, {"ok": True})
                self.error(404, "not found")
            except NotFound:
                self.error(404, "no such review")
            except (ValueError, KeyError, TypeError) as err:
                self.error(400, f"bad request: {err}")

        def do_PUT(self):
            parts = urlsplit(self.path).path.split("/")
            try:
                data = self.body()
                if parts[1:3] == ["api", "reviews"] and len(parts) == 5 and parts[4] == "state":
                    state = store.update(parts[3], data)
                    if data.get("status") == "done":
                        log.info("done", review=parts[3], answers=len(state["answers"]))
                    return self.send_json(200, state)
                if parts[1:3] == ["api", "reviews"] and len(parts) == 5 and parts[4] == "files":
                    rid, files = parts[3], decode_files(data)
                    reopened = store.replace(rid, files, data.get("title"))
                    log.info("updated", review=rid, files=len(files), reopened=reopened)
                    url = f"{self.base_url()}/r/{rid}/"
                    return self.send_json(200, {"id": rid, "url": url, "reopened": reopened})
                self.error(404, "not found")
            except NotFound:
                self.error(404, "no such review")
            except (ValueError, KeyError, TypeError) as err:
                self.error(400, f"bad request: {err}")

        def create(self, data: dict) -> None:
            files = decode_files(data)
            rid = store.create(str(data.get("title") or "Review"), data.get("task"), files)
            url = f"{self.base_url()}/r/{rid}/"
            log.info("published", review=rid, title=data.get("title"), files=len(files))
            self.send_json(201, {"id": rid, "url": url, "number": store.get(rid)["number"]})

    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, request, client_address):
            if not isinstance(sys.exception(), ConnectionError):  # a client hung up: routine
                super().handle_error(request, client_address)

    return Server((host, port), Handler)


def serve(store: Store, host: str, port: int) -> None:
    server = make_server(store, host, port)
    log.info("serving", bind=f"{host}:{server.server_address[1]}", state=str(store.root))
    try:
        server.serve_forever()
    finally:
        server.server_close()
