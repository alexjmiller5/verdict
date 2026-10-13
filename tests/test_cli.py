"""The agent-facing CLI against a live server."""

import io
import json
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from verdict.main import cli
from verdict.server import make_server
from verdict.store import Store


@pytest.fixture
def base(tmp_path, monkeypatch):
    srv = make_server(Store(tmp_path / "state"), "127.0.0.1", 0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    monkeypatch.setenv("VERDICT_URL", url)
    yield url
    srv.shutdown()


@pytest.fixture
def review_dir(tmp_path):
    d = tmp_path / "review"
    d.mkdir()
    (d / "index.html").write_text("<h1 data-item='a' data-choices='go|wait'>Ship?</h1>")
    (d / "shot.png").write_bytes(b"\x89PNG")
    return d


def run(capsys, *argv):
    code = cli(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def published_id(out: str) -> str:
    return out.strip().splitlines()[0].rstrip("/").rsplit("/", 1)[1]


def test_publish_prints_url_then_show_and_close(base, review_dir, capsys):
    code, out, _ = run(capsys, "publish", str(review_dir), "--title", "Ship it", "--task", "t1")
    assert code == 0
    url = out.strip().splitlines()[0]
    assert url.startswith(f"{base}/r/")
    rid = published_id(out)

    code, out, _ = run(capsys, "show", rid)
    state = json.loads(out)
    assert state["status"] == "open" and state["title"] == "Ship it" and state["task"] == "t1"

    code, out, _ = run(capsys, "list")
    assert rid in out and "open" in out

    assert run(capsys, "close", rid)[0] == 0
    assert json.loads(run(capsys, "show", rid)[1])["status"] == "closed"


def test_publish_single_file(base, review_dir, capsys):
    code, out, _ = run(capsys, "publish", str(review_dir / "index.html"))
    assert code == 0 and "/r/" in out


def test_wait_returns_answers_when_done(base, review_dir, capsys):
    _, out, _ = run(capsys, "publish", str(review_dir))
    rid = published_id(out)

    def finish():
        httpx.put(f"{base}/api/reviews/{rid}/state", json={"answers": {"a": {"choice": "go"}}})
        httpx.put(f"{base}/api/reviews/{rid}/state", json={"status": "done"})

    threading.Timer(0.3, finish).start()
    code, out, _ = run(capsys, "wait", rid, "--timeout", "10")
    assert code == 0
    assert json.loads(out)["answers"]["a"]["choice"] == "go"


def test_wait_rides_out_a_server_restart(tmp_path, monkeypatch, capsys):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    env = os.environ | {
        "VERDICT_BIND": "127.0.0.1",
        "VERDICT_PORT": str(port),
        "VERDICT_STATE_DIR": str(tmp_path / "state"),
    }

    def serve():
        proc = subprocess.Popen([sys.executable, "-m", "verdict.main", "serve"], env=env)
        for _ in range(100):
            try:
                httpx.get(f"{url}/api/reviews")
                return proc
            except httpx.TransportError:
                time.sleep(0.1)
        raise AssertionError("server did not start")

    servers = [serve()]
    rid = httpx.post(f"{url}/api/reviews", json={"title": "T", "files": {"index.html": ""}}).json()[
        "id"
    ]
    monkeypatch.setenv("VERDICT_URL", url)

    def restart_then_finish():  # a deploy kills the server mid-wait, then starts it again
        time.sleep(0.5)
        servers[0].kill()
        servers[0].wait()
        time.sleep(1)
        servers.append(serve())
        httpx.put(f"{url}/api/reviews/{rid}/state", json={"status": "done"})

    restart = threading.Thread(target=restart_then_finish)
    restart.start()
    try:
        code, out, err = run(capsys, "wait", rid, "--timeout", "20")
    finally:
        restart.join()
        for proc in servers:
            proc.kill()
            proc.wait()
    assert code == 0 and json.loads(out)["status"] == "done"
    assert "retrying" in err


def test_wait_fails_fast_when_the_server_never_answers(monkeypatch, capsys):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setenv("VERDICT_URL", f"http://127.0.0.1:{port}")
    code, _, err = run(capsys, "wait", "nope", "--timeout", "20")
    assert code == 5 and "cannot reach the server" in err


def test_wait_times_out_with_exit_code(base, review_dir, capsys):
    _, out, _ = run(capsys, "publish", str(review_dir))
    rid = published_id(out)
    code, out, err = run(capsys, "wait", rid, "--timeout", "0.3")
    assert code == 3 and "still open" in err


def test_gc_reports_and_kills_local_orphans(base, review_dir, capsys, monkeypatch):
    _, out, _ = run(capsys, "publish", str(review_dir))
    rid = published_id(out)
    run(capsys, "close", rid)
    killed = []
    monkeypatch.setattr("verdict.main.local_wait_pids", lambda: {777: rid, 778: "other"})
    monkeypatch.setattr("verdict.main.kill", lambda pid: killed.append(pid))
    code, out, _ = run(capsys, "gc")
    assert code == 0
    assert killed == [777]
    assert "777" in out


def test_publish_warns_about_stale_reviews(base, review_dir, capsys):
    _, out, _ = run(capsys, "publish", str(review_dir))
    rid = published_id(out)
    old = (datetime.now(UTC) - timedelta(days=9)).isoformat()
    httpx.post(f"{base}/api/reviews/{rid}/touch", json={"created": old, "updated": old})
    _, _, err = run(capsys, "publish", str(review_dir))
    assert "stale" in err and rid in err


def test_publish_runs_the_publish_hook_with_the_review(
    base, review_dir, capsys, monkeypatch, tmp_path
):
    seen = tmp_path / "hook.txt"
    monkeypatch.setenv(
        "VERDICT_PUBLISH_HOOK",
        f'echo "$VERDICT_REVIEW_ID $VERDICT_REVIEW_URL" > {seen}; echo armed a waker',
    )
    code, out, err = run(capsys, "publish", str(review_dir))
    assert code == 0
    url = out.strip()
    assert url.startswith(base) and "\n" not in url  # stdout stays the URL alone
    rid = url.rstrip("/").rsplit("/", 1)[1]
    assert seen.read_text().split() == [rid, url]
    assert "armed a waker" in err


def test_a_failing_publish_hook_never_fails_the_publish(base, review_dir, capsys, monkeypatch):
    monkeypatch.setenv("VERDICT_PUBLISH_HOOK", "echo nope; exit 7")
    code, out, err = run(capsys, "publish", str(review_dir))
    assert code == 0 and out.strip().startswith(base)
    assert "publish hook failed (exit 7)" in err and "nope" in err


NOTES_MD = """# Five decisions

Where these came from.

## 1. Price source

| provider | history |
|---|---|
| Tiingo | full |

<div data-item="prices" data-choices="Tiingo|Alpha Vantage" data-default="Tiingo">Price source</div>

## 2. Context only

Nothing to decide here.
"""


def page(base, rid, name=""):
    return httpx.get(f"{base}/r/{rid}/{name}").text


def test_publish_markdown_renders_one_card_per_heading(base, tmp_path, capsys):
    notes = tmp_path / "notes.md"
    notes.write_text(NOTES_MD)
    code, out, _ = run(capsys, "publish", str(notes))
    assert code == 0
    rid = published_id(out)
    html = page(base, rid)
    assert "<title>Five decisions</title>" in html
    assert json.loads(run(capsys, "show", rid)[1])["title"] == "Five decisions"
    lead, first, second = html.split('<section class="card"')
    assert "<h1>Five decisions</h1>" in lead and "Where these came from." in lead
    assert "<h2>1. Price source</h2>" in first and "<table>" in first
    assert '<div data-item="prices" data-choices="Tiingo|Alpha Vantage"' in first
    assert "<h2>2. Context only</h2>" in second and "Price source" not in second


def test_publish_markdown_from_stdin(base, capsys, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(NOTES_MD))
    code, out, _ = run(capsys, "publish", "-", "--title", "From stdin")
    assert code == 0
    rid = published_id(out)
    assert "<h2>1. Price source</h2>" in page(base, rid)
    assert json.loads(run(capsys, "show", rid)[1])["title"] == "From stdin"


def test_publish_directory_with_index_md_keeps_its_images(base, tmp_path, capsys):
    d = tmp_path / "review"
    d.mkdir()
    (d / "index.md").write_text("# Shots\n\n![the screen](shot.png)\n")
    (d / "shot.png").write_bytes(b"\x89PNG")
    _, out, _ = run(capsys, "publish", str(d))
    rid = published_id(out)
    assert '<img alt="the screen" src="shot.png"' in page(base, rid)
    assert httpx.get(f"{base}/r/{rid}/shot.png").content == b"\x89PNG"


def test_update_replaces_the_page_keeps_answers_and_reopens(base, review_dir, tmp_path, capsys):
    _, out, _ = run(capsys, "publish", str(review_dir))
    url, rid = out.strip(), published_id(out)
    httpx.put(f"{base}/api/reviews/{rid}/state", json={"answers": {"a": {"choice": "go"}}})
    httpx.put(f"{base}/api/reviews/{rid}/state", json={"status": "done"})
    notes = tmp_path / "round2.md"
    notes.write_text("# Round two\n\nApplied: go.\n")
    code, out, _ = run(capsys, "update", rid, str(notes))
    assert code == 0 and out.strip() == url
    state = json.loads(run(capsys, "show", rid)[1])
    assert state["status"] == "open" and state["answers"]["a"]["choice"] == "go"
    assert state["revision"] == 2 and state["title"] == "Round two"
    assert "Applied: go." in page(base, rid)
    assert httpx.get(f"{base}/r/{rid}/shot.png").status_code == 404  # replaced, not merged


def test_update_an_unknown_review_exits_4(base, review_dir, capsys):
    code, _, err = run(capsys, "update", "nope", str(review_dir))
    assert code == 4 and "nope" in err


def test_update_reruns_the_publish_hook_only_when_it_reopens(base, review_dir, capsys, monkeypatch):
    _, out, _ = run(capsys, "publish", str(review_dir))
    rid = published_id(out)
    monkeypatch.setenv("VERDICT_PUBLISH_HOOK", 'echo "armed $VERDICT_REVIEW_ID"')
    _, _, err = run(capsys, "update", rid, str(review_dir))
    assert "armed" not in err  # still open: the waker armed at publish is still waiting
    httpx.put(f"{base}/api/reviews/{rid}/state", json={"status": "done"})
    _, _, err = run(capsys, "update", rid, str(review_dir))
    assert f"armed {rid}" in err


def test_a_refused_publish_says_why_instead_of_blaming_the_network(base, review_dir, capsys):
    (review_dir / "my shot.png").write_bytes(b"\x89PNG")
    code, _, err = run(capsys, "publish", str(review_dir))
    assert code == 1 and "bad file name: my shot.png" in err


def test_the_callers_publish_hook_never_runs_in_tests(base, review_dir, capsys, tmp_path):
    # conftest strips VERDICT_PUBLISH_HOOK; a hook left in the environment would run here
    assert "VERDICT_PUBLISH_HOOK" not in os.environ
    code, _, err = run(capsys, "publish", str(review_dir))
    assert code == 0 and "hook" not in err
