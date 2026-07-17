from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx

from .football_data import FootballDataConfig, MatchSource, sync_matches_once
from .models import (
    PREDICTABLE_STATUSES,
    fixture_sort_key,
    is_resolved_fixture,
    isoformat_z,
    parse_iso_z,
    prediction_lock_at,
    prediction_request_payload,
    utc_now,
)
from .scoring import score_completed_matches, validate_prediction
from .storage import JsonStore, get_store


@dataclass(frozen=True)
class RunnerConfig:
    lock_minutes: int = 30
    lookahead_hours: int = 24
    timeout_seconds: float = 15.0
    retries: int = 1
    concurrency: int = 20
    sync_source: bool = False


def due_prediction_jobs(
    fixtures: list[dict[str, Any]],
    registry: list[dict[str, Any]],
    predictions: list[dict[str, Any]],
    now: datetime,
    config: RunnerConfig,
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    now = now.astimezone(UTC)
    lock_deadline = now + timedelta(minutes=config.lock_minutes)
    lookahead_deadline = now + timedelta(hours=config.lookahead_hours)
    existing = {
        _prediction_key(prediction)
        for prediction in predictions
        if _is_valid_prediction_record(prediction)
    }
    active = [contestant for contestant in registry if contestant.get("status", "active") == "active"]
    jobs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for fixture in sorted(fixtures, key=fixture_sort_key):
        if fixture.get("status") not in PREDICTABLE_STATUSES or not is_resolved_fixture(fixture):
            continue
        kickoff_value = fixture.get("kickoff_at")
        if not kickoff_value:
            continue
        try:
            kickoff_at = parse_iso_z(str(kickoff_value))
        except ValueError:
            continue
        if not (lock_deadline < kickoff_at <= lookahead_deadline):
            continue
        for contestant in active:
            if (contestant["id"], fixture["match_id"]) not in existing:
                jobs.append((fixture, contestant))
    return jobs


async def run_due_once(
    store: JsonStore | None = None,
    config: RunnerConfig | None = None,
    now: datetime | None = None,
    match_source: MatchSource | None = None,
    source_config: FootballDataConfig | None = None,
) -> dict[str, Any]:
    store = store or get_store()
    config = config or RunnerConfig()
    now = (now or utc_now()).astimezone(UTC)
    source_report: dict[str, Any] | None = None
    source_error: str | None = None

    if config.sync_source:
        try:
            source_report = await sync_matches_once(store, match_source, source_config)
        except Exception as exc:
            source_error = f"{type(exc).__name__}: {exc}"

    with store.locked():
        fixtures = store.read("fixtures.json")
        registry = store.read("registry.json")
        predictions = store.read("predictions.json")

    jobs = due_prediction_jobs(fixtures, registry, predictions, now, config)
    attempts = await _call_prediction_jobs(jobs, config)

    with store.locked():
        fixtures = store.read("fixtures.json")
        registry = store.read("registry.json")
        predictions = store.read("predictions.json")
        scores = store.read("scores.json")
        run_log = store.read("run_log.json")

        predictions, recorded_count = _merge_prediction_attempts(predictions, attempts)
        score_count_before = len(scores)
        scores = score_completed_matches(fixtures, registry, predictions, scores)
        entry = {
            "id": str(uuid4()),
            "ran_at": isoformat_z(now),
            "source_fetched": source_report["fetched"] if source_report else 0,
            "source_inserted": source_report["inserted"] if source_report else 0,
            "source_updated": source_report["updated"] if source_report else 0,
            "source_error": source_error,
            "jobs_attempted": len(jobs),
            "predictions_recorded": recorded_count,
            "scores_added": len(scores) - score_count_before,
            "scores_total": len(scores),
        }
        run_log.append(entry)
        store.write("predictions.json", predictions)
        store.write("scores.json", scores)
        store.write("run_log.json", run_log[-200:])
    return entry


def _prediction_key(prediction: dict[str, Any]) -> tuple[str, str]:
    return prediction["contestant_id"], prediction["match_id"]


def _is_valid_prediction_record(prediction: dict[str, Any]) -> bool:
    return bool(prediction.get("valid"))


def _merge_prediction_attempts(
    predictions: list[dict[str, Any]],
    new_predictions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    merged: list[dict[str, Any]] = []
    index_by_key: dict[tuple[str, str], int] = {}
    valid_keys: set[tuple[str, str]] = set()
    for prediction in predictions:
        _merge_prediction_record(merged, index_by_key, valid_keys, prediction)

    recorded_count = 0
    for prediction in new_predictions:
        if _merge_prediction_record(merged, index_by_key, valid_keys, prediction):
            recorded_count += 1
    return merged, recorded_count


def _merge_prediction_record(
    merged: list[dict[str, Any]],
    index_by_key: dict[tuple[str, str], int],
    valid_keys: set[tuple[str, str]],
    prediction: dict[str, Any],
) -> bool:
    key = _prediction_key(prediction)
    if key in valid_keys:
        return False
    existing_index = index_by_key.get(key)
    if existing_index is None:
        index_by_key[key] = len(merged)
        merged.append(prediction)
    else:
        merged[existing_index] = prediction
    if _is_valid_prediction_record(prediction):
        valid_keys.add(key)
    return True


async def _call_prediction_jobs(
    jobs: list[tuple[dict[str, Any], dict[str, Any]]],
    config: RunnerConfig,
) -> list[dict[str, Any]]:
    if not jobs:
        return []
    semaphore = asyncio.Semaphore(max(1, config.concurrency))
    timeout = httpx.Timeout(config.timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async def guarded(fixture: dict[str, Any], contestant: dict[str, Any]) -> dict[str, Any]:
            async with semaphore:
                return await _call_one(client, fixture, contestant, config)

        return await asyncio.gather(*(guarded(fixture, contestant) for fixture, contestant in jobs))


async def _call_one(
    client: httpx.AsyncClient,
    fixture: dict[str, Any],
    contestant: dict[str, Any],
    config: RunnerConfig,
) -> dict[str, Any]:
    requested_at = isoformat_z(utc_now())
    received_at: str | None = None
    payload = prediction_request_payload(fixture)
    response_json: dict[str, Any] | None = None
    error: str | None = None
    valid = False
    prediction: dict[str, Any] | None = None
    for attempt in range(config.retries + 1):
        try:
            response = await client.post(contestant["url"], json=payload)
            received = utc_now()
            received_at = isoformat_z(received)
            response.raise_for_status()
            response_json = response.json()
            if not isinstance(response_json, dict):
                raise ValueError("Prediction response must be a JSON object")
            valid, prediction, validation_error = validate_prediction(fixture, response_json)
            if not valid:
                raise ValueError(validation_error or "Invalid prediction response")
            lock_at = prediction_lock_at(fixture, config.lock_minutes)
            if lock_at is None or received >= lock_at:
                valid = False
                prediction = None
                raise ValueError("Prediction response arrived at or after the fixture lock")
            error = None
            break
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            lock_at = prediction_lock_at(fixture, config.lock_minutes)
            if lock_at is not None and utc_now() >= lock_at:
                break
            if attempt < config.retries:
                await asyncio.sleep(0.2)

    return {
        "id": str(uuid4()),
        "contestant_id": contestant["id"],
        "match_id": fixture["match_id"],
        "requested_at": requested_at,
        "received_at": received_at,
        "valid": valid,
        "prediction": prediction,
        "raw_response": response_json,
        "error": error,
    }
