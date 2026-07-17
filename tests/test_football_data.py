from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from epl_tipping.football_data import (
    FootballDataConfig,
    FootballDataResponse,
    map_source_match,
    sync_matches_once,
)


CONFIG = FootballDataConfig(token="test-token", competition_code="PL", season=2026)


def source_match(
    source_id: int = 1001,
    *,
    status: str = "TIMED",
    score_home: int | None = None,
    score_away: int | None = None,
    source_winner: str | None = None,
    kickoff_at: str = "2026-08-15T14:00:00Z",
    updated_at: str = "2026-07-01T12:00:00Z",
) -> dict[str, Any]:
    return {
        "id": source_id,
        "utcDate": kickoff_at,
        "status": status,
        "matchday": 1,
        "lastUpdated": updated_at,
        "homeTeam": {"id": 57, "name": "Arsenal FC", "shortName": "Arsenal", "tla": "ARS"},
        "awayTeam": {"id": 61, "name": "Chelsea FC", "shortName": "Chelsea", "tla": "CHE"},
        "score": {
            "winner": source_winner,
            "fullTime": {"home": score_home, "away": score_away},
        },
    }


@dataclass
class FakeSource:
    matches: list[dict[str, Any]]
    requests_available: str | None = "9"

    async def fetch_matches(self) -> FootballDataResponse:
        return FootballDataResponse(
            payload={
                "competition": {"code": "PL"},
                "filters": {"season": "2026"},
                "matches": self.matches,
            },
            requests_available=self.requests_available,
            request_counter_reset="60",
        )


def test_maps_scheduled_match_to_canonical_epl_fixture() -> None:
    fixture = map_source_match(source_match(), CONFIG)

    assert fixture == {
        "match_id": "fd-1001",
        "source": "football-data.org",
        "source_match_id": 1001,
        "source_last_updated_at": "2026-07-01T12:00:00Z",
        "competition_code": "PL",
        "season": 2026,
        "stage": "regular_season",
        "matchday": 1,
        "kickoff_at": "2026-08-15T14:00:00Z",
        "source_status": "TIMED",
        "status": "scheduled",
        "home_team_id": 57,
        "home_team": "Arsenal FC",
        "home_team_short_name": "Arsenal",
        "home_team_tla": "ARS",
        "away_team_id": 61,
        "away_team": "Chelsea FC",
        "away_team_short_name": "Chelsea",
        "away_team_tla": "CHE",
        "score_home": None,
        "score_away": None,
        "result": None,
        "winner": None,
        "result_source": None,
    }


@pytest.mark.parametrize(
    ("score_home", "score_away", "source_winner", "result", "winner"),
    [
        (2, 1, "HOME_TEAM", "HOME_WIN", "Arsenal FC"),
        (0, 2, "AWAY_TEAM", "AWAY_WIN", "Chelsea FC"),
        (1, 1, "DRAW", "DRAW", None),
    ],
)
def test_maps_completed_scores_and_result(
    score_home: int,
    score_away: int,
    source_winner: str,
    result: str,
    winner: str | None,
) -> None:
    fixture = map_source_match(
        source_match(
            status="FINISHED",
            score_home=score_home,
            score_away=score_away,
            source_winner=source_winner,
        ),
        CONFIG,
    )

    assert fixture["status"] == "completed"
    assert fixture["result"] == result
    assert fixture["winner"] == winner
    assert fixture["result_source"] == "football-data.org"


def test_finished_match_without_full_time_score_remains_in_progress() -> None:
    fixture = map_source_match(source_match(status="FINISHED"), CONFIG)
    assert fixture["status"] == "in_progress"
    assert fixture["result_source"] is None


def test_in_play_running_score_is_not_exposed_as_a_final_result() -> None:
    fixture = map_source_match(
        source_match(status="IN_PLAY", score_home=2, score_away=1, source_winner="HOME_TEAM"),
        CONFIG,
    )

    assert fixture["status"] == "in_progress"
    assert fixture["score_home"] is None
    assert fixture["score_away"] is None
    assert fixture["result"] is None
    assert fixture["winner"] is None
    assert fixture["result_source"] is None


def test_mapping_rejects_unknown_status_invalid_id_and_conflicting_winner() -> None:
    with pytest.raises(ValueError, match="Unsupported"):
        map_source_match(source_match(status="UNKNOWN"), CONFIG)
    with pytest.raises(ValueError, match="match id"):
        map_source_match(source_match() | {"id": True}, CONFIG)
    with pytest.raises(ValueError, match="conflicts"):
        map_source_match(
            source_match(status="FINISHED", score_home=2, score_away=1, source_winner="AWAY_TEAM"),
            CONFIG,
        )


def test_sync_inserts_and_sorts_matches_and_records_source_state(store) -> None:
    report = asyncio.run(
        sync_matches_once(
            store,
            FakeSource(
                [
                    source_match(1002, kickoff_at="2026-08-16T14:00:00Z"),
                    source_match(1001, kickoff_at="2026-08-15T14:00:00Z"),
                ]
            ),
            CONFIG,
        )
    )

    assert report["fetched"] == 2
    assert report["inserted"] == 2
    assert report["changed_match_ids"] == ["fd-1002", "fd-1001"]
    assert [row["match_id"] for row in store.read("fixtures.json")] == ["fd-1001", "fd-1002"]
    assert store.read("source_state.json") | {"last_attempted_at": "ignored", "last_successful_at": "ignored"} == {
        "source": "football-data.org",
        "competition_code": "PL",
        "season": 2026,
        "last_attempted_at": "ignored",
        "last_successful_at": "ignored",
        "last_error": None,
        "fixture_count": 2,
        "requests_available": "9",
        "request_counter_reset": "60",
        "last_report": {
            "inserted": 2,
            "updated": 0,
            "unchanged": 0,
            "missing_from_source": 0,
            "score_invalidations": 0,
            "status_counts": {"scheduled": 2},
        },
    }


def test_sync_updates_changed_result_and_invalidates_derived_scores(store) -> None:
    original = map_source_match(
        source_match(status="FINISHED", score_home=2, score_away=1, source_winner="HOME_TEAM"),
        CONFIG,
    )
    store.write("fixtures.json", [original])
    store.write(
        "scores.json",
        [{"contestant_id": "alpha", "match_id": "fd-1001", "points": 1.5, "reason": "exact_score"}],
    )

    report = asyncio.run(
        sync_matches_once(
            store,
            FakeSource(
                [
                    source_match(
                        status="FINISHED",
                        score_home=3,
                        score_away=1,
                        source_winner="HOME_TEAM",
                        updated_at="2026-08-15T16:00:00Z",
                    )
                ]
            ),
            CONFIG,
        )
    )

    assert report["updated"] == 1
    assert report["score_invalidations"] == 1
    assert store.read("fixtures.json")[0]["score_home"] == 3
    assert store.read("scores.json") == []


def test_sync_preserves_manual_result_and_keeps_source_missing_local_fixture(store, make_fixture) -> None:
    manual = map_source_match(source_match(status="TIMED"), CONFIG)
    manual.update(
        status="completed",
        score_home=4,
        score_away=0,
        result="HOME_WIN",
        winner="Arsenal FC",
        result_source="manual",
    )
    local = make_fixture(source_match_id=None, match_id="local-friendly", kickoff_at="2026-08-14T14:00:00Z")
    store.write("fixtures.json", [manual, local])

    report = asyncio.run(sync_matches_once(store, FakeSource([source_match(status="TIMED")]), CONFIG))
    fixtures = {row["match_id"]: row for row in store.read("fixtures.json")}

    assert report["missing_from_source"] == 1
    assert fixtures["fd-1001"]["score_home"] == 4
    assert fixtures["fd-1001"]["result_source"] == "manual"
    assert fixtures["local-friendly"] == local


def test_failed_sync_records_error_without_mutating_fixtures(store, make_fixture) -> None:
    class BrokenSource:
        async def fetch_matches(self) -> FootballDataResponse:
            raise RuntimeError("source unavailable")

    store.write("fixtures.json", [make_fixture()])

    with pytest.raises(RuntimeError, match="unavailable"):
        asyncio.run(sync_matches_once(store, BrokenSource(), CONFIG))

    assert store.read("fixtures.json") == [make_fixture()]
    state = store.read("source_state.json")
    assert state["last_error"] == "RuntimeError: source unavailable"
    assert state["competition_code"] == "PL"


def test_dry_run_reports_changes_without_writing(store) -> None:
    report = asyncio.run(sync_matches_once(store, FakeSource([source_match()]), CONFIG, dry_run=True))
    assert report["inserted"] == 1
    assert store.read("fixtures.json") == []
    assert store.read("source_state.json") == {}
