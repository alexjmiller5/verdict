# Verdict

Review pages an agent publishes and a person answers in the browser.

An agent that needs decisions, feedback on a design, or a look at screenshots
writes an ordinary HTML page, publishes it with one command, and sends the
link. The reviewer opens it on any device, picks answers, pins notes to any
part of the page and presses Done. The agent reads the answers back as JSON.
Any agent that can run a shell command can use it.

```sh
verdict publish ./review/ --title "Finance categories" --task 42   # prints the link
verdict wait <id>          # blocks until Done, prints answers as JSON (exit 3 on timeout)
verdict show <id>          # the current state, any time
verdict close <id>         # answers consumed; purged later by gc
verdict list               # open and done reviews, stale ones flagged
verdict gc                 # purge old closed reviews, kill orphan waits, list stale reviews
```

The page contract is three attributes: `data-item="id"` marks something to
answer, `data-choices="a|b|c"` (with `data-default`) makes it a pick-one,
`data-input` makes it free text. Everything else is still annotatable in
comment mode. Images next to `index.html` are published with it.

## Install

The flake ships the package and a nix-darwin module:

```nix
services.verdict = {
  enable = true;
  user = "me";
  bind = "<tailnet ip>";                       # a private interface, never 0.0.0.0
  port = 7071;
  publicUrl = "http://host.example.ts.net:7071";  # exported as VERDICT_URL
  label = "com.example.verdict";
};
```

Clients elsewhere need only `VERDICT_URL` and the package on PATH. The server
has no authentication; its privacy is the network interface it binds to.

Configuration is `VERDICT_*` environment variables: `VERDICT_URL` (client),
`VERDICT_BIND`, `VERDICT_PORT`, `VERDICT_STATE_DIR` (server),
`VERDICT_STALE_DAYS` (7) and `VERDICT_PURGE_DAYS` (30).

See [AGENTS.md](AGENTS.md) for the architecture, page contract and API.
