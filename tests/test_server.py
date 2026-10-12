"""Behavior tests for the Verdict server: publish, serve, answer, wait, close, gc."""

import base64
import glob
import html
import json
import os
import re
import shutil
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from verdict.server import make_server
from verdict.store import Store

PAGE = (
    b"<html><body><h1 data-item='a' data-choices='yes|no'>Q</h1><img src='shot.png'></body></html>"
)


@pytest.fixture
def server(tmp_path):
    store = Store(tmp_path)
    srv = make_server(store, "127.0.0.1", 0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    yield base, store
    srv.shutdown()


def publish(base, title="T", task="task-1", files=None):
    files = files or {"index.html": PAGE, "shot.png": b"\x89PNG"}
    body = {
        "title": title,
        "task": task,
        "files": {k: base64.b64encode(v).decode() for k, v in files.items()},
    }
    r = httpx.post(f"{base}/api/reviews", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def test_publish_serves_page_with_injected_client_and_static_files(server):
    base, _ = server
    review = publish(base)
    assert review["url"] == f"{base}/r/{review['id']}/"
    page = httpx.get(review["url"])
    assert page.status_code == 200
    assert b"/_verdict/verdict.js" in page.content
    assert b"data-item='a'" in page.content
    assert httpx.get(f"{base}/r/{review['id']}/shot.png").content == b"\x89PNG"
    assert httpx.get(f"{base}/_verdict/verdict.js").status_code == 200
    assert httpx.get(f"{base}/r/nope/").status_code == 404


def test_publish_requires_index_html(server):
    base, _ = server
    r = httpx.post(f"{base}/api/reviews", json={"title": "x", "files": {}})
    assert r.status_code == 400


def test_answers_round_trip_and_done(server):
    base, _ = server
    rid = publish(base)["id"]
    state = httpx.get(f"{base}/api/reviews/{rid}").json()
    assert state["status"] == "open" and state["answers"] == {} and state["title"] == "T"
    patch = {
        "answers": {"a": {"choice": "no", "text": "because"}},
        "comments": [{"item": "a", "snippet": "Q", "text": "move this up"}],
        "general": "looks fine",
    }
    r = httpx.put(f"{base}/api/reviews/{rid}/state", json=patch)
    assert r.status_code == 200
    r = httpx.put(f"{base}/api/reviews/{rid}/state", json={"status": "done"})
    state = r.json()
    assert state["status"] == "done"
    assert state["answers"]["a"]["choice"] == "no"
    assert state["comments"][0]["text"] == "move this up"
    assert state["general"] == "looks fine"
    assert state["done_at"]


def test_wait_long_polls_until_change(server):
    base, _ = server
    rid = publish(base)["id"]
    state = httpx.get(f"{base}/api/reviews/{rid}").json()
    version = state["version"]

    def finish():
        time.sleep(0.2)
        httpx.put(f"{base}/api/reviews/{rid}/state", json={"status": "done"})

    threading.Thread(target=finish).start()
    started = time.monotonic()
    r = httpx.get(f"{base}/api/reviews/{rid}", params={"wait": 5, "version": version}, timeout=10)
    assert r.json()["status"] == "done"
    assert time.monotonic() - started < 3
    # an unchanged version times out and returns the current state
    r = httpx.get(f"{base}/api/reviews/{rid}", params={"wait": 0.2, "version": r.json()["version"]})
    assert r.json()["status"] == "done"


def test_close_list_and_gc(server):
    base, store = server
    old = publish(base, title="old")["id"]
    done = publish(base, title="done")["id"]
    fresh = publish(base, title="fresh")["id"]
    httpx.post(f"{base}/api/reviews/{done}/close").raise_for_status()
    httpx.put(f"{base}/api/reviews/{fresh}/state", json={"status": "done"})
    now = datetime.now(UTC)
    store.touch(old, created=now - timedelta(days=10), updated=now - timedelta(days=10))
    store.touch(done, closed=now - timedelta(days=40))

    listing = httpx.get(f"{base}/api/reviews").json()["reviews"]
    by_id = {r["id"]: r for r in listing}
    assert by_id[old]["status"] == "open" and by_id[done]["status"] == "closed"

    report = httpx.post(f"{base}/api/gc", json={"stale_days": 7, "purge_days": 30}).json()
    assert old in report["stale_open"]
    assert done in report["purged"]
    assert fresh not in report["stale_open"]
    assert httpx.get(f"{base}/r/{done}/").status_code == 404
    assert httpx.get(f"{base}/r/{old}/").status_code == 200


def test_waiters_register_and_expire(server):
    base, store = server
    rid = publish(base)["id"]
    r = httpx.post(f"{base}/api/reviews/{rid}/waiters", json={"pid": 4242, "host": "mini"})
    assert r.status_code == 200
    waiters = httpx.get(f"{base}/api/reviews/{rid}").json()["waiters"]
    assert waiters == [{"pid": 4242, "host": "mini", "seen": waiters[0]["seen"]}]
    store.touch_waiter(rid, 4242, seen=datetime.now(UTC) - timedelta(minutes=10))
    report = httpx.post(f"{base}/api/gc", json={}).json()
    assert report["dead_waiters"] == [{"review": rid, "pid": 4242, "host": "mini"}]
    assert httpx.get(f"{base}/api/reviews/{rid}").json()["waiters"] == []


def test_state_survives_restart(tmp_path):
    store = Store(tmp_path)
    rid = store.create("T", "task", {"index.html": PAGE})
    store.update(rid, {"answers": {"a": {"choice": "yes"}}})
    again = Store(tmp_path)
    assert again.get(rid)["answers"]["a"]["choice"] == "yes"
    assert json.loads((tmp_path / "reviews" / rid / "state.json").read_text())["title"] == "T"


CHROME = next(
    filter(
        None,
        [
            os.environ.get("CHROME"),
            shutil.which("chromium"),
            shutil.which("google-chrome"),
            *glob.glob("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
        ],
    ),
    None,
)
# Types into each text box, pauses past the autosave debounce, then records what
# has focus. The long poll is shortened so headless virtual time can advance.
FOCUS_PAGE = b"""<html><body>
<p data-item="q" data-input="why?">Q</p><p data-item="c" data-choices="yes|no">C</p>
<script>
const realFetch = window.fetch;
window.fetch = (url, opts) => realFetch(String(url).replace(/wait=\\d+/, "wait=0"), opts);
const pause = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  const result = {};
  for (const sel of [".vd-slot textarea", ".vd-general"]) {
    let box;
    while (!(box = document.querySelector(sel))) await pause(50);
    box.focus();
    box.value = "half a sent";
    box.dispatchEvent(new Event("input"));
    box.setSelectionRange(4, 6);
    await pause(1500);
    result[sel] = [document.activeElement === box, box.selectionStart, box.selectionEnd, box.value];
  }
  document.body.dataset.result = JSON.stringify(result);
})();
</script></body></html>"""


@pytest.mark.skipif(not CHROME, reason="needs Chrome or Chromium")
def test_autosave_keeps_focus_and_caret_in_the_text_box(server, tmp_path):
    base, _ = server
    review = publish(base, files={"index.html": FOCUS_PAGE})
    args = ["--headless", "--no-first-run", f"--user-data-dir={tmp_path}/chrome"]
    args += ["--virtual-time-budget=8000", "--dump-dom", review["url"]]
    proc = subprocess.Popen(
        [CHROME, *args], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
    )
    killer = threading.Timer(60, proc.kill)  # Chrome prints the DOM but may never exit
    killer.start()
    dom = ""
    try:
        for line in proc.stdout:
            dom += line
            if "</html>" in line:
                break
    finally:
        killer.cancel()
        proc.kill()
        proc.wait()
    result = re.search(r'data-result="([^"]*)"', dom)
    assert result, dom[-2000:]
    for sel, (focused, start, end, value) in json.loads(html.unescape(result.group(1))).items():
        assert (focused, start, end, value) == (True, 4, 6, "half a sent"), sel
    state = httpx.get(f"{base}/api/reviews/{review['id']}").json()
    assert state["answers"]["q"]["text"] == state["general"] == "half a sent"


# Ctrl+Option+C toggles comment mode (the owner's Hyper+C sends it); other
# combos with C must not.
KEY_PAGE = b"""<html><body><p data-item="c" data-choices="yes|no">C</p>
<script>
const realFetch = window.fetch;
window.fetch = (url, opts) => realFetch(String(url).replace(/wait=\\d+/, "wait=0"), opts);
const pause = (ms) => new Promise((r) => setTimeout(r, ms));
const press = (mods) => document.dispatchEvent(new KeyboardEvent("keydown",
  { code: "KeyC", key: "c", bubbles: true, cancelable: true, ...mods }));
const on = () => document.body.classList.contains("vd-comment-mode");
(async () => {
  while (!document.querySelector(".vd-bar")) await pause(50);
  const seen = [];
  press({ ctrlKey: true, altKey: true }); seen.push(on());
  press({ ctrlKey: true, altKey: true }); seen.push(on());
  press({}); seen.push(on());
  press({ metaKey: true, ctrlKey: true, altKey: true }); seen.push(on());
  document.body.dataset.result = JSON.stringify(seen);
})();
</script></body></html>"""


@pytest.mark.skipif(not CHROME, reason="needs Chrome or Chromium")
def test_ctrl_option_c_toggles_comment_mode(server, tmp_path):
    base, _ = server
    review = publish(base, files={"index.html": KEY_PAGE})
    args = ["--headless", "--no-first-run", f"--user-data-dir={tmp_path}/chrome"]
    args += ["--virtual-time-budget=5000", "--dump-dom", review["url"]]
    proc = subprocess.Popen(
        [CHROME, *args], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
    )
    killer = threading.Timer(60, proc.kill)
    killer.start()
    dom = ""
    try:
        for line in proc.stdout:
            dom += line
            if "</html>" in line:
                break
    finally:
        killer.cancel()
        proc.kill()
        proc.wait()
    result = re.search(r'data-result="([^"]*)"', dom)
    assert result, dom[-2000:]
    assert json.loads(html.unescape(result.group(1))) == [True, False, False, False]


# Done asks first, says whether an agent is listening, and only Send finishes.
DONE_PAGE = b"""<html><body><p data-item="c" data-choices="yes|no">C</p>
<script>
const realFetch = window.fetch;
window.fetch = (url, opts) => realFetch(String(url).replace(/wait=\\d+/, "wait=0"), opts);
const pause = (ms) => new Promise((r) => setTimeout(r, ms));
const status = async () => (await (await realFetch(location.pathname.replace("/r/", "/api/reviews/").replace(/\\/$/, ""))).json()).status;
const button = (text) => [...document.querySelectorAll("button")].find((b) => b.textContent.trim() === text);
(async () => {
  while (!button("Done")) await pause(50);
  button("Done").click();
  await pause(300);
  const sheet = document.querySelector(".vd-confirm");
  const result = { sheet: sheet ? sheet.innerText : null, before: await status() };
  button("Keep editing").click();
  result.keptOpen = !document.querySelector(".vd-confirm") && (await status()) === "open";
  button("Done").click();
  await pause(300);
  button("Send").click();
  await pause(1500);
  result.after = await status();
  result.bar = document.querySelector(".vd-status").textContent;
  document.body.dataset.result = JSON.stringify(result);
})();
</script></body></html>"""


@pytest.mark.skipif(not CHROME, reason="needs Chrome or Chromium")
@pytest.mark.parametrize("listening", [True, False])
def test_done_confirms_and_says_whether_the_agent_is_listening(server, tmp_path, listening):
    base, _ = server
    review = publish(base, files={"index.html": DONE_PAGE})
    if listening:
        httpx.post(f"{base}/api/reviews/{review['id']}/waiters", json={"pid": 1, "host": "h"})
    args = ["--headless", "--no-first-run", f"--user-data-dir={tmp_path}/chrome"]
    args += ["--virtual-time-budget=8000", "--dump-dom", review["url"]]
    proc = subprocess.Popen(
        [CHROME, *args], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
    )
    killer = threading.Timer(60, proc.kill)
    killer.start()
    dom = ""
    try:
        for line in proc.stdout:
            dom += line
            if "</html>" in line:
                break
    finally:
        killer.cancel()
        proc.kill()
        proc.wait()
    found = re.search(r'data-result="([^"]*)"', dom)
    assert found, dom[-2000:]
    result = json.loads(html.unescape(found.group(1)))
    assert "Send your answers to the agent?" in result["sheet"]
    assert ("listening and gets them right away" in result["sheet"]) is listening
    assert result["before"] == "open" and result["keptOpen"]
    assert result["after"] == "done"
    assert result["bar"] == "sent to the agent"
