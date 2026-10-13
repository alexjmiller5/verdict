"""Reviews on disk: one directory per review, state.json plus the published files.

Everything is write-through to disk and held in memory; a Condition wakes
long-polling readers whenever a review's version changes.
"""

import json
import re
import secrets
import shutil
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


def now() -> datetime:
    return datetime.now(UTC)


def stamp(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse(value: str) -> datetime:
    return datetime.fromisoformat(value)


def safe_file(name: str) -> bool:
    return bool(SAFE_NAME.match(name)) and ".." not in name.split("/")


def check_files(files: dict[str, bytes]) -> None:
    if "index.html" not in files:
        raise ValueError("index.html is required")
    for name in files:
        if not safe_file(name):
            raise ValueError(f"bad file name: {name}")


def write_files(folder: Path, files: dict[str, bytes]) -> None:
    folder.mkdir(parents=True)
    for name, content in files.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


class NotFound(KeyError):
    pass


class Store:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.reviews_dir = self.root / "reviews"
        self.reviews_dir.mkdir(parents=True, exist_ok=True)
        self.cond = threading.Condition()
        self.states: dict[str, dict] = {}
        for state_file in self.reviews_dir.glob("*/state.json"):
            state = json.loads(state_file.read_text())
            self.states[state["id"]] = state
        counter = self.root / "last-number"
        self.last_number = max(
            int(counter.read_text()) if counter.exists() else 0,
            max((s.get("number", 0) for s in self.states.values()), default=0),
        )
        for state in sorted(self.states.values(), key=lambda s: (s["created"], s["id"])):
            if "number" not in state:
                state["number"] = self._next_number()
                self._write(state)

    def _next_number(self) -> int:
        self.last_number += 1
        path = self.root / "last-number"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(str(self.last_number))
        tmp.replace(path)
        return self.last_number

    # --- lifecycle -------------------------------------------------------

    def create(self, title: str, task: str | None, files: dict[str, bytes]) -> str:
        check_files(files)
        rid = secrets.token_urlsafe(6).replace("-", "a").replace("_", "b")
        write_files(self.reviews_dir / rid / "files", files)
        created = stamp(now())
        state = {
            "id": rid,
            "title": title,
            "task": task,
            "status": "open",
            "created": created,
            "updated": created,
            "done_at": None,
            "closed_at": None,
            "version": 1,
            "revision": 1,
            "answers": {},
            "comments": [],
            "general": "",
            "waiters": {},
        }
        with self.cond:
            state["number"] = self._next_number()
            self.states[rid] = state
            self._write(state)
        return rid

    def get(self, rid: str) -> dict | None:
        with self.cond:
            state = self.states.get(rid)
            return self._public(state) if state else None

    def list(self) -> list[dict]:
        with self.cond:
            summaries = [self._public(s, summary=True) for s in self.states.values()]
        return sorted(summaries, key=lambda s: s["created"], reverse=True)

    def update(self, rid: str, patch: dict) -> dict:
        with self.cond:
            state = self._state(rid)
            if "answers" in patch:
                for item, answer in patch["answers"].items():
                    state["answers"][item] = {**state["answers"].get(item, {}), **answer}
            if "comments" in patch:
                state["comments"] = list(patch["comments"])
            if "general" in patch:
                state["general"] = str(patch["general"])
            status = patch.get("status")
            if status in ("open", "done") and state["status"] != "closed":
                state["status"] = status
                state["done_at"] = stamp(now()) if status == "done" else None
            self._bump(state)
            return self._public(state)

    def replace(self, rid: str, files: dict[str, bytes], title: str | None = None) -> bool:
        """Swap in a new page, keep answers and notes, reopen; True if it was finished."""
        with self.cond:
            state = self._state(rid)
            check_files(files)
            folder = self.reviews_dir / rid / "files"
            fresh, old = folder.with_name("files.new"), folder.with_name("files.old")
            shutil.rmtree(fresh, ignore_errors=True)
            write_files(fresh, files)
            folder.rename(old)
            fresh.rename(folder)
            shutil.rmtree(old, ignore_errors=True)
            reopened = state["status"] != "open"
            state.update(status="open", done_at=None, closed_at=None)
            state["revision"] = state.get("revision", 1) + 1
            if title:
                state["title"] = title
            self._bump(state)
            return reopened

    def close(self, rid: str) -> dict:
        with self.cond:
            state = self._state(rid)
            state["status"] = "closed"
            state["closed_at"] = stamp(now())
            state["waiters"] = {}
            self._bump(state)
            return self._public(state)

    def delete(self, rid: str) -> None:
        with self.cond:
            self.states.pop(rid, None)
            shutil.rmtree(self.reviews_dir / rid, ignore_errors=True)
            self.cond.notify_all()

    def file_path(self, rid: str, name: str) -> Path | None:
        if rid not in self.states or not safe_file(name):
            return None
        path = self.reviews_dir / rid / "files" / name
        return path if path.is_file() else None

    # --- waiting -----------------------------------------------------------

    def wait_for(self, rid: str, version: int, timeout: float) -> dict | None:
        deadline = now() + timedelta(seconds=timeout)
        with self.cond:
            while True:
                state = self.states.get(rid)
                if state is None:
                    return None
                if state["version"] != version:
                    return self._public(state)
                remaining = (deadline - now()).total_seconds()
                if remaining <= 0:
                    return self._public(state)
                self.cond.wait(remaining)

    def waiter(self, rid: str, pid: int, host: str) -> None:
        with self.cond:
            state = self._state(rid)
            state["waiters"][str(pid)] = {"pid": pid, "host": host, "seen": stamp(now())}
            self._write(state)

    def touch_waiter(self, rid: str, pid: int, seen: datetime) -> None:
        with self.cond:
            self._state(rid)["waiters"][str(pid)]["seen"] = stamp(seen)

    def touch(self, rid: str, created=None, updated=None, closed=None) -> None:
        """Backdate timestamps (tests and gc rehearsals)."""
        with self.cond:
            state = self._state(rid)
            if created:
                state["created"] = stamp(created)
            if updated:
                state["updated"] = stamp(updated)
            if closed:
                state["closed_at"] = stamp(closed)
            self._write(state)

    # --- garbage collection ----------------------------------------------

    def gc(self, stale_days: int = 7, purge_days: int = 30, waiter_minutes: int = 2) -> dict:
        current = now()
        report = {"stale_open": [], "purged": [], "dead_waiters": []}
        with self.cond:
            for state in list(self.states.values()):
                rid = state["id"]
                if state["status"] == "closed" and state["closed_at"]:
                    if current - parse(state["closed_at"]) > timedelta(days=purge_days):
                        report["purged"].append(rid)
                        continue
                elif current - parse(state["updated"]) > timedelta(days=stale_days):
                    report["stale_open"].append(rid)
                for key, waiter in list(state["waiters"].items()):
                    if current - parse(waiter["seen"]) > timedelta(minutes=waiter_minutes):
                        report["dead_waiters"].append(
                            {"review": rid, "pid": waiter["pid"], "host": waiter["host"]}
                        )
                        del state["waiters"][key]
                        self._write(state)
        for rid in report["purged"]:
            self.delete(rid)
        return report

    # --- internals ---------------------------------------------------------

    def _state(self, rid: str) -> dict:
        state = self.states.get(rid)
        if state is None:
            raise NotFound(rid)
        return state

    def _bump(self, state: dict) -> None:
        state["version"] += 1
        state["updated"] = stamp(now())
        self._write(state)
        self.cond.notify_all()

    def _write(self, state: dict) -> None:
        path = self.reviews_dir / state["id"] / "state.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1))
        tmp.replace(path)

    @staticmethod
    def _public(state: dict, summary: bool = False) -> dict:
        public = {k: v for k, v in state.items() if k != "waiters"}
        public["waiters"] = list(state["waiters"].values())
        if summary:
            public["items"] = len(state["answers"])
            public["comment_count"] = len(state["comments"])
            for key in ("answers", "comments", "general"):
                public.pop(key)
        return public
