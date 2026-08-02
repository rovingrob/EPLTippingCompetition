# Firebase emulator (local only)

This is an alternate way to run the app locally, in addition to the systemd/VPS
deployment described in `DEPLOYMENT.md`. It serves the same FastAPI app as a
2nd-gen Python Cloud Function behind Firebase Hosting, backed by Firestore
instead of the JSON files.

## Prerequisites

- `firebase-tools` (`npm install -g firebase-tools`)
- Java (required by the Firestore emulator)
- Python 3.12 (the function's runtime; a separate venv is built for it)

## One-time setup

```bash
cd functions
python3.12 -m venv venv
venv/bin/pip install -r requirements.txt
```

## Run it

```bash
firebase emulators:start --project demo-epl-tipping --only functions,firestore,hosting
```

This uses the demo project id in `.firebaserc`, so no `firebase login` is
required. Once ready, the app is served at `http://127.0.0.1:5000` (Hosting
rewrites everything to the function), with the Emulator UI at
`http://127.0.0.1:4000`.

## Storage backend

`epl_tipping/storage.py`'s `get_store()` picks the backend from
`TIPPING_STORAGE_BACKEND`:

- `json` (default) - the existing `JsonStore`, used everywhere else (VPS
  deploy, tests).
- `firestore` - `epl_tipping/firestore_store.py`'s `FirestoreStore`, which
  stores each JSON "file" as one document in a `store` collection. It talks to
  whatever `google.cloud.firestore.Client()` resolves to, so it automatically
  targets the emulator when `FIRESTORE_EMULATOR_HOST` is set.

`functions/main.py` sets `TIPPING_STORAGE_BACKEND=firestore` for the function;
everything else (VPS, tests) is unaffected since the default stays `json`.
`google-cloud-firestore` is an optional dependency (`pip install
epl-tipping[firestore]` / `uv sync --extra firestore`) so the base app and test
suite don't need it installed.

## Caveats

- **sys.path import**: `functions/main.py` adds the repo root to `sys.path` and
  imports `epl_tipping` directly from source. That only works because the
  emulator runs functions out of this checkout. A real `firebase deploy` only
  uploads the contents of `functions/`, so a production deploy would need the
  `epl_tipping` package vendored into (or pip-installed within) `functions/`
  first.
- **ASGIMiddleware must be built lazily**: `a2wsgi.ASGIMiddleware` starts a
  background thread with its own asyncio event loop as soon as it's
  constructed. functions-framework serves Python functions via gunicorn, which
  imports `functions/main.py` in its master process and then forks workers -
  a thread started before that fork doesn't exist in the forked child, so
  every request would hang forever. `functions/main.py` builds the
  `ASGIMiddleware` lazily on first request instead, inside the already-forked
  worker.
