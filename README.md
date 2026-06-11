# FIFA World Cup Tipping Competition

This competition is for people who want to build a prediction bot for the FIFA World Cup.

Each contestant implements a small HTTP server. Before each match, the competition runner calls every registered server with the match details and all previous results. The contestant server returns a prediction. After the real match result is known, the platform scores each prediction and updates the leaderboard.

## How The Competition Works

1. Contestants build and host a prediction server.
2. Contestants submit their name, server URL, and contact details for `registry.json`.
3. The registry validator checks that the server responds correctly to a test request.
4. Before each match, a cron job calls every active contestant server.
5. Each server receives:
   - the two teams playing
   - the match stage
   - the match identifier
   - all previous completed results
6. Each server returns a prediction.
7. The platform stores the prediction in JSON.
8. When the match result is available, the scorer awards points.
9. The leaderboard is updated with total scores and per-match breakdowns.

## Entry Requirements

To enter, provide the competition organiser with:

- contestant name
- prediction server URL
- contact details for operational issues

The server URL must accept:

```http
POST /predict
```

Servers must:

- return valid JSON
- respond within the configured timeout
- use the exact team names supplied in the request
- return non-negative integer scores
- follow the group stage and knockout stage winner rules
- stay available throughout the tournament

Failed, unavailable, or invalid responses score `0` for that match.

## Data Storage

The competition is intentionally lightweight.

Admin-managed configuration and runtime state live in JSON files:

```text
data/
  fixtures.json
  groups.json
  registry.json
  predictions.json
  scores.json
  run_log.json
  simulations.json
  simulation_runs.json
```

## Local Development

This app uses `uv` for dependency management and a local virtual environment.

```bash
uv sync
uv run pytest
ADMIN_TOKEN=local-dev-admin-token ADMIN_COOKIE_SECRET=local-dev-cookie-secret-change-me uv run uvicorn world_cup_tipping.main:app --host 127.0.0.1 --port 8000
```

Open the app at `http://127.0.0.1:8000/tipping/`.

Admin login sets an encrypted `admin_session` cookie. The raw admin token is not stored in the cookie and bearer-header admin authentication is not accepted.

Run the deterministic local contestant server in another terminal:

```bash
uv run uvicorn examples.fixed_fastapi_server.server:app --host 127.0.0.1 --port 8001
```

Run the cron workflow manually:

```bash
uv run python -m world_cup_tipping.cron run-due
```

The schedule page is the source of truth for fixtures and results. There is no separate results page.

Leaderboard contestant pages can also store simulated full-tournament brackets in `data/simulations.json`. Anyone can run a simulation for a contestant; public runs are limited to one per contestant per UTC day in `data/simulation_runs.json`, while admins can rerun as needed. The app calls that contestant's `/predict` endpoint for all 104 fixtures, builds predicted group tables, resolves knockout placeholders, and saves the latest bracket visualisation.

Contestant pages also include an API tester at `/tipping/leaderboard/{contestant_id}/api-test`. It loads editable JSON payloads matching the runner request shape for next, group, and knockout fixtures, then the browser sends the POST directly to the stored contestant endpoint and validates the response using the same prediction rules as the cron runner. Contestant endpoints need browser-accessible CORS if they are hosted on a different origin.

## Production Hosting

Deployment assets live under `deploy/` and the walkthrough is in `docs/DEPLOYMENT.md`.

Before publishing or deploying, read `SECURITY.md`.

The recommended production shape is:

- run uvicorn from the `uv` virtualenv through systemd
- install the systemd app under `/opt/world-cup-tipping`, especially on SELinux-enforcing hosts
- bind only to `127.0.0.1:8000`
- point Cloudflare Tunnel at `http://127.0.0.1:8000`
- enable the systemd timer for `python -m world_cup_tipping.cron run-due`
- keep admin secrets in `/etc/world-cup-tipping.env`
- put Cloudflare Access in front of `/tipping/admin*`

Production must set `ADMIN_TOKEN`, `ADMIN_COOKIE_SECRET`, and
`ADMIN_COOKIE_SECURE=true`. Use long, random, different values for the two
secrets. Never publish `.env` files or generated deployment env files.

The app exposes a lightweight health check at `GET /tipping/healthz`.

## Open Source Notes

This repo ignores local `.env*` files, source workbooks, and scraped/raw
third-party datasets. Do not publish ignored local data unless you have the
right license and have removed private contestant/contact details.

The importer tests use the local `world_cup_2026_v1.3.xlsx` workbook when it is
present. In a public checkout without that workbook, those tests skip rather
than requiring unpublished source data.

## Fixtures

Fixtures are stored in `data/fixtures.json`.

Each fixture should include:

- `match_id`
- `stage`
- `team_a`
- `team_b`
- `kickoff_at`
- result fields once known

Example:

```json
{
  "match_id": "2026-GROUP-A-001",
  "stage": "group",
  "team_a": "Australia",
  "team_b": "France",
  "kickoff_at": "2026-06-12T20:00:00Z",
  "score_a": null,
  "score_b": null,
  "winner": null
}
```

## Contestant Registry

Contestants are stored in `data/registry.json`.

Example:

```json
[
  {
    "name": "Example Bot",
    "url": "https://example.com/predict",
    "contact": "ops@example.com",
    "status": "active"
  }
]
```

The registry validator runs when `registry.json` changes, or on a regular cron schedule. It validates the registry format and sends a test prediction request to each contestant server.

In the admin UI, saving an endpoint sends a dummy prediction request using the next resolved fixture. Endpoints that fail validation are saved as inactive with the validation error recorded.

## Prediction Request

The competition runner sends a request like this:

```json
{
  "match_id": "2026-GROUP-A-001",
  "stage": "group",
  "team_a": "Australia",
  "team_b": "France",
  "previous_results": [
    {
      "match_id": "2026-GROUP-A-000",
      "stage": "group",
      "team_a": "Germany",
      "team_b": "Japan",
      "score_a": 2,
      "score_b": 1,
      "winner": "Germany"
    }
  ]
}
```

## Prediction Response

Contestant servers must return:

```json
{
  "predicted_score_a": 2,
  "predicted_score_b": 1,
  "predicted_winner": "Australia",
  "confidence": 0.62
}
```

Required fields:

- `predicted_score_a`: non-negative integer
- `predicted_score_b`: non-negative integer

Optional fields:

- `confidence`: number from `0` to `1`

Conditionally required field:

- `predicted_winner`: required depending on stage and predicted score

## Group Stage Prediction Rules

For group stage matches:

- draws are allowed
- `predicted_winner` is required if the predicted score is not a draw
- `predicted_winner` may be omitted or set to `null` if the predicted score is a draw
- if supplied with a drawn scoreline, `predicted_winner` is ignored and the prediction is treated as a draw

Group stage draw example:

```json
{
  "predicted_score_a": 1,
  "predicted_score_b": 1,
  "predicted_winner": null,
  "confidence": 0.48
}
```

Group stage non-draw example:

```json
{
  "predicted_score_a": 2,
  "predicted_score_b": 1,
  "predicted_winner": "Australia",
  "confidence": 0.62
}
```

## Knockout Stage Prediction Rules

For knockout stage matches:

- predicted scores may be draws
- a draw means the predicted match score is level before penalties
- `predicted_winner` is always required
- `predicted_winner` represents the team expected to advance
- `predicted_winner` must be either `team_a` or `team_b`

Knockout draw with penalty winner example:

```json
{
  "predicted_score_a": 1,
  "predicted_score_b": 1,
  "predicted_winner": "Australia",
  "confidence": 0.54
}
```

## Scoring

Group-stage scoring is intentionally simple.
For group-stage predictions, the result is always taken from the predicted scoreline. A drawn predicted scoreline is scored as a draw even if the API response includes `predicted_winner`.

| Outcome | Points |
| --- | ---: |
| Incorrect result | 0 |
| Correct result | 1 |
| Exact score | 1.5 |

Exact score is worth `1.5` total points, not `1 + 1.5`.

Examples:

| Prediction | Actual | Score |
| --- | --- | ---: |
| `2-1` | `2-1` | 1.5 |
| `2-1` | `1-0` | 1 |
| `1-1` | `0-0` | 1 |
| `1-0` | `0-1` | 0 |

Knockout-stage points increase by round. Exact score is still worth an extra `0.5` points.

| Stage | Points |
| --- | ---: |
| `round_of_32` | 1 |
| `round_of_16` | 2 |
| `quarterfinal` | 3 |
| `semifinal` | 4 |
| `third_place` | 5 |
| `final` | 6 |

Examples:

| Stage | Prediction | Actual | Score |
| --- | --- | --- | ---: |
| `final` | `2-1` | `2-0` | 6 |
| `semifinal` | `0-1` | `1-0` | 0 |
| `round_of_16` | `1-1` (team b winner) | `1-1` (team a winner) | 0.5 |
| `round_of_16` | `1-0` | `1-0` | 2.5 |

Drawn knockout scorelines can earn the exact-score bonus, but not the round result points.

For MVP scoring, the result is based on the official match score. Knockout-stage `predicted_winner` is required for the prediction to be valid, but the initial leaderboard scoring uses the scoreline result. A later version may add a separate advancement or penalty winner bonus.

## Operational Rules

The organiser should publish final values before the tournament starts.

Recommended defaults:

- predictions lock 30 minutes before kickoff
- each server gets one prediction per match
- request timeout is 15 seconds
- the runner may retry once after a short delay
- the first valid locked prediction is used
- invalid responses score `0`
- missing responses score `0`
- late responses score `0`
- predictions are hidden until the match locks
- request and response logs are retained for disputes

## Backend Components

The platform has these components:

- `fixtures.json`: teams, fixtures, stages, kickoff times, and results
- `registry.json`: contestant names, server URLs, contact details, and status
- registry validator: checks registry format and test-calls contestant servers
- prediction runner: cron job that calls contestant APIs before each match
- `simulation_runs.json`: public bracket simulation daily run ledger
- result importer: initially manual or semi-manual, later scraped from a reliable source
- scorer: calculates points when results are known
- leaderboard backend: Python service backed by JSON files

## Example Contestant Server

A runnable version of this minimal FastAPI server is available at `examples/random_fastapi_server/server.py`.

```python
from random import choice, randint, random

from fastapi import FastAPI


app = FastAPI()


@app.post("/predict")
def predict(payload: dict):
    team_a = payload["team_a"]
    team_b = payload["team_b"]
    stage = payload["stage"]

    score_a = randint(0, 4)
    score_b = randint(0, 4)

    winner = None
    if score_a > score_b:
        winner = team_a
    elif score_b > score_a:
        winner = team_b
    elif stage != "group":
        winner = choice([team_a, team_b])

    return {
        "predicted_score_a": score_a,
        "predicted_score_b": score_b,
        "predicted_winner": winner,
        "confidence": round(random(), 2),
    }
```

Run it locally:

```bash
cd examples/random_fastapi_server
pip install -r requirements.txt
uvicorn server:app --host 0.0.0.0 --port 8000
```

Test it:

```bash
curl -X POST http://localhost:8000/predict \
  -H "Content-Type: application/json" \
  -d '{
    "match_id": "2026-GROUP-A-001",
    "stage": "group",
    "team_a": "Australia",
    "team_b": "France",
    "previous_results": []
  }'
```

## Recommended Build Order

1. Define `fixtures.json`, `registry.json`, and the prediction API schema.
2. Build the sample FastAPI contestant server.
3. Build the registry validator.
4. Create the SQLite schema.
5. Build the prediction runner cron script.
6. Build the scoring logic.
7. Build a simple leaderboard backend.
8. Add result importing or scraping after the core loop works.

## Launch Checklist

Before the competition starts:

- freeze scoring rules
- freeze API schema
- publish timeout and retry rules
- publish prediction lock timing
- validate all contestant servers
- run at least one mock match
- verify leaderboard scoring
- verify request and response logging
- confirm the result source
