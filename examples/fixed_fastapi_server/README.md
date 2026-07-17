# Fixed FastAPI Contestant Server

This deterministic local server returns a `2-1` home win for every fixture.
It allows browser CORS requests so the leaderboard API tester can call it directly.

```bash
uvicorn examples.fixed_fastapi_server.server:app --host 127.0.0.1 --port 8001
```

It implements the fixture-only v1 contract and ignores fields it does not need.
