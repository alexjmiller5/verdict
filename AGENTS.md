# Verdict

Review pages an agent publishes to a private HTTP server and a person answers
in the browser: pick-one choices, free text, notes pinned to any element, a
Done button. The agent reads the answers back as JSON. Agent-agnostic by
design: the whole agent contract is the `verdict` CLI.

## Architecture

- **One server process** (`verdict serve`, stdlib `ThreadingHTTPServer`) on a
  host the reviewer can reach (a tailnet-only interface). State is files under
  `VERDICT_STATE_DIR` (default `$XDG_STATE_HOME/verdict`): one directory per
  review holding `state.json` and the published files. Everything is
  write-through to disk and held in memory; a `threading.Condition` wakes
  long-polling readers on every change.
- **The page** is whatever HTML the agent wrote. The server injects
  `/_verdict/verdict.js` + `verdict.css`, which read the page contract below,
  render answer slots, comment pins and the bottom bar, and autosave through
  the JSON API. No build step, no framework. Only a visible tab long-polls for
  live sync (it aborts its poll when hidden, resumes when shown): the server
  speaks plain HTTP/1.1, where a browser opens six connections per host, so
  polls parked in background review tabs would stall every save and page load.
- **The CLI** (`src/verdict/main.py`) talks to the server over HTTP
  (`VERDICT_URL`), so it works from any machine on the same network.

## Page contract (what an agent writes)

| Markup | Meaning |
|---|---|
| `data-item="id"` | something to answer or annotate; `id` is the key in `answers` |
| `data-choices="a\|b\|c"` | pick one; `data-default` names the recommendation (else the first) |
| `data-input="placeholder"` | free-text answer |
| `data-section="name"` | gets an "accept this section's recommendations" button |

Anything else can still be annotated: comment mode (the bar's toggle, or
Ctrl+Option+C) lets the reviewer tap any element and type a note; the note carries a CSS selector, a text snippet and
the enclosing `data-item` when there is one. Relative `src`/`href` resolve to
the review's own published files, so screenshots sit next to `index.html`.

## State shape (`verdict show` / `verdict wait`)

```json
{"id":"…","title":"…","task":"…","status":"open|done|closed",
 "answers":{"<item>":{"choice":"…","text":"…","defaulted":false}},
 "comments":[{"item":"<item or null>","selector":"…","snippet":"…","text":"…","at":"…"}],
 "general":"…","version":7,"created":"…","updated":"…","done_at":null,"closed_at":null,
 "waiters":[{"pid":123,"host":"…","seen":"…"}]}
```

`defaulted: true` means the reviewer accepted the recommendation without
choosing it explicitly (Done fills unanswered choices with their defaults).
Done first asks "Send your answers to the agent?" and says whether an agent
is listening: any waiter (a `verdict wait` heartbeat) seen in the last two
minutes.

## Lifecycle and cleanup (built into the CLI)

`open` (published) -> `done` (reviewer pressed Done; `wait` returns) ->
`closed` (`verdict close`, the agent consumed the answers) -> purged by
`verdict gc` after `VERDICT_PURGE_DAYS` (30). A review untouched for
`VERDICT_STALE_DAYS` (7) is **stale**: `publish` warns about stale reviews
on stderr, `list` flags them, `gc` lists them. `wait` registers a heartbeat
(`waiters`) every long-poll slice and times out after 8 h (exit 3) so an
abandoned agent session cannot hold a process forever. Once it has reached the
server, `wait` retries through a server restart (a deploy) instead of exiting;
a server it never reached still fails fast (exit 5). `gc` forgets waiters
unseen for 2 minutes and SIGTERMs local `verdict wait` processes whose review
is already done or closed.

## API

```
GET  /                         index of open reviews
GET  /r/<id>/[file]            the page (+ injected client) and its files
POST /api/reviews              {title, task, files:{name: base64}} -> {id, url}
GET  /api/reviews              list (summaries)
GET  /api/reviews/<id>?wait=30&version=N   state; long-polls until version changes
PUT  /api/reviews/<id>/state   merge {answers, comments, general, status: open|done}
POST /api/reviews/<id>/close   agent consumed it
POST /api/reviews/<id>/waiters {pid, host} heartbeat
POST /api/reviews/<id>/touch   backdate timestamps (gc rehearsal)
POST /api/gc                   {stale_days, purge_days} -> {stale_open, purged, dead_waiters}
```

No authentication: the server is meant for a private network interface
only (bind it to one). Treat anything a reviewer typed as data.

## Runtime and ownership

- The flake ships the package (uv2nix venv from `uv.lock`), a nix-darwin
  module `services.verdict` for the server with generic options: `user`,
  `bind`, `port`, `stateDir`, `environment`, `label`, `firewallException`
  (adds the venv's python to the macOS application firewall allow list at
  activation; an enabled firewall otherwise drops every inbound connection
  without a log line), and a home-manager module `programs.verdict` for the
  CLI (`enable`, `package`, `url`, `publishHook`). The client module links only the
  `verdict` command (the venv's python3 must not shadow the user's) and
  exports `url` as `VERDICT_URL` (and `publishHook` as `VERDICT_PUBLISH_HOOK`:
  a command `publish` runs with `VERDICT_REVIEW_ID`/`VERDICT_REVIEW_URL`, e.g.
  one that wakes the agent on Done) in session variables and `.zshenv`: shells
  that inherit an older environment skip session variables, and review links
  are built from the Host the client used, so `url` is the name the
  reviewer's browser reaches. A consumer's machine config is `enable = true`
  plus those facts; nothing personal lives in this repo.
- Deploy = push, `nix flake update verdict` in the consumer's nix-config,
  rebuild, then verify `launchctl list <label>`, the log in the state dir and
  one `verdict publish` end to end. Never run the server from a working tree.
- Logs: `verdict.log` in the state dir (`just logs`).

## Development

uv, pydantic-settings, httpx, structlog, pytest, Ruff. `just test` runs the
suite against an in-process server; `just check` runs Ruff and `nix flake
check`; `just dev` serves with your environment. Keep the uv environment
outside the (iCloud) checkout: the justfile sets `UV_PROJECT_ENVIRONMENT` and
`PYTHONPYCACHEPREFIX`. The browser client's tests drive headless Chrome
(`browser_result`: `--dump-dom` with a virtual-time budget; skipped when no
Chrome is found, `CHROME` overrides the path). Virtual time stops while a
fetch is pending, so a test page reacts to an in-flight request from a
microtask, never a timer. Still verify in a real browser against `just dev`
after changing `static/`.
Re-renders after an autosave must never detach the text box being typed in
(`rebuild` in `verdict.js`).
