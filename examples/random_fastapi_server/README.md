# Random FastAPI Contestant Server

This minimal EPL contestant server returns a random score for every fixture. It
allows browser CORS requests so the leaderboard API tester can call it directly.

It exposes:

```http
POST /predict
GET /health
```

## Run

```bash
pip install -r requirements.txt
uvicorn server:app --host 0.0.0.0 --port 8000
```

## Test

```bash
curl -X POST http://localhost:8000/predict \
  -H "Content-Type: application/json" \
  -d '{
    "schema_version": 1,
    "competition": "PL",
    "season": 2026,
    "match_id": "fd-497410",
    "source_match_id": 497410,
    "matchday": 1,
    "kickoff_at": "2026-08-15T19:00:00Z",
    "home_team": {"id": 57, "name": "Arsenal FC", "short_name": "Arsenal", "tla": "ARS"},
    "away_team": {"id": 65, "name": "Manchester City FC", "short_name": "Man City", "tla": "MCI"}
  }'
```
