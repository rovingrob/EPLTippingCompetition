from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from .models import (
    PROJECTABLE_STATUSES,
    RESULT_AWAY_WIN,
    RESULT_DRAW,
    RESULT_HOME_WIN,
    fixture_sort_key,
    is_completed_fixture,
    is_resolved_fixture,
    isoformat_z,
    parse_iso_z,
    prediction_request_payload,
    result_key,
    utc_now,
    winner_from_score,
)
from .scoring import validate_prediction
from .storage import JsonStore, get_store


PredictionClient = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
ProgressCallback = Callable[[int, int], Awaitable[None]]


class ProjectionError(RuntimeError):
    pass


@dataclass(frozen=True)
class SimulationConfig:
    timeout_seconds: float = 15.0
    retries: int = 1
    concurrency: int = 5
    european_places: int = 5


async def simulate_season(
    contestant: dict[str, Any],
    fixtures: list[dict[str, Any]],
    config: SimulationConfig | None = None,
    prediction_client: PredictionClient | None = None,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    config = config or SimulationConfig()
    ordered_fixtures = sorted(fixtures, key=fixture_sort_key)
    actual_matches = [_actual_match(fixture) for fixture in ordered_fixtures if is_completed_fixture(fixture)]
    remaining = [
        fixture
        for fixture in ordered_fixtures
        if not is_completed_fixture(fixture)
        and fixture.get("status") in PROJECTABLE_STATUSES
        and is_resolved_fixture(fixture)
    ]
    projected_ids = {fixture["match_id"] for fixture in remaining}
    actual_ids = {match["match_id"] for match in actual_matches}
    omitted = [
        {
            "match_id": fixture.get("match_id"),
            "status": fixture.get("status"),
            "reason": "unresolved_teams" if not is_resolved_fixture(fixture) else "unsupported_status",
        }
        for fixture in ordered_fixtures
        if fixture.get("match_id") not in actual_ids | projected_ids
    ]
    processed = len(actual_matches) + len(omitted)
    total = len(ordered_fixtures)
    if progress_callback:
        await progress_callback(processed, total)

    predicted_matches: list[dict[str, Any]] = []
    if remaining:
        semaphore = asyncio.Semaphore(max(1, config.concurrency))
        timeout = httpx.Timeout(config.timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as http_client:
            async def invoke(fixture: dict[str, Any]) -> dict[str, Any]:
                async with semaphore:
                    return await _predict_fixture(
                        contestant,
                        fixture,
                        config,
                        prediction_client,
                        http_client,
                    )

            tasks = [asyncio.create_task(invoke(fixture)) for fixture in remaining]
            for task in asyncio.as_completed(tasks):
                predicted_matches.append(await task)
                processed += 1
                if progress_callback and (processed == total or processed % 10 == 0):
                    await progress_callback(processed, total)

    matches = sorted([*actual_matches, *predicted_matches], key=fixture_sort_key)
    table = projected_table(matches)
    errors = sum(1 for match in matches if match.get("fallback_used"))
    status = "completed"
    if errors and omitted:
        status = "completed_incomplete_with_fallbacks"
    elif errors:
        status = "completed_with_fallbacks"
    elif omitted:
        status = "completed_incomplete"
    european_count = min(config.european_places, len(table))
    relegation_count = min(3, len(table))
    return {
        "id": str(uuid4()),
        "contestant_id": contestant["id"],
        "contestant_name": contestant.get("name", contestant["id"]),
        "simulated_at": isoformat_z(utc_now()),
        "status": status,
        "error_count": errors,
        "source_fixture_count": len(ordered_fixtures),
        "omitted_match_count": len(omitted),
        "omitted_matches": omitted,
        "actual_match_count": len(actual_matches),
        "predicted_match_count": len(predicted_matches),
        "matches": matches,
        "table": table,
        "champion": table[0]["team"] if table else None,
        "european_places": [row["team"] for row in table[:european_count]],
        "relegated": [row["team"] for row in table[-relegation_count:]] if table else [],
    }


def enqueue_projection(
    contestant_id: str,
    *,
    store: JsonStore | None = None,
    requested_by: str = "public",
    enforce_daily_limit: bool = True,
    now: datetime | None = None,
) -> dict[str, Any]:
    store = store or get_store()
    now = now or utc_now()
    with store.locked():
        registry = store.read("registry.json")
        fixtures = store.read("fixtures.json")
        runs = store.read("projection_runs.json")
        recovered = _recover_stale_runs(runs, now)
        if recovered:
            store.write("projection_runs.json", runs)
        contestant = next((row for row in registry if row.get("id") == contestant_id), None)
        if contestant is None:
            raise ProjectionError("Contestant not found")
        if contestant.get("status", "active") != "active":
            raise ProjectionError("Contestant is not active")
        if any(
            run.get("contestant_id") == contestant_id and run.get("status") in {"queued", "running"}
            for run in runs
        ):
            raise ProjectionError("A season projection is already queued or running")
        if enforce_daily_limit:
            today = _local_date(now)
            if any(
                run.get("contestant_id") == contestant_id
                and run.get("requested_by") == "public"
                and _date_from_iso(run.get("requested_at")) == today
                for run in runs
            ):
                raise ProjectionError("A public season projection has already been requested today")

        run = {
            "id": str(uuid4()),
            "contestant_id": contestant_id,
            "contestant_name": contestant.get("name", contestant_id),
            "requested_by": requested_by,
            "requested_at": isoformat_z(now),
            "started_at": None,
            "last_progress_at": None,
            "completed_at": None,
            "status": "queued",
            "processed": 0,
            "total": len(fixtures),
            "projection_id": None,
            "error": None,
        }
        runs.append(run)
        store.write("projection_runs.json", runs[-1000:])
    return run


async def process_next_projection(
    store: JsonStore | None = None,
    config: SimulationConfig | None = None,
    prediction_client: PredictionClient | None = None,
) -> dict[str, Any] | None:
    store = store or get_store()
    config = config or SimulationConfig()
    with store.locked():
        runs = store.read("projection_runs.json")
        _recover_stale_runs(runs, utc_now())
        queued = next((run for run in runs if run.get("status") == "queued"), None)
        if queued is None:
            store.write("projection_runs.json", runs)
            return None
        run_id = queued["id"]
        contestant_id = queued["contestant_id"]
        queued["status"] = "running"
        queued["started_at"] = isoformat_z(utc_now())
        queued["last_progress_at"] = queued["started_at"]
        queued["error"] = None
        store.write("projection_runs.json", runs)
        registry = store.read("registry.json")
        fixtures = store.read("fixtures.json")

    contestant = next((row for row in registry if row.get("id") == contestant_id), None)
    if contestant is None:
        _finish_run(store, run_id, status="failed", error="Contestant not found")
        return _run_by_id(store, run_id)

    async def progress(processed: int, total: int) -> None:
        _update_run(
            store,
            run_id,
            {"processed": processed, "total": total, "last_progress_at": isoformat_z(utc_now())},
        )

    try:
        projection = await simulate_season(
            contestant,
            fixtures,
            config,
            prediction_client,
            progress,
        )
        projection["run_id"] = run_id
        with store.locked():
            projections = store.read("season_projections.json")
            projections.append(projection)
            store.write("season_projections.json", projections[-200:])
        _finish_run(
            store,
            run_id,
            status="completed",
            projection_id=projection["id"],
            processed=projection["source_fixture_count"],
            total=projection["source_fixture_count"],
        )
    except Exception as exc:
        _finish_run(store, run_id, status="failed", error=f"{type(exc).__name__}: {exc}")
    return _run_by_id(store, run_id)


def latest_projection(projections: list[dict[str, Any]], contestant_id: str) -> dict[str, Any] | None:
    candidates = [row for row in projections if row.get("contestant_id") == contestant_id]
    return max(candidates, key=lambda row: str(row.get("simulated_at") or ""), default=None)


def latest_projection_run(runs: list[dict[str, Any]], contestant_id: str) -> dict[str, Any] | None:
    candidates = [row for row in runs if row.get("contestant_id") == contestant_id]
    return max(candidates, key=lambda row: str(row.get("requested_at") or ""), default=None)


def projected_table(matches: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for match in matches:
        home_key = _team_key(match, "home")
        away_key = _team_key(match, "away")
        home = rows.setdefault(home_key, _table_row(match, "home"))
        away = rows.setdefault(away_key, _table_row(match, "away"))
        score_home = int(match["score_home"])
        score_away = int(match["score_away"])
        home["played"] += 1
        away["played"] += 1
        home["goals_for"] += score_home
        home["goals_against"] += score_away
        away["goals_for"] += score_away
        away["goals_against"] += score_home
        result = result_key(score_home, score_away)
        if result == RESULT_HOME_WIN:
            home["won"] += 1
            home["points"] += 3
            away["lost"] += 1
        elif result == RESULT_AWAY_WIN:
            away["won"] += 1
            away["points"] += 3
            home["lost"] += 1
        else:
            home["drawn"] += 1
            away["drawn"] += 1
            home["points"] += 1
            away["points"] += 1

    for row in rows.values():
        row["goal_difference"] = row["goals_for"] - row["goals_against"]
    ordered = sorted(
        rows.values(),
        key=lambda row: (
            -row["points"],
            -row["goal_difference"],
            -row["goals_for"],
            row["team"].casefold(),
        ),
    )
    for position, row in enumerate(ordered, start=1):
        row["position"] = position
    return ordered


async def _predict_fixture(
    contestant: dict[str, Any],
    fixture: dict[str, Any],
    config: SimulationConfig,
    prediction_client: PredictionClient | None,
    http_client: httpx.AsyncClient,
) -> dict[str, Any]:
    request_payload = prediction_request_payload(fixture)
    raw: dict[str, Any] | None = None
    error: str | None = None
    valid = False
    prediction: dict[str, Any] | None = None
    for attempt in range(config.retries + 1):
        try:
            if prediction_client is not None:
                raw = await prediction_client(request_payload)
            else:
                response = await http_client.post(contestant["url"], json=request_payload)
                response.raise_for_status()
                raw = response.json()
            if not isinstance(raw, dict):
                raise ValueError("Prediction response must be a JSON object")
            valid, prediction, validation_error = validate_prediction(fixture, raw)
            if not valid:
                raise ValueError(validation_error or "Invalid prediction response")
            error = None
            break
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            if attempt < config.retries:
                await asyncio.sleep(0.2)
    fallback_used = not valid or prediction is None
    if fallback_used:
        prediction = {"predicted_score_home": 0, "predicted_score_away": 0, "confidence": None}

    score_home = int(prediction["predicted_score_home"])
    score_away = int(prediction["predicted_score_away"])
    return _match_payload(
        fixture,
        score_home,
        score_away,
        actual_result=False,
        valid=valid,
        fallback_used=fallback_used,
        error=error,
        confidence=prediction.get("confidence"),
    )


def _actual_match(fixture: dict[str, Any]) -> dict[str, Any]:
    return _match_payload(
        fixture,
        int(fixture["score_home"]),
        int(fixture["score_away"]),
        actual_result=True,
        valid=True,
        fallback_used=False,
        error=None,
        confidence=None,
    )


def _match_payload(
    fixture: dict[str, Any],
    score_home: int,
    score_away: int,
    *,
    actual_result: bool,
    valid: bool,
    fallback_used: bool,
    error: str | None,
    confidence: float | None,
) -> dict[str, Any]:
    return {
        "match_id": fixture["match_id"],
        "source_match_id": fixture.get("source_match_id"),
        "matchday": fixture.get("matchday"),
        "kickoff_at": fixture.get("kickoff_at"),
        "home_team_id": fixture.get("home_team_id"),
        "home_team": fixture.get("home_team"),
        "home_team_short_name": fixture.get("home_team_short_name"),
        "away_team_id": fixture.get("away_team_id"),
        "away_team": fixture.get("away_team"),
        "away_team_short_name": fixture.get("away_team_short_name"),
        "score_home": score_home,
        "score_away": score_away,
        "result": result_key(score_home, score_away),
        "winner": winner_from_score(
            fixture.get("home_team"),
            fixture.get("away_team"),
            score_home,
            score_away,
        ),
        "actual_result": actual_result,
        "valid": valid,
        "fallback_used": fallback_used,
        "error": error,
        "confidence": confidence,
    }


def _team_key(match: dict[str, Any], side: str) -> str:
    team_id = match.get(f"{side}_team_id")
    return f"id:{team_id}" if team_id is not None else f"name:{match.get(f'{side}_team')}"


def _table_row(match: dict[str, Any], side: str) -> dict[str, Any]:
    return {
        "team_id": match.get(f"{side}_team_id"),
        "team": match.get(f"{side}_team_short_name") or match.get(f"{side}_team") or "TBD",
        "played": 0,
        "won": 0,
        "drawn": 0,
        "lost": 0,
        "goals_for": 0,
        "goals_against": 0,
        "goal_difference": 0,
        "points": 0,
    }


def _update_run(store: JsonStore, run_id: str, updates: dict[str, Any]) -> None:
    with store.locked():
        runs = store.read("projection_runs.json")
        for run in runs:
            if run.get("id") == run_id:
                run.update(updates)
                break
        store.write("projection_runs.json", runs)


def _finish_run(
    store: JsonStore,
    run_id: str,
    *,
    status: str,
    projection_id: str | None = None,
    processed: int | None = None,
    total: int | None = None,
    error: str | None = None,
) -> None:
    updates: dict[str, Any] = {
        "status": status,
        "completed_at": isoformat_z(utc_now()),
        "projection_id": projection_id,
        "error": error,
    }
    if processed is not None:
        updates["processed"] = processed
    if total is not None:
        updates["total"] = total
    _update_run(store, run_id, updates)


def _run_by_id(store: JsonStore, run_id: str) -> dict[str, Any] | None:
    return next((run for run in store.read("projection_runs.json") if run.get("id") == run_id), None)


def _recover_stale_runs(runs: list[dict[str, Any]], now: datetime) -> int:
    stale_after = max(60, int(os.getenv("TIPPING_PROJECTION_STALE_SECONDS", "7200")))
    recovered = 0
    for run in runs:
        if run.get("status") != "running":
            continue
        timestamp = run.get("last_progress_at") or run.get("started_at")
        try:
            last_activity = parse_iso_z(str(timestamp))
        except (TypeError, ValueError):
            last_activity = None
        if last_activity is not None and (now.astimezone(last_activity.tzinfo) - last_activity).total_seconds() <= stale_after:
            continue
        run.update(
            {
                "status": "failed",
                "completed_at": isoformat_z(now),
                "error": "Projection worker stopped before completing the run",
            }
        )
        recovered += 1
    return recovered


def _display_timezone() -> ZoneInfo:
    name = os.getenv("TIPPING_DISPLAY_TIMEZONE", "Australia/Sydney").strip() or "Australia/Sydney"
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def _local_date(value: datetime) -> str:
    return value.astimezone(_display_timezone()).date().isoformat()


def _date_from_iso(value: Any) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return _local_date(parsed)
