from __future__ import annotations

import copy
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from .storage import DEFAULT_FILES

COLLECTION = "store"
LOCK_DOCUMENT = ("store_locks", "global")


class FirestoreStore:
    """Same public surface as JsonStore, backed by Firestore (or its emulator)."""

    def __init__(self, data_dir: Path | str | None = None) -> None:
        # data_dir is accepted for signature compatibility with JsonStore; unused.
        self._client = None

    @property
    def client(self):
        if self._client is None:
            from google.cloud import firestore  # lazy import: only needed for this backend

            self._client = firestore.Client()
        return self._client

    def _doc(self, filename: str):
        if filename not in DEFAULT_FILES:
            raise KeyError(f"Unknown store file: {filename}")
        return self.client.collection(COLLECTION).document(filename)

    def ensure_defaults(self) -> None:
        with self.locked():
            for filename, default in DEFAULT_FILES.items():
                if not self._doc(filename).get().exists:
                    self.write(filename, default)

    def read(self, filename: str) -> Any:
        snapshot = self._doc(filename).get()
        if not snapshot.exists:
            return copy.deepcopy(DEFAULT_FILES[filename])
        payload = snapshot.to_dict() or {}
        return payload.get("data", copy.deepcopy(DEFAULT_FILES[filename]))

    def write(self, filename: str, data: Any) -> None:
        self._doc(filename).set({"data": data})

    def read_all(self) -> dict[str, Any]:
        self.ensure_defaults()
        return {filename: self.read(filename) for filename in DEFAULT_FILES}

    @contextmanager
    def locked(self, timeout_seconds: float = 30.0, stale_after_seconds: float = 300.0) -> Iterator[None]:
        from google.cloud import firestore

        collection, document = LOCK_DOCUMENT
        lock_ref = self.client.collection(collection).document(document)
        deadline = time.monotonic() + timeout_seconds
        token = uuid4().hex

        @firestore.transactional
        def try_acquire(transaction) -> bool:
            snapshot = lock_ref.get(transaction=transaction)
            existing = snapshot.to_dict() if snapshot.exists else None
            now = time.time()
            if existing and existing.get("held") and (now - existing.get("acquired_at", 0)) <= stale_after_seconds:
                return False
            transaction.set(lock_ref, {"held": True, "acquired_at": now, "token": token})
            return True

        while not try_acquire(self.client.transaction()):
            if time.monotonic() > deadline:
                raise TimeoutError(f"Timed out waiting for Firestore store lock at {collection}/{document}")
            time.sleep(0.05)

        try:
            yield
        finally:
            lock_ref.set({"held": False, "acquired_at": time.time(), "token": token})
