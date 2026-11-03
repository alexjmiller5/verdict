"""Behavior tests for the Verdict server: publish, serve, answer, wait, close, gc."""

import base64
import glob
import html
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

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


def test_front_page_numbers_every_review_open_oldest_first_and_links_back(server):
    base, store = server
    first = publish(base, title="First <review>")
    second = publish(base, title="Second")
    publish(base, title="Third")
    store.update(first["id"], {"status": "done"})
    page = httpx.get(base + "/").text
    assert page.index("Second") < page.index("Third") < page.index("First &lt;review&gt;")
    assert "#1 First &lt;review&gt;" in page
    assert "#2 Second" in page
    assert 'class="vd-finished"' in page
    store.close(first["id"])
    assert "First &lt;review&gt;" in httpx.get(base + "/").text
    assert '<a href="/"' in httpx.get(second["url"]).text
    assert first["number"] == 1


def test_review_numbers_survive_restart_reopen_and_removal(tmp_path):
    store = Store(tmp_path)
    first = store.create("First", None, {"index.html": PAGE})
    second = store.create("Second", None, {"index.html": PAGE})
    assert store.get(first)["number"] == 1
    assert store.get(second)["number"] == 2
    store.delete(second)
    store = Store(tmp_path)
    third = store.create("Third", None, {"index.html": PAGE})
    assert store.get(third)["number"] == 3
    store.close(first)
    store.replace(first, {"index.html": PAGE})
    assert store.get(first)["number"] == 1


def test_existing_reviews_receive_durable_chronological_numbers(tmp_path):
    store = Store(tmp_path)
    first = store.create("First", None, {"index.html": PAGE})
    second = store.create("Second", None, {"index.html": PAGE})
    store.touch(first, created=datetime(2026, 1, 1, tzinfo=UTC))
    store.touch(second, created=datetime(2026, 1, 2, tzinfo=UTC))
    for rid in (first, second):
        path = tmp_path / "reviews" / rid / "state.json"
        state = json.loads(path.read_text())
        state.pop("number", None)
        path.write_text(json.dumps(state))
    (tmp_path / "last-number").unlink(missing_ok=True)
    store = Store(tmp_path)
    assert store.get(first)["number"] == 1
    assert store.get(second)["number"] == 2
    assert Store(tmp_path).get(second)["number"] == 2


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


def browser_result(url, tmp_path, budget):
    """Load url in headless Chrome; return the JSON the page left in body[data-result]."""
    args = ["--headless", "--no-first-run", f"--user-data-dir={tmp_path}/chrome"]
    # An earlier document gives the tested tab ordinary browser history, so a
    # review cannot close the DOM-dump harness after successfully submitting.
    initial = "data:text/html," + quote(f"<script>location.href={json.dumps(url)}</script>")
    args += [f"--virtual-time-budget={budget}", "--dump-dom", initial]
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
    found = re.search(r'data-result="([^"]*)"', dom)
    assert found, dom[-2000:]
    return json.loads(html.unescape(found.group(1)))


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
    result = browser_result(review["url"], tmp_path, 8000)
    for sel, (focused, start, end, value) in result.items():
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
    result = browser_result(review["url"], tmp_path, 5000)
    assert result == [True, False, False, False]


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
    result = browser_result(review["url"], tmp_path, 8000)
    assert "Send your answers to the agent?" in result["sheet"]
    assert ("listening and gets them right away" in result["sheet"]) is listening
    assert result["before"] == "open" and result["keptOpen"]
    assert result["after"] == "done"
    assert result["bar"] == "sent to the agent"


# A hidden tab must not park a long poll: browsers open six connections per
# host, so a few background review tabs would otherwise stall every save and
# page load until one of their polls times out.
HIDDEN_PAGE = b"""<html><body><p data-item="c" data-choices="yes|no">C</p>
<script>
const realFetch = window.fetch;
const polls = [];
let firstPoll;
const polled = new Promise((r) => { firstPoll = r; });
window.fetch = (url, opts = {}) => {
  if (String(url).includes("wait=")) {
    const poll = { aborted: false };
    polls.push(poll);
    opts.signal?.addEventListener("abort", () => { poll.aborted = true; });
    firstPoll();
  }
  return realFetch(String(url).replace(/wait=\\d+/, "wait=2"), opts);
};
let hidden = false;
Object.defineProperty(document, "hidden", { get: () => hidden });
Object.defineProperty(document, "visibilityState", { get: () => (hidden ? "hidden" : "visible") });
const show = (visible) => { hidden = !visible; document.dispatchEvent(new Event("visibilitychange")); };
const pause = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  await polled;  // a microtask: timers stall while a fetch is pending in virtual time
  show(false);
  const result = { aborted: polls[0].aborted, before: polls.length };
  await pause(5000);
  result.whileHidden = polls.length - result.before;
  show(true);
  await pause(200);
  result.resumed = polls.length - result.before;
  show(false);
  document.body.dataset.result = JSON.stringify(result);
})();
</script></body></html>"""


@pytest.mark.skipif(not CHROME, reason="needs Chrome or Chromium")
def test_hidden_tab_drops_its_long_poll_and_resumes_when_shown(server, tmp_path):
    base, _ = server
    review = publish(base, files={"index.html": HIDDEN_PAGE})
    result = browser_result(review["url"], tmp_path, 10000)
    assert result == {"aborted": True, "before": 1, "whileHidden": 0, "resumed": 1}


def test_a_client_hanging_up_is_not_logged_as_an_error(server, capsys):
    base, _ = server
    host, port = base.removeprefix("http://").split(":")
    sock = socket.create_connection((host, int(port)))
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
    sock.close()  # linger 0: the server reads a connection reset
    httpx.get(f"{base}/api/reviews")  # the server is still answering
    time.sleep(0.2)
    assert "Traceback" not in capsys.readouterr().err


def b64(content: bytes) -> str:
    return base64.b64encode(content).decode()


def test_replacing_files_keeps_answers_reopens_and_bumps_the_revision(server):
    base, _ = server
    review = publish(base)
    rid = review["id"]
    httpx.put(f"{base}/api/reviews/{rid}/state", json={"answers": {"a": {"choice": "yes"}}})
    httpx.put(f"{base}/api/reviews/{rid}/state", json={"status": "done"})
    v2 = {"index.html": b64(b"<p>round two</p>")}
    r = httpx.put(f"{base}/api/reviews/{rid}/files", json={"title": "T2", "files": v2})
    assert r.status_code == 200
    assert r.json() == {"id": rid, "url": review["url"], "reopened": True}
    state = httpx.get(f"{base}/api/reviews/{rid}").json()
    assert (state["status"], state["done_at"], state["revision"]) == ("open", None, 2)
    assert state["title"] == "T2" and state["answers"]["a"]["choice"] == "yes"
    assert b"round two" in httpx.get(review["url"]).content
    assert httpx.get(f"{base}/r/{rid}/shot.png").status_code == 404
    # an open review stays open, and a missing title keeps the old one
    r = httpx.put(f"{base}/api/reviews/{rid}/files", json={"files": v2})
    assert r.json()["reopened"] is False
    assert httpx.get(f"{base}/api/reviews/{rid}").json()["title"] == "T2"


def test_replacing_files_rejects_bad_input_and_changes_nothing(server):
    base, _ = server
    rid = publish(base)["id"]
    url = f"{base}/api/reviews/{rid}/files"
    bad_name = {"index.html": b64(b"x"), "../escape": b64(b"x")}
    assert httpx.put(url, json={"files": bad_name}).status_code == 400
    assert httpx.put(url, json={"files": {"shot.png": b64(b"x")}}).status_code == 400
    assert httpx.put(f"{base}/api/reviews/nope/files", json={"files": {}}).status_code == 404
    assert httpx.get(f"{base}/api/reviews/{rid}").json()["revision"] == 1
    assert httpx.get(f"{base}/r/{rid}/shot.png").content == b"\x89PNG"


# Done sends only what the reviewer touched: an untouched choice stays
# unanswered, an accept button records the recommendation as defaulted, and
# only sections with choices get an accept button.
UNTOUCHED_PAGE = b"""<html><body>
<section data-section="s1"><p data-item="a" data-choices="yes|no">A</p>
<p data-item="b" data-choices="yes|no">B</p></section>
<section data-section="s2"><p data-item="c" data-choices="keep|drop" data-default="drop">C</p></section>
<section data-section="s3"><p>Context only</p></section>
<script>
const realFetch = window.fetch;
window.fetch = (url, opts) => realFetch(String(url).replace(/wait=\\d+/, "wait=0"), opts);
const pause = (ms) => new Promise((r) => setTimeout(r, ms));
const state = async () => (await realFetch(location.pathname.replace("/r/", "/api/reviews/").replace(/\\/$/, ""))).json();
const button = (text, scope = document) => [...scope.querySelectorAll("button")].find((b) => b.textContent.trim() === text);
(async () => {
  while (!button("Done")) await pause(50);
  const result = { sectionButtons: document.querySelectorAll(".vd-section-accept").length };
  button("no", document.querySelector('[data-for="a"]')).click();
  document.querySelector('[data-section="s2"] .vd-section-accept').click();
  await pause(1000);
  button("Done").click();
  await pause(300);
  button("Send").click();
  await pause(1500);
  const s = await state();
  result.status = s.status;
  result.answers = s.answers;
  document.body.dataset.result = JSON.stringify(result);
})();
</script></body></html>"""


@pytest.mark.skipif(not CHROME, reason="needs Chrome or Chromium")
def test_done_leaves_untouched_items_unanswered(server, tmp_path):
    base, _ = server
    review = publish(base, files={"index.html": UNTOUCHED_PAGE})
    result = browser_result(review["url"], tmp_path, 8000)
    assert result["sectionButtons"] == 2
    assert result["status"] == "done"
    assert result["answers"] == {
        "a": {"choice": "no", "defaulted": False},
        "c": {"choice": "drop", "defaulted": True},
    }


# A page with nothing to answer is read, not answered: one "Got it" button.
INFO_PAGE = b"""<html><body><section data-section="s"><h2>Report</h2><p>All done.</p></section>
<script>
const realFetch = window.fetch;
window.fetch = (url, opts) => realFetch(String(url).replace(/wait=\\d+/, "wait=0"), opts);
const pause = (ms) => new Promise((r) => setTimeout(r, ms));
const buttons = () => [...document.querySelectorAll(".vd-bar button")].map((b) => b.textContent.trim());
(async () => {
  while (!document.querySelector(".vd-bar")) await pause(50);
  const result = { buttons: buttons(), status: document.querySelector(".vd-status").textContent,
                   sectionButtons: document.querySelectorAll(".vd-section-accept").length };
  [...document.querySelectorAll(".vd-bar button")].find((b) => b.textContent.trim() === "Got it").click();
  await pause(1500);
  result.after = (await (await realFetch(location.pathname.replace("/r/", "/api/reviews/").replace(/\\/$/, ""))).json()).status;
  document.body.dataset.result = JSON.stringify(result);
})();
</script></body></html>"""


@pytest.mark.skipif(not CHROME, reason="needs Chrome or Chromium")
def test_a_page_with_nothing_to_answer_offers_got_it(server, tmp_path):
    base, _ = server
    review = publish(base, files={"index.html": INFO_PAGE})
    result = browser_result(review["url"], tmp_path, 8000)
    assert "Got it" in result["buttons"] and "Done" not in result["buttons"]
    assert "accept all recommendations" not in result["buttons"]
    assert result["status"].startswith("nothing to answer")
    assert result["sectionButtons"] == 0
    assert result["after"] == "done"


# `verdict update` bumps the revision; an open tab reloads into the new page.
# The first page replaces itself once its client is up, so no timer races
# Chrome's virtual clock.
RELOAD_V2 = b"""<html><body><p>round two</p>
<script>
const realFetch = window.fetch;
window.fetch = (url, opts) => realFetch(String(url).replace(/wait=\\d+/, "wait=0"), opts);
document.body.dataset.result = JSON.stringify({ page: "v2" });
</script></body></html>"""
RELOAD_V1 = """<html><body><p data-item="c" data-choices="yes|no">C</p>
<script>
const realFetch = window.fetch;
window.fetch = (url, opts) => realFetch(String(url).replace(/wait=\\d+/, "wait=0"), opts);
const pause = (ms) => new Promise((r) => setTimeout(r, ms));
(async () => {
  while (!document.querySelector(".vd-bar")) await pause(50);
  const api = location.pathname.replace("/r/", "/api/reviews/").replace(/\\/$/, "");
  await realFetch(`${api}/files`, { method: "PUT", body: JSON.stringify({ files: { "index.html": "V2" } }) });
})();
</script></body></html>"""


@pytest.mark.skipif(not CHROME, reason="needs Chrome or Chromium")
def test_an_open_tab_reloads_when_the_page_is_replaced(server, tmp_path):
    base, _ = server
    v1 = RELOAD_V1.replace("V2", b64(RELOAD_V2)).encode()
    review = publish(base, files={"index.html": v1})
    result = browser_result(review["url"], tmp_path, 8000)
    assert result == {"page": "v2"}


COMMENT_ANSWER_PAGE = b"""<html><body>
<p data-item="q" data-choices="yes|no"><span>Question</span></p>
<p data-item="untouched" data-input="answer">Unanswered</p>
<script>
const realFetch = window.fetch;
window.fetch = (url, opts) => realFetch(String(url).replace(/wait=\\d+/, "wait=0"), opts);
const pause = ms => new Promise(r => setTimeout(r, ms));
const button = text => [...document.querySelectorAll('button')].find(b => b.textContent.trim() === text);
(async () => {
 while (!button('Done')) await pause(50);
 button('comment mode').click();
 document.querySelector('[data-item="q"] span').click();
 document.querySelector('.vd-sheet textarea').value = 'Use another approach';
 button('Add note').click();
 await pause(900);
 document.querySelector('.vd-toggle').click();
 const progress = document.querySelector('.vd-status').textContent;
 button('Done').click();
 document.body.dataset.result = JSON.stringify({progress, sheet:document.querySelector('.vd-confirm').innerText});
})();
</script></body></html>"""


@pytest.mark.skipif(not CHROME, reason="needs Chrome or Chromium")
def test_element_comment_counts_as_answer_only_for_its_question(server, tmp_path):
    base, _ = server
    review = publish(base, files={"index.html": COMMENT_ANSWER_PAGE})
    result = browser_result(review["url"], tmp_path, 5000)
    assert "1/2 answered" in result["progress"]
    assert "1 left unanswered" in result["sheet"]
    state = httpx.get(f"{base}/api/reviews/{review['id']}").json()
    assert state["answers"] == {}
    assert state["comments"][0]["item"] == "q"


@pytest.mark.skipif(not CHROME, reason="needs Chrome or Chromium")
@pytest.mark.parametrize(
    "action, expected",
    [
        (
            "document.querySelector('.vd-note').click(); document.querySelector('.vd-sheet textarea').value='Alternative'; button('Add note').click();",
            1,
        ),
        (
            "const box=document.querySelector('.vd-slot textarea'); box.value='Typed answer'; box.dispatchEvent(new Event('input'));",
            1,
        ),
        (
            "const box=document.querySelector('.vd-slot textarea'); box.value='   '; box.dispatchEvent(new Event('input'));",
            2,
        ),
    ],
)
def test_done_counts_notes_and_nonblank_text(server, tmp_path, action, expected):
    base, _ = server
    page = COMMENT_ANSWER_PAGE.replace(
        b" button('comment mode').click();\n document.querySelector('[data-item=\"q\"] span').click();\n document.querySelector('.vd-sheet textarea').value = 'Use another approach';\n button('Add note').click();",
        action.encode(),
    )
    review = publish(base, files={"index.html": page})
    result = browser_result(review["url"], tmp_path, 5000)
    assert f"{expected} left unanswered" in result["sheet"]


@pytest.mark.skipif(not CHROME, reason="needs Chrome or Chromium")
@pytest.mark.parametrize("selector", [".vd-slot textarea", ".vd-general", ".vd-sheet textarea"])
def test_cmd_enter_submits_text_with_explicit_done_confirmation(server, tmp_path, selector):
    base, _ = server
    action = """
    const selector = SELECTOR;
    if (selector === '.vd-sheet textarea') document.querySelector('.vd-note').click();
    const box = document.querySelector(selector);
    box.focus(); box.value = 'Keyboard answer'; box.dispatchEvent(new Event('input'));
    box.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', bubbles:true, cancelable:true}));
    const plainEnter = !document.querySelector('.vd-confirm');
    box.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', metaKey:true, isComposing:true, bubbles:true, cancelable:true}));
    const composing = !document.querySelector('.vd-confirm');
    box.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', metaKey:true, bubbles:true, cancelable:true}));
    await pause(900);
    document.body.dataset.result = JSON.stringify({
      plainEnter, composing, confirmation:!!document.querySelector('.vd-confirm'),
      noteDialog:!!document.querySelector('.vd-sheet:not(.vd-confirm)')
    });
    """.replace("SELECTOR", json.dumps(selector))
    page = (
        COMMENT_ANSWER_PAGE.split(b" button('comment mode').click();")[0]
        + action.encode()
        + b"})();</script></body></html>"
    )
    review = publish(base, files={"index.html": page})
    result = browser_result(review["url"], tmp_path, 5000)
    assert result["plainEnter"] and result["composing"]
    assert result["confirmation"] is (selector != ".vd-sheet textarea")
    assert not result["noteDialog"]
    state = httpx.get(f"{base}/api/reviews/{review['id']}").json()
    assert state["status"] == "open"
    if selector == ".vd-sheet textarea":
        assert state["comments"][0]["text"] == "Keyboard answer"
    elif selector == ".vd-general":
        assert state["general"] == "Keyboard answer"
    else:
        assert state["answers"]["untouched"]["text"] == "Keyboard answer"


SUBMIT_SAVE_PAGE = rb"""<html><body>
<p data-item="q" data-input="Answer">Question</p>
<script>
const mode = 'MODE';
const realFetch = window.fetch;
const pause = ms => new Promise(r => setTimeout(r, ms));
const button = text => [...document.querySelectorAll('button')].find(b => b.textContent.trim() === text);
let release, started, atDone, active = 0, activeAtDone;
const saveStarted = new Promise(r => started = r);
let fail = mode === 'rejected', hold = mode.startsWith('delayed');
window.fetch = (url, opts) => {
 const patch = opts?.method === 'PUT' ? JSON.parse(opts.body) : {};
 if (patch.answers || patch.comments || patch.general) {
   active++;
   const send = () => realFetch(url, opts).then(r => { active--; return r; });
   if (hold) {
     hold = false;
     return new Promise(resolve => { release = () => resolve(send()); started(); });
   }
   if (fail) { active--; return Promise.resolve(new Response('', {status:503})); }
   return send();
 }
 if (patch.status === 'done') { activeAtDone = active; return read().then(s => { atDone = s; return realFetch(url, opts); }); }
 return realFetch(String(url).replace(/wait=\d+/, 'wait=0'), opts);
};
const read = async () => (await realFetch(location.pathname.replace('/r/', '/api/reviews/').replace(/\/$/, ''))).json();
(async () => {
 while (!button('Done')) await pause(50);
 const box = document.querySelector('.vd-slot textarea');
 box.value = 'Retain this answer'; box.dispatchEvent(new Event('input'));
 document.querySelector('.vd-note').click();
 document.querySelector('.vd-sheet textarea').value = 'Retain this note'; button('Add note').click();
 if (mode.startsWith('delayed')) await saveStarted;
 const general = document.querySelector('.vd-general');
 if (mode !== 'delayed-empty') { general.value = 'Latest unsaved general'; general.dispatchEvent(new Event('input')); }
 general.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', metaKey:true, bubbles:true}));
 button('Send').click();
 await pause(100);
 const before = await read();
 const retained = document.querySelector('.vd-slot textarea').value;
 const retainedComment = document.querySelector('.vd-pin')?.title;
 const retainedGeneral = general.value;
 if (mode.startsWith('delayed')) release();
 else {
   fail = false;
   if (button('Done')) { button('Done').click(); button('Send').click(); }
 }
 await pause(1200);
 document.body.dataset.result = JSON.stringify({before, retained, retainedComment, retainedGeneral, atDone, activeAtDone, after:await read()});
})();
</script></body></html>"""


@pytest.mark.skipif(not CHROME, reason="needs Chrome or Chromium")
@pytest.mark.parametrize("mode", ["delayed", "delayed-empty", "rejected"])
def test_done_waits_for_acknowledged_saves_and_retains_failed_text_and_notes(
    server, tmp_path, mode
):
    base, _ = server
    review = publish(base, files={"index.html": SUBMIT_SAVE_PAGE.replace(b"MODE", mode.encode())})
    result = browser_result(review["url"], tmp_path, 6000)
    assert result["activeAtDone"] == 0
    assert result["before"]["status"] == "open"
    assert result["retained"] == "Retain this answer"
    assert result["retainedComment"] == "Retain this note"
    assert result["retainedGeneral"] == (
        "" if mode == "delayed-empty" else "Latest unsaved general"
    )
    assert result["atDone"]["general"] == (
        "" if mode == "delayed-empty" else "Latest unsaved general"
    )
    assert result["atDone"]["answers"]["q"]["text"] == "Retain this answer"
    assert result["atDone"]["comments"][0]["text"] == "Retain this note"
    state = result["after"]
    assert state["status"] == "done"
    assert state["answers"]["q"]["text"] == "Retain this answer"
    assert state["comments"][0]["text"] == "Retain this note"
    assert state["general"] == ("" if mode == "delayed-empty" else "Latest unsaved general")


def test_explicit_reopen_preserves_review_content_and_identity(server):
    base, store = server
    review = publish(base)
    rid = review["id"]
    store.update(
        rid,
        {
            "answers": {"a": {"choice": "no"}},
            "general": "Keep this",
            "comments": [{"item": "a", "text": "Keep note"}],
            "status": "done",
        },
    )
    closed = store.close(rid)
    assert (
        httpx.put(f"{base}/api/reviews/{rid}/state", json={"status": "open"}).json()["status"]
        == "closed"
    )
    r = httpx.post(f"{base}/api/reviews/{rid}/reopen")
    assert r.status_code == 200
    reopened = r.json()
    assert reopened["status"] == "open"
    assert reopened["done_at"] is None and reopened["closed_at"] is None
    for field in ["answers", "comments", "general", "number", "revision"]:
        assert reopened[field] == closed[field]
    assert store.file_path(rid, "index.html").read_bytes() == PAGE
    assert Store(store.root).get(rid)["status"] == "open"
    assert httpx.post(f"{base}/api/reviews/missing/reopen").status_code == 404


CENTERED_DONE_PAGE = b"""<html><body><p data-item="q" data-choices="yes|no">Q</p>
<script>
const pause = ms => new Promise(r => setTimeout(r, ms));
(async () => {
 let done;
 while (!(done=[...document.querySelectorAll('button')].find(b=>b.textContent==='Done'))) await pause(50);
 done.click();
 const dialog=document.querySelector('.vd-confirm');
 const r=dialog.getBoundingClientRect();
 document.body.dataset.result=JSON.stringify({
  modal:dialog.matches(':modal'), focus:document.activeElement.textContent,
  width:r.width, height:r.height, x:r.x+r.width/2, y:r.y+r.height/2,
  viewport:[innerWidth,innerHeight]
 });
})();
</script></body></html>"""


@pytest.mark.skipif(not CHROME, reason="needs Chrome or Chromium")
def test_done_confirmation_is_a_compact_centered_modal(server, tmp_path):
    base, _ = server
    review = publish(base, files={"index.html": CENTERED_DONE_PAGE})
    result = browser_result(review["url"], tmp_path, 3000)
    assert result["modal"]
    assert result["focus"] == "Keep editing"
    assert result["width"] <= 448 and result["height"] < result["viewport"][1]
    assert abs(result["x"] - result["viewport"][0] / 2) < 2
    assert abs(result["y"] - result["viewport"][1] / 2) < 2


INDEX_CONTROLS_PAGE = rb"""<html><body><iframe src="/" style="width:360px;height:640px"></iframe>
<script>
const realFetch=window.fetch;
window.fetch=(url,opts)=>realFetch(String(url).replace(/wait=\d+/, 'wait=0'),opts);
const pause=ms=>new Promise(r=>setTimeout(r,ms));
const frame=document.querySelector('iframe');
const doc=()=>frame.contentDocument;
const row=()=>doc()?.querySelector('[data-review="TARGET"]');
const wait=async action=>{const start=Date.now();while(!action()) {if(Date.now()-start>2500) throw Error('Missing review controls'); await pause(50);}};
const state=async()=> (await (await fetch('/api/reviews/TARGET')).json());
(async()=>{
 await wait(()=>row()?.querySelector('button'));
 row().querySelector('button').click();
 const dialog=doc().querySelector('.vd-confirm');
 const r=dialog.getBoundingClientRect();
 const result={modal:dialog.matches(':modal'),width:r.width,
   centered:Math.abs(r.x+r.width/2-frame.contentWindow.innerWidth/2)<2 && Math.abs(r.y+r.height/2-frame.contentWindow.innerHeight/2)<2};
 [...dialog.querySelectorAll('button')].find(b=>b.textContent==='Cancel').click();
 result.cancelled=(await state()).status;
 row().querySelector('button').click();
 doc().querySelector('.vd-confirm .vd-primary').click();
 await wait(()=>row()?.dataset.status==='closed');
 result.closed=await state();
 row().querySelector('button').click();
 result.reopenLabel=doc().querySelector('.vd-confirm .vd-primary').textContent;
 doc().querySelector('.vd-confirm .vd-primary').click();
 await wait(()=>row()?.dataset.status==='open');
 result.reopened=await state();
 document.body.dataset.result=JSON.stringify(result);
})().catch(error=>document.body.dataset.result=JSON.stringify({error:String(error)}));
</script></body></html>"""


@pytest.mark.skipif(not CHROME, reason="needs Chrome or Chromium")
def test_reviews_list_confirms_close_and_reopens_without_losing_answers(server, tmp_path):
    base, store = server
    review = publish(base, title="Synthetic <review>")
    store.update(review["id"], {"answers": {"a": {"choice": "no"}}, "general": "Preserve"})
    driver = publish(
        base, files={"index.html": INDEX_CONTROLS_PAGE.replace(b"TARGET", review["id"].encode())}
    )
    result = browser_result(driver["url"], tmp_path, 6000)
    assert result["modal"] and result["centered"] and result["width"] <= 328
    assert result["cancelled"] == "open"
    assert result["closed"]["status"] == "closed"
    assert result["reopenLabel"] == "Reopen review"
    assert result["reopened"]["status"] == "open"
    for field in ["answers", "comments", "general", "number", "revision"]:
        assert result["reopened"][field] == result["closed"][field]


@pytest.mark.skipif(not CHROME, reason="needs Chrome or Chromium")
def test_reviews_list_failed_action_stays_open_and_can_retry(server, tmp_path):
    base, _ = server
    review = publish(base)
    page = INDEX_CONTROLS_PAGE.replace(b"TARGET", review["id"].encode())
    start = page.index(b" row().querySelector('button').click();")
    end = page.index(b" result.closed=await state();")
    page = (
        page[:start]
        + rb"""
 const original=frame.contentWindow.fetch;
 frame.contentWindow.fetch=()=>Promise.resolve(new Response('',{status:503}));
 row().querySelector('button').click();
 doc().querySelector('.vd-confirm .vd-primary').click();
 await wait(()=>row()?.querySelector('[role="alert"]'));
 const result={error:row().querySelector('[role="alert"]').textContent,
   failedStatus:(await state()).status, enabled:!row().querySelector('button').disabled};
 frame.contentWindow.fetch=original;
 row().querySelector('button').click();
 doc().querySelector('.vd-confirm .vd-primary').click();
 await wait(()=>row()?.dataset.status==='closed');
"""
        + page[end:]
    )
    driver = publish(base, files={"index.html": page})
    result = browser_result(driver["url"], tmp_path, 6000)
    assert result["failedStatus"] == "open" and result["enabled"]
    assert "Try again" in result["error"]
    assert result["closed"]["status"] == "closed"
    assert result["reopened"]["status"] == "open"


PANE_CONTENT = rb"""<html><body><h1>Synthetic responsive review</h1>
<section class="card" id="reading"><h2>Readable content</h2><p>Synthetic review content fills the available area.</p>
<p data-item="q" data-input="Answer">Question</p></section>
<script>const original=window.fetch;window.fetch=(u,o)=>original(String(u).replace(/wait=\d+/,'wait=0'),o);</script>
</body></html>"""

PANE_DRIVER = rb"""<html><body><iframe src="CHILD" style="border:0;width:WIDTHpx;height:HEIGHTpx"></iframe>
<script>
const original=window.fetch;window.fetch=(u,o)=>original(String(u).replace(/wait=\d+/,'wait=0'),o);
const pause=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{
 const frame=document.querySelector('iframe');
 while(!frame.contentDocument?.querySelector('.vd-bar')) await pause(50);
 const d=frame.contentDocument,w=frame.contentWindow,p=d.querySelector('.vd-bar');
 const toggle=d.querySelector('#vd-pane-toggle[data-action=toggle-review-controls]');
 const bounds=()=>{const r=p.getBoundingClientRect(),c=d.querySelector('#reading').getBoundingClientRect();return {x:r.x,y:r.y,width:r.width,height:r.height,right:r.right,bottom:r.bottom,contentWidth:c.width,contentRight:c.right,scrollWidth:d.documentElement.scrollWidth}};
 const result={viewport:[w.innerWidth,w.innerHeight],expanded:bounds(),toggle:!!toggle,label:toggle?.getAttribute('aria-label'),controls:toggle?.getAttribute('aria-controls')};
 if(!toggle){document.body.dataset.result=JSON.stringify(result);return;}
 const note=d.querySelector('.vd-general');note.value='Retain collapse note';note.dispatchEvent(new Event('input'));
 toggle.click();
 result.collapsed= bounds(); result.compactActions=[...p.querySelectorAll('.vd-pane-body button')].filter(b=>['comment mode','Done','Got it'].includes(b.textContent)).every(b=>b.getBoundingClientRect().height>0); result.hidden=getComputedStyle(d.querySelector('.vd-general')).display==='none';
 result.ariaCollapsed=toggle.getAttribute('aria-expanded');
 await pause(900);toggle.click();
 result.expandedAgain=toggle.getAttribute('aria-expanded');
 result.expandedLabel=toggle.getAttribute('aria-label');
 result.buttons=[...p.querySelectorAll('button')].map(b=>b.textContent.trim());
 result.title=p.querySelector('.vd-title').textContent;
 result.sameBox=note===d.querySelector('.vd-general');result.value=note.value;
 document.body.dataset.result=JSON.stringify(result);
})().catch(e=>document.body.dataset.result=JSON.stringify({error:String(e)}));
</script></body></html>"""


@pytest.mark.skipif(not CHROME, reason="needs Chrome or Chromium")
@pytest.mark.parametrize("width,height", [(1280, 800), (375, 812), (320, 568), (640, 320)])
@pytest.mark.parametrize("report", [False, True])
def test_review_controls_collapse_and_leave_full_reading_space(
    server, tmp_path, width, height, report
):
    base, _ = server
    content = PANE_CONTENT.replace(b'data-input="Answer"', b"") if report else PANE_CONTENT
    child = publish(base, files={"index.html": content})
    source = (
        PANE_DRIVER.replace(b"CHILD", child["url"].encode())
        .replace(b"WIDTH", str(width).encode())
        .replace(b"HEIGHT", str(height).encode())
    )
    driver = publish(base, files={"index.html": source})
    result = browser_result(driver["url"], tmp_path, 5000)
    assert result["toggle"], result
    assert result["label"] == "Collapse review controls"
    assert result["controls"] == "vd-pane-body"
    assert result["expandedLabel"] == "Collapse review controls"
    assert ("Got it" if report else "Done") in result["buttons"]
    assert "comment mode" in result["buttons"]
    assert result["title"] == "T"
    assert (
        result["hidden"]
        and result["ariaCollapsed"] == "false"
        and result["expandedAgain"] == "true"
    )
    assert result["compactActions"]
    assert result["sameBox"] and result["value"] == "Retain collapse note"
    expanded, collapsed = result["expanded"], result["collapsed"]
    assert expanded["scrollWidth"] <= width and collapsed["scrollWidth"] <= width
    for bounds in [expanded, collapsed]:
        assert abs(bounds["x"]) < 2 and abs(bounds["bottom"] - height) < 2
        assert bounds["height"] <= height * 0.7 + 2
    assert expanded["contentWidth"] >= width - 48
    assert abs(collapsed["contentWidth"] - expanded["contentWidth"]) < 2
    if width >= 1024:
        assert 280 <= expanded["width"] <= 384
        assert collapsed["width"] == expanded["width"]
    else:
        assert expanded["width"] == width
    assert collapsed["height"] < expanded["height"] - 40
    state = httpx.get(f"{base}/api/reviews/{child['id']}").json()
    assert state["general"] == "Retain collapse note"


CLOSE_ATTEMPT_PAGE = rb"""<html><body><p data-item="q" data-input="Answer">Question</p>
<script>
const original=window.fetch;const mode='MODE';
const read=async()=> (await original(location.pathname.replace('/r/','/api/reviews/').replace(/\/$/,''))).json();
const closeStates=[];window.close=()=>closeStates.push(read());
window.fetch=(url,opts)=>{const p=opts?.method==='PUT'?JSON.parse(opts.body):{};
 if((mode==='answer'&&p.answers)||(mode==='done'&&p.status==='done'))return Promise.resolve(new Response('',{status:503}));
 return original(String(url).replace(/wait=\d+/,'wait=0'),opts);};
const pause=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{let done;while(!(done=[...document.querySelectorAll('button')].find(b=>b.textContent==='Done')))await pause(50);
 const box=document.querySelector('.vd-slot textarea');box.value='Retain close answer';box.dispatchEvent(new Event('input'));
 done.click();document.querySelector('.vd-confirm .vd-primary').click();
 await pause(1200);
 document.body.dataset.result=JSON.stringify({atClose:await Promise.all(closeStates),value:box.value,editable:!box.disabled,status:document.querySelector('.vd-status').textContent});
})();
</script></body></html>"""


@pytest.mark.skipif(not CHROME, reason="needs Chrome or Chromium")
@pytest.mark.parametrize("mode", ["success", "answer", "done"])
def test_close_attempt_requires_acknowledged_done_and_preserves_failed_edits(
    server, tmp_path, mode
):
    base, _ = server
    review = publish(base, files={"index.html": CLOSE_ATTEMPT_PAGE.replace(b"MODE", mode.encode())})
    result = browser_result(review["url"], tmp_path, 5000)
    assert len(result["atClose"]) == (1 if mode == "success" else 0), result
    if mode == "success":
        assert result["atClose"][0]["status"] == "done"
        assert result["atClose"][0]["answers"]["q"]["text"] == "Retain close answer"
    else:
        state = httpx.get(f"{base}/api/reviews/{review['id']}").json()
        assert state["status"] == "open"
        assert result["value"] == "Retain close answer" and result["editable"]
        assert "not sent" in result["status"] or "not saved" in result["status"]


@pytest.mark.skipif(not CHROME, reason="needs Chrome or Chromium")
def test_ordinary_review_tab_retains_honest_sent_and_close_fallback(server, tmp_path):
    base, _ = server
    source = CLOSE_ATTEMPT_PAGE.replace(
        b"const closeStates=[];window.close=()=>closeStates.push(read());",
        b'const closeStates=[];history.pushState(null,"","?ordinary=history");',
    )
    source = source.replace(
        b"status:document.querySelector('.vd-status').textContent",
        b"status:document.querySelector('.vd-status').textContent,history:history.length,opener:!!opener,fallback:document.querySelector('.vd-sent-help')?.textContent,closeButton:!!document.querySelector('.vd-close-tab')",
    )
    source = source.replace(
        b"done.click();document.querySelector('.vd-confirm .vd-primary').click();",
        b"document.querySelector('.vd-pane-toggle').click();box.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',metaKey:true,bubbles:true}));document.querySelector('.vd-confirm .vd-primary').click();",
    )
    source = source.replace(
        b"closeButton:!!document.querySelector('.vd-close-tab')",
        b"closeButton:!!document.querySelector('.vd-close-tab'),paneVisible:getComputedStyle(document.querySelector('.vd-pane-body')).display!=='none'",
    )
    review = publish(base, files={"index.html": source.replace(b"MODE", b"success")})
    result = browser_result(review["url"], tmp_path, 5000)
    assert result["history"] > 1 and not result["opener"]
    assert "sent" in result["status"]
    assert "close this tab" in (result.get("fallback") or "").lower()
    assert result["closeButton"] and result["paneVisible"]


def test_review_disclosure_icon_is_served_as_local_svg(server):
    base, _ = server
    response = httpx.get(base + "/_verdict/chevron-right.svg")
    assert response.status_code == 200 and response.headers["content-type"].startswith(
        "image/svg+xml"
    )
    assert "<svg" in response.text and 'viewBox="0 0 24 24"' in response.text


@pytest.mark.skipif(not CHROME, reason="needs Chrome or Chromium")
def test_done_locks_inputs_until_ack_and_does_not_close_embedded_review(server, tmp_path):
    base, _ = server
    page = CLOSE_ATTEMPT_PAGE.replace(
        b"const closeStates=[];window.close=()=>closeStates.push(read());",
        b"""const closeStates=[];window.close=()=>closeStates.push(read());let release,ready;
const posted=new Promise(r=>ready=r);""",
    )
    page = page.replace(
        b"return original(String(url).replace",
        b"if(p.status==='done')return new Promise(resolve=>{release=()=>resolve(original(url,opts));ready();});\n return original(String(url).replace",
    )
    page = page.replace(
        b" await pause(1200);",
        b""" await posted;
 const locked=box.disabled&&document.querySelector('.vd-general').disabled;
 const early=closeStates.length;
 release();await pause(900);""",
    )
    page = page.replace(
        b"atClose:await Promise.all(closeStates)",
        b"locked,early,atClose:await Promise.all(closeStates)",
    )
    review = publish(base, files={"index.html": page.replace(b"MODE", b"success")})
    result = browser_result(review["url"], tmp_path, 5000)
    assert result["locked"] and result["early"] == 0
    assert len(result["atClose"]) == 1 and result["atClose"][0]["status"] == "done"
    # A review embedded by another page must not close that containing tab.
    child = publish(base, files={"index.html": CLOSE_ATTEMPT_PAGE.replace(b"MODE", b"success")})
    outer = b"""<html><body><iframe src="URL"></iframe><script>
const original=window.fetch;window.fetch=(u,o)=>original(String(u).replace(/wait=\\d+/,'wait=0'),o);
const pause=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{const f=document.querySelector('iframe');while(!f.contentDocument?.body.dataset.result)await pause(50);document.body.dataset.result=f.contentDocument.body.dataset.result;})();
</script></body></html>""".replace(b"URL", child["url"].encode())
    driver = publish(base, files={"index.html": outer})
    result = browser_result(driver["url"], tmp_path / "embedded", 5000)
    assert result["atClose"] == [] and result["value"] == "Retain close answer"


OPTIONAL_PAGE = rb"""<html><body>
<p data-item="required" data-choices="Keep|Change">Required decision</p>
<p data-item="extra" data-input="Anything else?" data-optional>Additional instructions</p>
<script>
const original=window.fetch;window.fetch=(u,o)=>original(String(u).replace(/wait=\d+/,'wait=0'),o);
const pause=ms=>new Promise(r=>setTimeout(r,ms));
const button=t=>[...document.querySelectorAll('button')].find(b=>b.textContent===t);
(async()=>{
 while(!button('Done'))await pause(50);
 if(button('Keep'))button('Keep').click();
 if('FILLED'==='yes'){const box=document.querySelector('[data-for="extra"] textarea');box.value='Optional correction';box.dispatchEvent(new Event('input'));}
 await pause(600);document.querySelector('.vd-toggle').click();
 const progress=document.querySelector('.vd-status').textContent;
 button('Done').click();const confirmation=document.querySelector('.vd-confirm').innerText;
 document.querySelector('.vd-confirm .vd-primary').click();await pause(900);
 document.body.dataset.result=JSON.stringify({progress,confirmation});
})();
</script></body></html>"""


@pytest.mark.skipif(not CHROME, reason="needs Chrome or Chromium")
@pytest.mark.parametrize("only_optional", [False, True])
@pytest.mark.parametrize("filled", [False, True])
def test_optional_instructions_do_not_count_as_unanswered_and_still_save(
    server, tmp_path, only_optional, filled
):
    base, _ = server
    page = OPTIONAL_PAGE.replace(b"FILLED", b"yes" if filled else b"no")
    if only_optional:
        page = page.replace(
            b'<p data-item="required" data-choices="Keep|Change">Required decision</p>', b""
        )
    review = publish(base, files={"index.html": page})
    result = browser_result(review["url"], tmp_path, 5000)
    assert "left unanswered" not in result["confirmation"]
    assert ("no required answers" if only_optional else "1/1 answered") in result["progress"]
    state = httpx.get(f"{base}/api/reviews/{review['id']}").json()
    assert state["status"] == "done"
    if filled:
        assert state["answers"]["extra"]["text"] == "Optional correction"
    else:
        assert "extra" not in state["answers"]


BRAND_PAGE = rb"""<!doctype html><html><head><title>Viewport text is not a tag</title>
<!-- <meta name="viewport" content="fake"> -->
<meta name="icon" content="not-a-link"><link rel="viewport" href="not-a-meta">
<script>const fake="<body>";</script></head><body data-test=">">
<h1>Synthetic brand metadata</h1><script>
const original=window.fetch;window.fetch=(u,o)=>original(String(u).replace(/wait=\d+/,'wait=0'),o);
document.addEventListener('DOMContentLoaded',async()=>{
 const iconCorners=await Promise.all(['apple-touch-icon.png','icon-192.png','icon-512.png'].map(async name=>{
  const img=new Image();img.src='/_verdict/'+name;await img.decode();
  const canvas=document.createElement('canvas');canvas.width=img.width;canvas.height=img.height;
  const ctx=canvas.getContext('2d');ctx.drawImage(img,0,0);
  return [[0,0],[img.width-1,0],[0,img.height-1],[img.width-1,img.height-1]].map(([x,y])=>[...ctx.getImageData(x,y,1,1).data]);
 }));
 document.body.dataset.result=JSON.stringify({iconCorners,mode:document.compatMode,
 viewport:document.querySelector('meta[name="viewport"]')?.content,
 icon:document.querySelector('link[rel="icon"]')?.getAttribute('href'),
 touch:document.querySelector('link[rel="apple-touch-icon"]')?.getAttribute('href'),
 manifest:document.querySelector('link[rel="manifest"]')?.getAttribute('href'),
 appTitle:document.querySelector('meta[name="apple-mobile-web-app-title"]')?.content,
 navInBody:document.querySelector('nav.vd-home')?.parentNode===document.body});});
</script></body></html>"""


@pytest.mark.skipif(not CHROME, reason="needs Chrome or Chromium")
def test_review_brand_metadata_keeps_standards_mode_and_ignores_fake_tags(server, tmp_path):
    base, _ = server
    review = publish(base, files={"index.html": BRAND_PAGE})
    result = browser_result(review["url"], tmp_path, 3000)
    assert result["mode"] == "CSS1Compat" and result["navInBody"]
    assert "device-width" in result["viewport"]
    assert result["icon"] == "/_verdict/favicon.svg"
    assert result["touch"] == "/_verdict/apple-touch-icon.png"
    assert result["manifest"] == "/_verdict/manifest.webmanifest"
    assert result["appTitle"] == "Verdict"
    for corners in result["iconCorners"]:
        assert (
            all(c == corners[0] for c in corners)
            and corners[0][0] < 40
            and corners[0][2] < 100
            and corners[0][3] == 255
        )


def test_brand_icons_and_home_manifest_are_local_and_portable(server):
    base, _ = server
    svg = httpx.get(base + "/_verdict/favicon.svg")
    assert svg.status_code == 200 and svg.headers["content-type"].startswith("image/svg+xml")
    assert "<svg" in svg.text
    for name, size in [("apple-touch-icon.png", 180), ("icon-192.png", 192), ("icon-512.png", 512)]:
        response = httpx.get(base + "/_verdict/" + name)
        assert response.status_code == 200 and response.headers["content-type"] == "image/png"
        assert response.content[:8] == b"\x89PNG\r\n\x1a\n"
        assert struct.unpack(">II", response.content[16:24]) == (size, size)
    response = httpx.get(base + "/_verdict/manifest.webmanifest")
    assert response.status_code == 200
    manifest = response.json()
    assert manifest["name"] == manifest["short_name"] == "Verdict"
    assert manifest["start_url"] == manifest["scope"] == "/"
    assert manifest["display"] == "standalone"
    assert {i["sizes"] for i in manifest["icons"]} == {"192x192", "512x512"}
    assert all(i["src"].startswith("/_verdict/") for i in manifest["icons"])
    root = httpx.get(base + "/").text
    assert "apple-touch-icon" in root and "manifest.webmanifest" in root and "favicon.svg" in root


def test_injected_default_metadata_preserves_explicit_author_choices(server):
    base, _ = server
    source = b"""<!doctype html><html><head><meta NAME='VIEWPORT' content='width=device-width'>
<link rel='icon' href='custom.svg'><link rel='apple-touch-icon' href='touch.png'>
<link rel='manifest' href='custom.webmanifest'><meta name='theme-color' content='#123456'></head><body>Custom page</body></html>"""
    review = publish(base, files={"index.html": source})
    page = httpx.get(review["url"]).text
    assert page.startswith("<!doctype html>")
    assert "custom.svg" in page and "/_verdict/favicon.svg" not in page
    assert "touch.png" in page and "/_verdict/apple-touch-icon.png" not in page
    assert "custom.webmanifest" in page and "/_verdict/manifest.webmanifest" not in page
    assert "content='#123456'" in page


@pytest.mark.skipif(not CHROME, reason="needs Chrome or Chromium")
def test_mobile_review_fields_use_readable_size_and_fast_taps(server, tmp_path):
    base, _ = server
    child = publish(base, files={"index.html": PANE_CONTENT})
    source = rb"""<html><body><iframe src="CHILD" style="width:375px;height:812px"></iframe><script>
const original=window.fetch;window.fetch=(u,o)=>original(String(u).replace(/wait=\d+/,'wait=0'),o);
const pause=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{const f=document.querySelector('iframe');while(!f.contentDocument?.querySelector('.vd-bar'))await pause(50);
 const d=f.contentDocument,w=f.contentWindow;
 const box=d.querySelector('.vd-slot textarea');box.focus();
 document.body.dataset.result=JSON.stringify({font:getComputedStyle(box).fontSize,
 generalFont:getComputedStyle(d.querySelector('.vd-general')).fontSize,
 touch:getComputedStyle(d.body).touchAction,viewport:d.querySelector('meta[name=viewport]')?.content});
})();</script></body></html>""".replace(b"CHILD", child["url"].encode())
    driver = publish(base, files={"index.html": source})
    result = browser_result(driver["url"], tmp_path, 3000)
    assert result["font"] == result["generalFont"] == "16px"
    assert result["touch"] == "manipulation"
    assert "device-width" in result["viewport"] and "initial-scale=1" in result["viewport"]


def test_index_offers_list_and_open_review_feed(server):
    base, store = server
    first = publish(base, title="Feed <first>")
    done = publish(base, title="Finished")
    store.update(done["id"], {"status": "done"})
    page = httpx.get(base + "/").text
    assert 'data-view="list"' in page and 'data-view="feed"' in page
    assert 'id="vd-feed"' in page
    assert f"/r/{first['id']}/" in page
    assert "Feed &lt;first&gt;" in page


FEED_DRIVER = rb"""<html><body><iframe src="BASE/"></iframe><script>
const original=window.fetch;window.fetch=(u,o)=>original(String(u).replace(/wait=\d+/,'wait=0'),o);
const pause=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{
 const outer=document.querySelector('iframe');
 while(!outer.contentDocument?.querySelector('[data-view=feed]')) await pause(50);
 const d=outer.contentDocument;
 d.querySelector('[data-view=feed]').click();
 const frames=()=>[...d.querySelectorAll('.vd-feed-frame')];
 while(frames().length!==3||frames().some(f=>!f.contentDocument?.querySelector('.vd-general')))await pause(50);
 const [a,b,c]=frames(),ad=a.contentDocument,bd=b.contentDocument,cd=c.contentDocument;
 const input=(doc,selector,text)=>{let e=doc.querySelector(selector);e.focus();e.value=text;e.dispatchEvent(new Event('input'));return e};
 const sameBox=input(ad,'.vd-slot textarea','First answer');input(bd,'.vd-slot textarea','Second answer');
 ad.querySelector('.vd-note').click();ad.querySelector('.vd-sheet textarea').value='First comment';[...ad.querySelectorAll('.vd-sheet button')].find(x=>x.textContent==='Add note').click();
 input(ad,'.vd-general','First general');input(bd,'.vd-general','Second general');
 await pause(1000);
 const title=d.querySelector('.vd-feed-post h2 a').getAttribute('href');
 d.querySelector('[data-view=list]').focus();d.querySelector('[data-view=list]').click();d.querySelector('[data-view=feed]').focus();d.querySelector('[data-view=feed]').click();
 const retained=sameBox===ad.querySelector('.vd-slot textarea')&&sameBox.value==='First answer';const restoredFocus=d.activeElement===b&&bd.activeElement===bd.querySelector('.vd-general');
 let fail=true,failDone='MODE'==='done',release,started;const startedSave=new Promise(r=>started=r),real=a.contentWindow.fetch.bind(a.contentWindow);
 a.contentWindow.fetch=(url,opts)=>{if(opts?.method==='PUT'&&JSON.parse(opts.body).status==='done'&&failDone){failDone=false;return Promise.resolve(new a.contentWindow.Response('{}',{status:503}));}if(String(url).endsWith('/state')&&opts?.method==='PUT'&&!JSON.parse(opts.body).status){started();return new Promise(resolve=>release=()=>resolve(fail?new a.contentWindow.Response('{}',{status:503}):real(url,opts)));}return real(url,opts)};
 input(ad,'.vd-general','Retryable feed note');await startedSave;
 ad.querySelector('#vd-pane-toggle').click();
 const notesHidden=getComputedStyle(ad.querySelector('.vd-general')).display==='none';const actions=[...ad.querySelectorAll('.vd-pane-body button')].filter(x=>['comment mode','accept all recommendations','Done'].includes(x.textContent));const actionsVisible=actions.length===3&&actions.every(x=>x.getBoundingClientRect().height>0);actions.find(x=>x.textContent==='comment mode').click();const commentScoped=ad.body.classList.contains('vd-comment-mode')&&!bd.body.classList.contains('vd-comment-mode');actions.find(x=>x.textContent==='comment mode').click();actions.find(x=>x.textContent==='accept all recommendations').click();
 const send=()=>{ad.querySelector('.vd-bar .vd-primary').click();ad.querySelector('.vd-confirm .vd-primary').click()};
 send();await pause(50);const pendingPresent=d.querySelectorAll('.vd-feed-post').length===3;release();await pause(100);
 const failedPresent=d.querySelectorAll('.vd-feed-post').length===3&&ad.querySelector('.vd-general').value==='Retryable feed note'&&!ad.querySelector('.vd-general').disabled;
 cd.querySelector('#vd-pane-toggle').click();cd.querySelector('.vd-primary').click();await pause(200);
 const reportRemoved=d.querySelectorAll('.vd-feed-post').length===2;
 fail=false;send();release();await pause(150);const rejectedDoneRetained='MODE'!=='done'||d.querySelectorAll('.vd-feed-post').length===2;if('MODE'==='done')send();await pause(500);
 document.body.dataset.result=JSON.stringify({title,retained,actionsVisible,notesHidden,commentScoped,rejectedDoneRetained,pendingPresent,failedPresent,reportRemoved,remaining:d.querySelectorAll('.vd-feed-post').length,second:bd.querySelector('.vd-slot textarea').value,asset:ad.querySelector('img').src,focused:restoredFocus});
})().catch(e=>document.body.dataset.result=JSON.stringify({error:String(e)}));
</script></body></html>"""


@pytest.mark.skipif(not CHROME, reason="needs Chrome or Chromium")
@pytest.mark.parametrize("mode", ["save", "done"])
def test_feed_isolates_edits_and_filters_only_acknowledged_completion(server, tmp_path, mode):
    base, _ = server
    content = b'<html><body><p data-item="q" data-input="Answer" data-choices="Yes|No">Question</p><img src="asset.svg"></body></html>'
    first = publish(base, title="First", files={"index.html": content, "asset.svg": b"<svg/>"})
    second = publish(base, title="Second", files={"index.html": content, "asset.svg": b"<svg/>"})
    report = publish(base, title="Report", files={"index.html": b"<h1>Report</h1>"})
    driver = publish(
        base,
        files={
            "index.html": FEED_DRIVER.replace(b"BASE", base.encode()).replace(
                b"MODE", mode.encode()
            )
        },
    )
    # Keep the driver itself out of the feed.
    httpx.post(f"{base}/api/reviews/{driver['id']}/close")
    result = browser_result(driver["url"], tmp_path, 7000)
    assert result.get("retained") and result.get("actionsVisible"), result
    assert result["notesHidden"]
    assert result["commentScoped"] and result["rejectedDoneRetained"] and result["focused"]
    assert result["pendingPresent"] and result["failedPresent"] and result["reportRemoved"]
    assert result["remaining"] == 1 and result["second"] == "Second answer"
    assert result["title"] == f"/r/{first['id']}/"
    assert result["asset"] == first["url"] + "asset.svg"
    a = httpx.get(f"{base}/api/reviews/{first['id']}").json()
    b = httpx.get(f"{base}/api/reviews/{second['id']}").json()
    assert a["status"] == "done" and b["status"] == "open"
    assert a["answers"]["q"]["text"] == "First answer"
    assert b["answers"]["q"]["text"] == "Second answer"
    assert a["answers"]["q"]["choice"] == "Yes"
    assert not b["answers"]["q"].get("choice")
    assert a["comments"][0]["text"] == "First comment" and b["comments"] == []
    assert a["general"] == "Retryable feed note" and b["general"] == "Second general"
    assert httpx.get(f"{base}/api/reviews/{report['id']}").json()["status"] == "done"


FEED_NAV_DRIVER = rb"""<html><body><iframe src="BASE/"></iframe><script>
const original=window.fetch;window.fetch=(u,o)=>original(String(u).replace(/wait=\d+/,'wait=0'),o);
const pause=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{
 const f=document.querySelector('iframe');while(!f.contentDocument?.querySelector('[data-view=feed]'))await pause(50);
 const d=f.contentDocument;d.querySelector('[data-view=feed]').click();
 while(d.querySelectorAll('.vd-feed-frame').length!==2||[...d.querySelectorAll('.vd-feed-frame')].some(x=>!x.contentDocument?.querySelector('.vd-general')))await pause(50);
 const [a,b]=d.querySelectorAll('.vd-feed-frame'),ad=a.contentDocument,bd=b.contentDocument;
 const target=d.querySelector('.vd-feed-post h2 a').target;
 const box=bd.querySelector('.vd-general');box.value='Other review draft';box.dispatchEvent(new Event('input'));
 d.querySelector('[data-view=list]').click();
 const close=which=>{d.querySelectorAll('#vd-list [data-action]')[which].click();d.querySelector('.vd-confirm .vd-primary').click()};
 close(0);await pause(500);
 const retained=box===b.contentDocument.querySelector('.vd-general')&&box.value==='Other review draft';
 let fail=true;const real=b.contentWindow.fetch.bind(b.contentWindow);b.contentWindow.fetch=(u,o)=>o?.method==='PUT'&&fail?Promise.resolve(new b.contentWindow.Response('{}',{status:503})):real(u,o);
 box.value='Retryable close note';box.dispatchEvent(new Event('input'));close(1);await pause(100);
 const failed=d.querySelectorAll('.vd-feed-post').length===1&&!!d.querySelector('.vd-index-error')&&box.value==='Retryable close note';
 fail=false;close(1);await pause(500);
 document.body.dataset.result=JSON.stringify({target,retained,failed,remaining:d.querySelectorAll('.vd-feed-post').length});
})().catch(e=>document.body.dataset.result=JSON.stringify({error:String(e)}));
</script></body></html>"""


@pytest.mark.skipif(not CHROME, reason="needs Chrome or Chromium")
def test_feed_list_close_preserves_other_drafts_and_refuses_unsaved_close(server, tmp_path):
    base, _ = server
    first = publish(base)
    second = publish(base)
    driver = publish(base, files={"index.html": FEED_NAV_DRIVER.replace(b"BASE", base.encode())})
    httpx.post(f"{base}/api/reviews/{driver['id']}/close")
    result = browser_result(driver["url"], tmp_path, 5000)
    assert result.get("target") == "_blank", result
    assert result["retained"] and result["failed"] and result["remaining"] == 0
    assert httpx.get(f"{base}/api/reviews/{first['id']}").json()["status"] == "closed"
    second_state = httpx.get(f"{base}/api/reviews/{second['id']}").json()
    assert second_state["status"] == "closed" and second_state["general"] == "Retryable close note"


FEED_REMOTE_DRIVER = rb"""<html><body><iframe src="BASE/"></iframe><script>
const original=window.fetch;window.fetch=(u,o)=>original(String(u).replace(/wait=\d+/,'wait=0'),o);
const pause=ms=>new Promise(r=>setTimeout(r,ms));
(async()=>{
 const f=document.querySelector('iframe');while(!f.contentDocument?.querySelector('[data-view=feed]'))await pause(50);
 const d=f.contentDocument;d.querySelector('[data-view=feed]').click();
 while(!d.querySelector('.vd-feed-frame')?.contentDocument?.querySelector('.vd-general'))await pause(50);
 const frame=d.querySelector('.vd-feed-frame'),doc=frame.contentDocument;
 let release,started;const saving=new Promise(r=>started=r);
 if('MODE'==='dirty'){
  const real=frame.contentWindow.fetch.bind(frame.contentWindow);
  frame.contentWindow.fetch=(u,o)=>o?.method==='PUT'?new Promise(r=>{release=()=>r(new frame.contentWindow.Response('{}',{status:503}));started()}):real(u,o);
  const box=doc.querySelector('.vd-general');box.value='Local unsaved draft';box.dispatchEvent(new Event('input'));doc.querySelector('.vd-bar .vd-primary').click();doc.querySelector('.vd-confirm .vd-primary').click();d.querySelector('.vd-feed-post h2 a').focus();await saving;
 }
 await original('BASE/api/reviews/RID/'+('MODE'==='closed'?'close':'state'),{method:'MODE'==='closed'?'POST':'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({status:'done'})});
 await pause(5200);
 if(release)release();await pause(50);
 document.body.dataset.result=JSON.stringify({posts:d.querySelectorAll('.vd-feed-post').length,rowStatus:d.querySelector('#vd-list [data-review]').dataset.status,value:doc.querySelector('.vd-general').value});
})().catch(e=>document.body.dataset.result=JSON.stringify({error:String(e)}));
</script></body></html>"""


@pytest.mark.skipif(not CHROME, reason="needs Chrome or Chromium")
@pytest.mark.parametrize("mode", ["clean", "closed", "dirty"])
def test_feed_remote_completion_filters_saved_posts_but_keeps_inflight_edits(
    server, tmp_path, mode
):
    base, _ = server
    review = publish(base)
    page = (
        FEED_REMOTE_DRIVER.replace(b"BASE", base.encode())
        .replace(b"RID", review["id"].encode())
        .replace(b"MODE", mode.encode())
    )
    driver = publish(base, files={"index.html": page})
    httpx.post(f"{base}/api/reviews/{driver['id']}/close")
    result = browser_result(driver["url"], tmp_path, 8000)
    assert result.get("posts") == (1 if mode == "dirty" else 0), result
    assert result["rowStatus"] == (
        "open" if mode == "dirty" else "closed" if mode == "closed" else "done"
    )
    if mode == "dirty":
        assert result["value"] == "Local unsaved draft"
