from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from epl_tipping import simulation
from epl_tipping.models import prediction_request_payload
from epl_tipping.simulation import (
    SimulationError,
    SimulationConfig,
    enqueue_simulation,
    latest_simulation,
    latest_simulation_run,
    process_next_simulation,
    simulated_table,
    simulate_season,
)


CONTESTANT = {
    "id": "alpha",
    "name": "Alpha Model",
    "url": "https://alpha.example/predict",
    "status": "active",
}


def test_simulation_combines_actuals_with_fixture_only_predictions(make_fixture) -> None:
    fixtures = [
        make_fixture(
            source_match_id=1,
            status="completed",
            source_status="FINISHED",
            score_home=1,
            score_away=0,
            result="HOME_WIN",
            winner="Arsenal FC",
            result_source="football-data.org",
        ),
        make_fixture(
            source_match_id=2,
            matchday=2,
            kickoff_at="2026-08-22T14:00:00Z",
            status="in_progress",
            source_status="IN_PLAY",
            home_team_id=61,
            home_team="Chelsea FC",
            home_team_short_name="Chelsea",
            home_team_tla="CHE",
            away_team_id=64,
            away_team="Liverpool FC",
            away_team_short_name="Liverpool",
            away_team_tla="LIV",
        ),
        make_fixture(
            source_match_id=3,
            matchday=3,
            kickoff_at="2026-08-29T14:00:00Z",
            status="postponed",
            source_status="POSTPONED",
            home_team_id=64,
            home_team="Liverpool FC",
            home_team_short_name="Liverpool",
            home_team_tla="LIV",
            away_team_id=57,
            away_team="Arsenal FC",
            away_team_short_name="Arsenal",
            away_team_tla="ARS",
        ),
        make_fixture(source_match_id=4, status="cancelled", source_status="CANCELLED"),
        make_fixture(source_match_id=5, home_team=None),
    ]
    requests: list[dict] = []
    progress: list[tuple[int, int]] = []

    async def predict(payload: dict) -> dict:
        requests.append(payload)
        if payload["match_id"] == "fd-2":
            return {"predicted_score_home": 2, "predicted_score_away": 0, "confidence": 0.8}
        return {"predicted_score_home": 1, "predicted_score_away": 1, "confidence": 0.6}

    async def report_progress(processed: int, total: int) -> None:
        progress.append((processed, total))

    result = asyncio.run(
        simulate_season(
            CONTESTANT,
            fixtures,
            SimulationConfig(retries=0),
            predict,
            report_progress,
        )
    )

    expected_requests = [prediction_request_payload(fixtures[1]), prediction_request_payload(fixtures[2])]
    assert sorted(requests, key=lambda row: row["match_id"]) == expected_requests
    assert all("previous_results" not in payload for payload in requests)
    assert result["actual_match_count"] == 1
    assert result["predicted_match_count"] == 2
    assert [row["match_id"] for row in result["matches"]] == ["fd-1", "fd-2", "fd-3"]
    assert result["matches"][0]["actual_result"] is True
    assert result["matches"][1]["actual_result"] is False
    assert result["status"] == "completed_incomplete"
    assert result["source_fixture_count"] == 5
    assert result["omitted_match_count"] == 2
    assert result["omitted_matches"] == [
        {"match_id": "fd-4", "status": "cancelled", "reason": "unsupported_status"},
        {"match_id": "fd-5", "status": "scheduled", "reason": "unresolved_teams"},
    ]
    assert result["champion"] == "Arsenal"
    assert progress[0] == (3, 5)
    assert progress[-1] == (5, 5)


def test_invalid_simulation_response_uses_zero_zero_fallback(make_fixture) -> None:
    async def invalid(payload: dict) -> dict:
        return {"predicted_score_home": -1, "predicted_score_away": 0}

    result = asyncio.run(
        simulate_season(CONTESTANT, [make_fixture()], SimulationConfig(retries=0), invalid)
    )

    assert result["status"] == "completed_with_fallbacks"
    assert result["error_count"] == 1
    assert result["matches"][0] | {"error": "ignored"} == {
        "match_id": "fd-1001",
        "source_match_id": 1001,
        "matchday": 1,
        "kickoff_at": "2026-08-15T14:00:00Z",
        "home_team_id": 57,
        "home_team": "Arsenal FC",
        "home_team_short_name": "Arsenal",
        "away_team_id": 61,
        "away_team": "Chelsea FC",
        "away_team_short_name": "Chelsea",
        "score_home": 0,
        "score_away": 0,
        "result": "DRAW",
        "winner": None,
        "actual_result": False,
        "valid": False,
        "fallback_used": True,
        "error": "ignored",
        "confidence": None,
    }
    assert "non-negative" in result["matches"][0]["error"]


def test_simulated_table_calculates_points_goal_difference_and_tiebreaks() -> None:
    matches = [
        {
            "home_team_id": 1,
            "home_team": "Alpha FC",
            "home_team_short_name": "Alpha",
            "away_team_id": 2,
            "away_team": "Bravo FC",
            "away_team_short_name": "Bravo",
            "score_home": 2,
            "score_away": 0,
        },
        {
            "home_team_id": 2,
            "home_team": "Bravo FC",
            "home_team_short_name": "Bravo",
            "away_team_id": 3,
            "away_team": "Charlie FC",
            "away_team_short_name": "Charlie",
            "score_home": 1,
            "score_away": 1,
        },
        {
            "home_team_id": 3,
            "home_team": "Charlie FC",
            "home_team_short_name": "Charlie",
            "away_team_id": 1,
            "away_team": "Alpha FC",
            "away_team_short_name": "Alpha",
            "score_home": 1,
            "score_away": 0,
        },
    ]

    table = simulated_table(matches)

    assert [row["team"] for row in table] == ["Charlie", "Alpha", "Bravo"]
    assert [row["position"] for row in table] == [1, 2, 3]
    assert table[0] == {
        "team_id": 3,
        "team": "Charlie",
        "played": 2,
        "won": 1,
        "drawn": 1,
        "lost": 0,
        "goals_for": 2,
        "goals_against": 1,
        "goal_difference": 1,
        "points": 4,
        "position": 1,
    }
    assert table[1]["points"] == 3
    assert table[2]["points"] == 1


def test_simulation_queue_rejects_unknown_inactive_duplicate_and_daily_repeat(store, make_fixture, monkeypatch) -> None:
    monkeypatch.setenv("TIPPING_COMPETITION_TIMEZONE", "UTC")
    store.write("fixtures.json", [make_fixture(), make_fixture(source_match_id=2)])
    store.write(
        "registry.json",
        [CONTESTANT, {"id": "inactive", "name": "Inactive", "url": "https://example.test", "status": "inactive"}],
    )
    now = datetime(2026, 8, 15, 10, tzinfo=UTC)

    with pytest.raises(SimulationError, match="not found"):
        enqueue_simulation("unknown", store=store, now=now)
    with pytest.raises(SimulationError, match="not active"):
        enqueue_simulation("inactive", store=store, now=now)

    queued = enqueue_simulation("alpha", store=store, now=now)
    assert queued["status"] == "queued"
    assert queued["total"] == 2
    with pytest.raises(SimulationError, match="already queued or running"):
        enqueue_simulation("alpha", store=store, now=now)

    runs = store.read("simulation_runs.json")
    runs[0]["status"] = "completed"
    store.write("simulation_runs.json", runs)
    with pytest.raises(SimulationError, match="already been requested today"):
        enqueue_simulation("alpha", store=store, now=now)

    admin_run = enqueue_simulation(
        "alpha",
        store=store,
        requested_by="admin",
        enforce_daily_limit=False,
        now=now,
    )
    assert admin_run["requested_by"] == "admin"


def test_process_next_simulation_completes_run_and_persists_simulation(store, make_fixture) -> None:
    store.write("fixtures.json", [make_fixture()])
    store.write("registry.json", [CONTESTANT])
    run = enqueue_simulation("alpha", store=store, enforce_daily_limit=False)
    seen: list[dict] = []

    async def predict(payload: dict) -> dict:
        seen.append(payload)
        return {"predicted_score_home": 3, "predicted_score_away": 1}

    completed = asyncio.run(process_next_simulation(store, SimulationConfig(retries=0), predict))

    assert seen == [prediction_request_payload(make_fixture())]
    assert completed["id"] == run["id"]
    assert completed["status"] == "completed"
    assert completed["processed"] == 1
    assert completed["simulation_id"]
    simulations = store.read("season_simulations.json")
    assert len(simulations) == 1
    assert simulations[0]["run_id"] == run["id"]
    assert simulations[0]["matches"][0]["score_home"] == 3
    assert asyncio.run(process_next_simulation(store, prediction_client=predict)) is None


def test_process_next_simulation_marks_unhandled_failure(store, make_fixture, monkeypatch) -> None:
    store.write("fixtures.json", [make_fixture()])
    store.write("registry.json", [CONTESTANT])
    run = enqueue_simulation("alpha", store=store, enforce_daily_limit=False)

    async def broken_simulation(*args, **kwargs):
        raise RuntimeError("simulation exploded")

    monkeypatch.setattr(simulation, "simulate_season", broken_simulation)
    failed = asyncio.run(process_next_simulation(store))

    assert failed["id"] == run["id"]
    assert failed["status"] == "failed"
    assert failed["error"] == "RuntimeError: simulation exploded"
    assert store.read("season_simulations.json") == []


def test_latest_simulation_helpers_select_newest_per_contestant() -> None:
    simulations = [
        {"id": "old", "contestant_id": "alpha", "simulated_at": "2026-08-01T00:00:00Z"},
        {"id": "other", "contestant_id": "bravo", "simulated_at": "2026-09-01T00:00:00Z"},
        {"id": "new", "contestant_id": "alpha", "simulated_at": "2026-08-02T00:00:00Z"},
    ]
    runs = [
        {"id": "first", "contestant_id": "alpha", "requested_at": "2026-08-01T00:00:00Z"},
        {"id": "second", "contestant_id": "alpha", "requested_at": "2026-08-03T00:00:00Z"},
    ]

    assert latest_simulation(simulations, "alpha")["id"] == "new"
    assert latest_simulation(simulations, "missing") is None
    assert latest_simulation_run(runs, "alpha")["id"] == "second"
    assert latest_simulation_run(runs, "missing") is None
