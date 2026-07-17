from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx

from epl_tipping import runner
from epl_tipping.models import isoformat_z, prediction_request_payload
from epl_tipping.runner import RunnerConfig, due_prediction_jobs, run_due_once


NOW = datetime(2026, 8, 15, 12, 0, tzinfo=UTC)


def contestant(contestant_id: str, *, status: str = "active") -> dict:
    return {
        "id": contestant_id,
        "name": contestant_id.title(),
        "url": f"https://{contestant_id}.example/predict",
        "status": status,
    }


def test_due_jobs_include_only_resolved_scheduled_fixtures_inside_window(make_fixture) -> None:
    fixtures = [
        make_fixture(source_match_id=1, kickoff_at=isoformat_z(NOW + timedelta(minutes=29))),
        make_fixture(source_match_id=2, kickoff_at=isoformat_z(NOW + timedelta(minutes=30))),
        make_fixture(source_match_id=8, kickoff_at=isoformat_z(NOW + timedelta(minutes=30, seconds=1))),
        make_fixture(source_match_id=3, kickoff_at=isoformat_z(NOW + timedelta(hours=24))),
        make_fixture(source_match_id=4, kickoff_at=isoformat_z(NOW + timedelta(hours=24, seconds=1))),
        make_fixture(source_match_id=5, kickoff_at=isoformat_z(NOW + timedelta(hours=2)), status="postponed"),
        make_fixture(source_match_id=6, kickoff_at=isoformat_z(NOW + timedelta(hours=2)), home_team=None),
        make_fixture(source_match_id=7, kickoff_at="not-a-date"),
    ]

    jobs = due_prediction_jobs(
        fixtures,
        [contestant("alpha"), contestant("disabled", status="inactive")],
        [],
        NOW,
        RunnerConfig(),
    )

    assert [(fixture["match_id"], endpoint["id"]) for fixture, endpoint in jobs] == [
        ("fd-8", "alpha"),
        ("fd-3", "alpha"),
    ]


def test_due_jobs_retry_invalid_attempts_but_not_valid_predictions(make_fixture) -> None:
    fixture = make_fixture(kickoff_at=isoformat_z(NOW + timedelta(hours=2)))
    endpoints = [contestant("alpha"), contestant("bravo")]
    predictions = [
        {"contestant_id": "alpha", "match_id": fixture["match_id"], "valid": True},
        {"contestant_id": "bravo", "match_id": fixture["match_id"], "valid": False},
    ]

    jobs = due_prediction_jobs(fixtures=[fixture], registry=endpoints, predictions=predictions, now=NOW, config=RunnerConfig())

    assert [(row[0]["match_id"], row[1]["id"]) for row in jobs] == [("fd-1001", "bravo")]


def test_call_one_posts_exact_fixture_only_contract(make_fixture) -> None:
    fixture = make_fixture(kickoff_at=isoformat_z(NOW + timedelta(hours=2)))
    endpoint = contestant("alpha")
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"predicted_score_home": 2, "predicted_score_away": 1, "confidence": 0.8},
        )

    async def invoke() -> dict:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await runner._call_one(client, fixture, endpoint, RunnerConfig(retries=0))

    record = asyncio.run(invoke())

    assert requests == [prediction_request_payload(fixture)]
    assert "previous_results" not in requests[0]
    assert record["contestant_id"] == "alpha"
    assert record["match_id"] == "fd-1001"
    assert record["received_at"] is not None
    assert record["valid"] is True
    assert record["prediction"] == {
        "predicted_score_home": 2,
        "predicted_score_away": 1,
        "confidence": 0.8,
    }
    assert record["error"] is None


def test_call_one_rejects_response_received_at_fixture_lock(make_fixture, monkeypatch) -> None:
    lock_at = datetime(2026, 8, 15, 13, 30, tzinfo=UTC)
    fixture = make_fixture(kickoff_at="2026-08-15T14:00:00Z")
    monkeypatch.setattr(runner, "utc_now", lambda: lock_at)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"predicted_score_home": 2, "predicted_score_away": 1})

    async def invoke() -> dict:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await runner._call_one(client, fixture, contestant("alpha"), RunnerConfig(retries=2))

    record = asyncio.run(invoke())

    assert record["requested_at"] == "2026-08-15T13:30:00Z"
    assert record["received_at"] == "2026-08-15T13:30:00Z"
    assert record["valid"] is False
    assert record["prediction"] is None
    assert "arrived at or after the fixture lock" in record["error"]


def test_call_one_retries_transport_error_and_records_invalid_response(make_fixture) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("temporary", request=request)
        return httpx.Response(200, json={"predicted_score_home": -1, "predicted_score_away": 1})

    async def invoke() -> dict:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await runner._call_one(client, make_fixture(), contestant("alpha"), RunnerConfig(retries=1))

    record = asyncio.run(invoke())

    assert attempts == 2
    assert record["valid"] is False
    assert record["prediction"] is None
    assert record["error"].endswith("Predicted scores must be non-negative")


def test_merge_attempts_replaces_invalid_record_and_never_overwrites_valid_record() -> None:
    invalid = {"id": "old", "contestant_id": "alpha", "match_id": "fd-1", "valid": False}
    valid = {"id": "valid", "contestant_id": "alpha", "match_id": "fd-1", "valid": True}
    later = {"id": "later", "contestant_id": "alpha", "match_id": "fd-1", "valid": True}

    merged, recorded = runner._merge_prediction_attempts([invalid], [valid, later])

    assert merged == [valid]
    assert recorded == 1


def test_run_due_records_predictions_then_scores_completed_fixture(store, make_fixture, monkeypatch) -> None:
    fixture = make_fixture(kickoff_at=isoformat_z(NOW + timedelta(hours=2)))
    store.write("fixtures.json", [fixture])
    store.write("registry.json", [contestant("alpha")])

    async def fake_calls(jobs, config):
        if not jobs:
            return []
        assert [(row[0]["match_id"], row[1]["id"]) for row in jobs] == [("fd-1001", "alpha")]
        return [
            {
                "id": "attempt-1",
                "contestant_id": "alpha",
                "match_id": "fd-1001",
                "requested_at": isoformat_z(NOW),
                "valid": True,
                "prediction": {
                    "predicted_score_home": 2,
                    "predicted_score_away": 1,
                    "confidence": None,
                },
                "raw_response": {"predicted_score_home": 2, "predicted_score_away": 1},
                "error": None,
            }
        ]

    monkeypatch.setattr(runner, "_call_prediction_jobs", fake_calls)
    first = asyncio.run(run_due_once(store, RunnerConfig(), NOW))
    assert first["jobs_attempted"] == 1
    assert first["predictions_recorded"] == 1
    assert first["scores_added"] == 0

    completed = store.read("fixtures.json")[0]
    completed.update(
        status="completed",
        source_status="FINISHED",
        score_home=2,
        score_away=1,
        result="HOME_WIN",
        winner="Arsenal FC",
        result_source="football-data.org",
    )
    store.write("fixtures.json", [completed])

    second = asyncio.run(run_due_once(store, RunnerConfig(), NOW))
    assert second["jobs_attempted"] == 0
    assert second["scores_added"] == 1
    score = store.read("scores.json")[0]
    assert score | {"scored_at": "ignored"} == {
        "contestant_id": "alpha",
        "match_id": "fd-1001",
        "points": 1.5,
        "reason": "exact_score",
        "scored_at": "ignored",
    }
    assert len(store.read("run_log.json")) == 2


def test_source_sync_failure_does_not_prevent_due_workflow(store, make_fixture, monkeypatch) -> None:
    store.write("fixtures.json", [make_fixture(kickoff_at=isoformat_z(NOW + timedelta(hours=2)))])
    store.write("registry.json", [contestant("alpha")])

    async def broken_sync(*args, **kwargs):
        raise RuntimeError("rate limited")

    async def no_calls(jobs, config):
        return []

    monkeypatch.setattr(runner, "sync_matches_once", broken_sync)
    monkeypatch.setattr(runner, "_call_prediction_jobs", no_calls)

    entry = asyncio.run(run_due_once(store, RunnerConfig(sync_source=True), NOW))

    assert entry["source_error"] == "RuntimeError: rate limited"
    assert entry["jobs_attempted"] == 1
