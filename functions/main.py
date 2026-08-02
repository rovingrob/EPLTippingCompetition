"""Firebase Cloud Function entry point that serves the FastAPI app.

Local/emulator note: this adds the repo root to sys.path so it can import
`epl_tipping` directly from source, which only works because the emulator
runs functions out of this checkout. A real `firebase deploy` only uploads
the contents of `functions/`, so a production deployment would need the
`epl_tipping` package vendored into (or pip-installed within) `functions/`
before this import would succeed.
"""

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("TIPPING_STORAGE_BACKEND", "firestore")
os.environ.setdefault("ADMIN_TOKEN", "local-dev-admin-token")
os.environ.setdefault("ADMIN_COOKIE_SECRET", "local-dev-admin-cookie-secret")

from a2wsgi import ASGIMiddleware  # noqa: E402
from firebase_functions import https_fn  # noqa: E402
from werkzeug.wrappers import Response  # noqa: E402

from epl_tipping.main import app as fastapi_app  # noqa: E402

# ASGIMiddleware spins up a background thread running its own asyncio event
# loop as soon as it's constructed. Gunicorn (which functions-framework uses
# to serve Python functions) imports this module in its master process and
# then forks worker processes - a thread started before that fork does not
# survive into the forked child, leaving every request hanging forever. Build
# it lazily on first request instead, so construction happens inside the
# already-forked worker process.
_wsgi_app = None


def _get_wsgi_app():
    global _wsgi_app
    if _wsgi_app is None:
        _wsgi_app = ASGIMiddleware(fastapi_app)
    return _wsgi_app


@https_fn.on_request()
def app(req: https_fn.Request) -> https_fn.Response:
    return Response.from_app(_get_wsgi_app(), req.environ)
