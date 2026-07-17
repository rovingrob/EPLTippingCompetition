from __future__ import annotations

from datetime import UTC, datetime

from epl_tipping.models import (
    display_team,
    fixture_sort_key,
    is_completed_fixture,
    is_resolved_fixture,
    isoformat_z,
    parse_iso_z,
    prediction_is_public,
    prediction_lock_at,
    prediction_request_payload,
    result_key,
    winner_from_score,
)


def test_utc_helpers_normalize_naive_and_offset_datetimes() -> None:
    assert isoformat_z(datetime(2026, 8, 15, 14, 30)) == "2026-08-15T14:30:00Z"
    assert parse_iso_z("2026-08-16T00:00:00+10:00") == datetime(2026, 8, 15, 14, tzinfo=UTC)


def test_result_and_winner_helpers_cover_home_away_and_draw() -> None:
    assert result_key(2, 1) == "HOME_WIN"
    assert result_key(0, 3) == "AWAY_WIN"
    assert result_key(1, 1) == "DRAW"
    assert winner_from_score("Arsenal FC", "Chelsea FC", 2, 1) == "Arsenal FC"
    assert winner_from_score("Arsenal FC", "Chelsea FC", 0, 1) == "Chelsea FC"
    assert winner_from_score("Arsenal FC", "Chelsea FC", 1, 1) is None


def test_fixture_helpers_use_short_names_and_require_teams_and_final_score(make_fixture) -> None:
    fixture = make_fixture()
    assert display_team(fixture, "home") == "Arsenal"
    assert is_resolved_fixture(fixture) is True
    assert is_completed_fixture(fixture) is False

    fixture.update(status="completed", score_home=0, score_away=0)
    assert is_completed_fixture(fixture) is True
    assert is_resolved_fixture(make_fixture(home_team=None)) is False
    assert display_team(make_fixture(home_team=None, home_team_short_name=None), "home") == "TBD"


def test_fixture_sort_key_prefers_kickoff_then_matchday_then_source_id(make_fixture) -> None:
    fixtures = [
        make_fixture(source_match_id=3, kickoff_at=None, matchday=1),
        make_fixture(source_match_id=2, kickoff_at="2026-08-15T14:00:00Z", matchday=2),
        make_fixture(source_match_id=1, kickoff_at="2026-08-15T14:00:00Z", matchday=1),
    ]
    assert [row["source_match_id"] for row in sorted(fixtures, key=fixture_sort_key)] == [1, 2, 3]


def test_prediction_lock_is_thirty_minutes_before_kickoff(make_fixture) -> None:
    fixture = make_fixture(kickoff_at="2026-08-15T14:00:00Z")
    assert prediction_lock_at(fixture) == datetime(2026, 8, 15, 13, 30, tzinfo=UTC)
    assert prediction_is_public(fixture, now=datetime(2026, 8, 15, 13, 29, 59, tzinfo=UTC)) is False
    assert prediction_is_public(fixture, now=datetime(2026, 8, 15, 13, 30, tzinfo=UTC)) is True
    assert prediction_lock_at(make_fixture(kickoff_at="bad")) is None


def test_prediction_payload_is_fixture_only_contract(make_fixture) -> None:
    payload = prediction_request_payload(make_fixture())

    assert payload == {
        "schema_version": 1,
        "competition": "PL",
        "season": 2026,
        "match_id": "fd-1001",
        "source_match_id": 1001,
        "matchday": 1,
        "kickoff_at": "2026-08-15T14:00:00Z",
        "home_team": {"id": 57, "name": "Arsenal FC", "short_name": "Arsenal", "tla": "ARS"},
        "away_team": {"id": 61, "name": "Chelsea FC", "short_name": "Chelsea", "tla": "CHE"},
    }
    assert "previous_results" not in payload
