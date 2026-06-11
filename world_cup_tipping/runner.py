from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx

from .models import completed_results, is_resolved_fixture, isoformat_z, parse_iso_z, utc_now
from .scoring import score_completed_matches, validate_prediction
from .storage import JsonStore, get_store


@dataclass(frozen=True)
class RunnerConfig:
    lock_minutes: int = 30
    lookahead_hours: int = 24
    timeout_seconds: float = 15.0
    retries: int = 1


def due_prediction_jobs(
    fixtures: list[dict[str, Any]],
    registry: list[dict[str, Any]],
    predictions: list[dict[str, Any]],
    now: datetime,
    config: RunnerConfig,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    lock_deadline = now + timedelta(minutes=config.lock_minutes)
    lookahead_deadline = now + timedelta(hours=config.lookahead_hours)
    existing = {(prediction["contestant_id"], prediction["match_id"]) for prediction in predictions}
    active_contestants = [contestant for contestant in registry if contestant.get("status", "active") == "active"]
    jobs = []
    for fixture in fixtures:
        if not is_resolved_fixture(fixture):
            continue
        kickoff_at = parse_iso_z(fixture["kickoff_at"])
        if not (lock_deadline <= kickoff_at <= lookahead_deadline):
            continue
        for contestant in active_contestants:
            if (contestant["id"], fixture["match_id"]) not in existing:
                jobs.append((fixture, contestant))
    return jobs


async def run_due_once(store: JsonStore | None = None, config: RunnerConfig | None = None, now: datetime | None = None) -> dict[str, Any]:
    store = store or get_store()
    config = config or RunnerConfig()
    now = (now or utc_now()).astimezone(UTC)

    with store.locked():
        fixtures = store.read("fixtures.json")
        registry = store.read("registry.json")
        predictions = store.read("predictions.json")
        scores = store.read("scores.json")
        run_log = store.read("run_log.json")

    jobs = due_prediction_jobs(fixtures, registry, predictions, now, config)
    previous_results = completed_results(fixtures)
    new_predictions = await _call_prediction_jobs(jobs, previous_results, config)

    with store.locked():
        fixtures = store.read("fixtures.json")
        registry = store.read("registry.json")
        predictions = store.read("predictions.json")
        scores = store.read("scores.json")
        run_log = store.read("run_log.json")

        existing = {(prediction["contestant_id"], prediction["match_id"]) for prediction in predictions}
        for prediction in new_predictions:
            if (prediction["contestant_id"], prediction["match_id"]) not in existing:
                predictions.append(prediction)
                existing.add((prediction["contestant_id"], prediction["match_id"]))

        score_count_before = len(scores)
        scores = score_completed_matches(fixtures, registry, predictions, scores)
        entry = {
            "id": str(uuid4()),
            "ran_at": isoformat_z(now),
            "jobs_attempted": len(jobs),
            "predictions_recorded": len(new_predictions),
            "scores_added": len(scores) - score_count_before,
            "scores_total": len(scores),
        }
        run_log.append(entry)
        store.write("predictions.json", predictions)
        store.write("scores.json", scores)
        store.write("run_log.json", run_log[-200:])
    return entry


async def _call_prediction_jobs(
    jobs: list[tuple[dict[str, Any], dict[str, Any]]],
    previous_results: list[dict[str, Any]],
    config: RunnerConfig,
) -> list[dict[str, Any]]:
    if not jobs:
        return []
    timeout = httpx.Timeout(config.timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await asyncio.gather(
            *[_call_one(client, fixture, contestant, previous_results, config) for fixture, contestant in jobs]
        )


async def _call_one(
    client: httpx.AsyncClient,
    fixture: dict[str, Any],
    contestant: dict[str, Any],
    previous_results: list[dict[str, Any]],
    config: RunnerConfig,
) -> dict[str, Any]:
    requested_at = isoformat_z(utc_now())
    payload = {
        "match_id": fixture["match_id"],
        "stage": fixture["stage"],
        "team_a": fixture["team_a"],
        "team_b": fixture["team_b"],
        "previous_results": previous_results,
    }

    response_json: dict[str, Any] | None = None
    error: str | None = None
    for attempt in range(config.retries + 1):
        try:
            response = await client.post(contestant["url"], json=payload)
            response.raise_for_status()
            response_json = response.json()
            error = None
            break
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if attempt < config.retries:
                await asyncio.sleep(0.2)

    valid = False
    prediction: dict[str, Any] | None = None
    if response_json is not None:
        valid, prediction, validation_error = validate_prediction(fixture, response_json)
        if validation_error:
            error = validation_error

    return {
        "id": str(uuid4()),
        "contestant_id": contestant["id"],
        "match_id": fixture["match_id"],
        "requested_at": requested_at,
        "valid": valid,
        "prediction": prediction,
        "raw_response": response_json,
        "error": error,
    }
