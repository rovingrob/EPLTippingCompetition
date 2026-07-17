from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
load_dotenv(PROJECT_ROOT / ".env", override=False)

DEFAULT_FILES: dict[str, Any] = {
    "fixtures.json": [],
    "registry.json": [],
    "predictions.json": [],
    "scores.json": [],
    "run_log.json": [],
    "source_state.json": {},
    "season_simulations.json": [],
    "simulation_runs.json": [],
}


class JsonStore:
    def __init__(self, data_dir: Path | str | None = None) -> None:
        env_dir = os.getenv("TIPPING_DATA_DIR")
        self.data_dir = Path(data_dir or env_dir or DEFAULT_DATA_DIR)
        self.lock_path = self.data_dir / ".json-store.lock"

    def ensure_defaults(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with self.locked():
            for filename, default in DEFAULT_FILES.items():
                path = self.data_dir / filename
                if not path.exists():
                    self.write(filename, default)

    def path(self, filename: str) -> Path:
        if filename not in DEFAULT_FILES:
            raise KeyError(f"Unknown store file: {filename}")
        return self.data_dir / filename

    def read(self, filename: str) -> Any:
        path = self.path(filename)
        if not path.exists():
            return json.loads(json.dumps(DEFAULT_FILES[filename]))
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)

    def write(self, filename: str, data: Any) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        path = self.path(filename)
        tmp_path = path.with_name(f".{path.name}.{os.getpid()}.{uuid4().hex}.tmp")
        payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
        tmp_path.write_text(payload + "\n", encoding="utf-8")
        os.replace(tmp_path, path)

    def read_all(self) -> dict[str, Any]:
        self.ensure_defaults()
        return {filename: self.read(filename) for filename in DEFAULT_FILES}

    @contextmanager
    def locked(self, timeout_seconds: float = 30.0, stale_after_seconds: float = 300.0) -> Iterator[None]:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + timeout_seconds
        fd: int | None = None
        while fd is None:
            try:
                fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, json.dumps({"pid": os.getpid(), "created_at": time.time()}).encode("ascii"))
            except FileExistsError:
                self._remove_stale_lock(stale_after_seconds)
                if time.monotonic() > deadline:
                    raise TimeoutError(f"Timed out waiting for JSON store lock at {self.lock_path}")
                time.sleep(0.05)
        try:
            yield
        finally:
            os.close(fd)
            try:
                self.lock_path.unlink()
            except FileNotFoundError:
                pass

    def _remove_stale_lock(self, stale_after_seconds: float) -> None:
        try:
            age = time.time() - self.lock_path.stat().st_mtime
        except FileNotFoundError:
            return
        if age <= stale_after_seconds:
            return
        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            pass


def get_store(data_dir: Path | str | None = None) -> JsonStore:
    store = JsonStore(data_dir)
    store.ensure_defaults()
    return store
