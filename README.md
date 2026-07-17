# EPL Tipping Competition

A prediction-bot competition for one configured English Premier League season.
Each contestant hosts a small HTTP endpoint. The runner sends upcoming fixtures,
records the first valid prediction, imports fixtures and final scores from
[football-data.org](https://www.football-data.org/), and maintains the public
leaderboard.

The application is a single-worker FastAPI/Jinja service with JSON persistence.
Public pages live below `/tipping/`.

## Competition rules

- The runner requests predictions from 24 hours until 30 minutes before kickoff.
- The first valid prediction for a contestant and fixture is retained, including
  when the source later changes the kickoff time.
- Predictions remain hidden from public HTML and JSON until the 30-minute lock.
- An exact score earns 1.5 points in total.
- A correct home/draw/away result earns 1 point.
- Every other, missing, timed-out, or invalid response earns 0 points.
- Admin-entered results are explicit overrides and are not replaced by source
  synchronisation until the override is cleared.

Kickoffs are stored in UTC and rendered in each user's browser timezone. The
Today page also uses that timezone to decide which fixtures belong to the
selected date. Competition-wide daily limits use `Australia/Sydney` by default.

## Contestant API v1

Contestants expose `POST /predict`, accept JSON, and respond within 15 seconds.
The request contains only the fixture; historical results are intentionally not
included.

```json
{
  "schema_version": 1,
  "competition": "PL",
  "season": 2026,
  "match_id": "fd-497410",
  "source_match_id": 497410,
  "matchday": 1,
  "kickoff_at": "2026-08-15T19:00:00Z",
  "home_team": {
    "id": 57,
    "name": "Arsenal FC",
    "short_name": "Arsenal",
    "tla": "ARS"
  },
  "away_team": {
    "id": 65,
    "name": "Manchester City FC",
    "short_name": "Man City",
    "tla": "MCI"
  }
}
```

The response requires non-negative integer scores. `confidence` is optional and,
when present, must be from 0 to 1.

```json
{
  "predicted_score_home": 2,
  "predicted_score_away": 1,
  "confidence": 0.62
}
```

See `examples/` for deterministic and random FastAPI contestant servers.

## Local development

Install dependencies and create a local `.env` without committing it:

```bash
uv sync
```

```text
FOOTBALL_DATA_TOKEN=<your football-data.org token>
TIPPING_COMPETITION_CODE=PL
TIPPING_SEASON=2026
TIPPING_COMPETITION_TIMEZONE=Australia/Sydney
ADMIN_TOKEN=local-dev-admin-token
ADMIN_COOKIE_SECRET=use-a-different-long-random-value
ADMIN_COOKIE_SECURE=false
```

The source client loads the root `.env` for local development. Production uses
the protected systemd environment file instead. The installer deliberately does
not copy local `.env` files; after placing the token in `/etc/epl-tipping.env`,
the repository-root `.env` can be removed.

Start the app:

```bash
uv run uvicorn epl_tipping.main:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000/tipping/>. Run tests with:

```bash
uv run pytest
```

## Operations

Synchronise the full configured season from football-data.org:

```bash
uv run python -m epl_tipping.cron sync-fixtures
```

Preview a sync without writing fixture or source state:

```bash
uv run python -m epl_tipping.cron sync-fixtures --dry-run
```

Synchronise, collect due predictions, and score completed fixtures:

```bash
uv run python -m epl_tipping.cron run-due
```

If the provider is temporarily unavailable, `run-due` records the source error
and continues with cached fixtures. Use `--no-sync-source` to skip the source
request intentionally.

Public full-season simulation requests are durably queued. A worker combines
authoritative completed results with a contestant's predictions for every
remaining fixture, then calculates the simulated table. Process one queued run:

```bash
uv run python -m epl_tipping.cron process-simulation
```

The deployment timer runs the due workflow every four hours (six source syncs
per day) and polls the simulation queue every minute. Simulation calls use five
concurrent requests, one retry, and a deterministic fallback after failure.
Public users may request one simulation per contestant per Sydney calendar day;
admins are exempt from that limit.

## Storage

Runtime state is stored atomically in the configured data directory:

```text
data/
  fixtures.json
  registry.json
  predictions.json
  scores.json
  run_log.json
  source_state.json
  season_simulations.json
  simulation_runs.json
```

Use only one app worker while JSON storage is enabled. Set
`TIPPING_DATA_DIR=/var/lib/epl-tipping/data` in production.

## Deployment and security

Production systemd and Cloudflare Tunnel assets are in `deploy/`; follow
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md). Before publishing or deploying,
read [`SECURITY.md`](SECURITY.md).

Required production secrets are `FOOTBALL_DATA_TOKEN`, `ADMIN_TOKEN`, and a
different `ADMIN_COOKIE_SECRET`. Never commit `.env` files or expose the source
token to browsers, logs, templates, or contestant endpoints.

The health endpoint is `GET /tipping/healthz`.
