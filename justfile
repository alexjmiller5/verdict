set shell := ["bash", "-cu"]
export UV_PROJECT_ENVIRONMENT := env_var("HOME") + "/.cache/uv-venvs/verdict"
export PYTHONPYCACHEPREFIX := env_var("HOME") + "/.cache/pycache/verdict"

default:
    @just --list

# The server with the caller's environment (VERDICT_BIND, VERDICT_PORT, VERDICT_STATE_DIR)
dev:
    uv run verdict serve

test:
    uv run pytest -p no:cacheprovider

# All static analysis (read-only, CI-safe)
check:
    uv run ruff check . && uv run ruff format --check . && nix flake check

fmt:
    uv run ruff format . && uv run ruff check --fix .

# Tail the installed server's log (on the host that runs it)
logs:
    tail -F "${XDG_STATE_HOME:-$HOME/.local/state}/verdict/verdict.log"
