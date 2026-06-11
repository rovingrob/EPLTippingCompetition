from __future__ import annotations

from datetime import UTC, datetime, timedelta

from world_cup_tipping.models import isoformat_z
from world_cup_tipping.runner import RunnerConfig, due_prediction_jobs


def fixture(match_id: str, kickoff_at: datetime) -> dict:
    return {
        "match_id": match_id,
        "match_number": int(match_id.rsplit("-", maxsplit=1)[1]),
        "stage": "group",
        "group": "A",
        "team_a": "Mexico",
        "team_b": "South Africa",
        "team_a_placeholder": None,
        "team_b_placeholder": None,
        "kickoff_at": isoformat_z(kickoff_at),
        "score_a": None,
        "score_b": None,
        "winner": None,
        "status": "scheduled",
    }


def test_default_runner_lookahead_checks_fixtures_24_hours_in_advance() -> None:
    now = datetime(2026, 6, 11, 0, 0, tzinfo=UTC)
    registry = [{"id": "active-bot", "url": "http://example.com/predict", "status": "active"}]
    fixtures = [
        fixture("2026-001", now + timedelta(hours=23)),
        fixture("2026-002", now + timedelta(hours=25)),
    ]

    jobs = due_prediction_jobs(fixtures, registry, [], now, RunnerConfig())

    assert [(job_fixture["match_id"], contestant["id"]) for job_fixture, contestant in jobs] == [
        ("2026-001", "active-bot")
    ]
