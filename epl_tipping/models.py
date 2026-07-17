from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any


STATUS_SCHEDULED = "scheduled"
STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETED = "completed"
STATUS_POSTPONED = "postponed"
STATUS_SUSPENDED = "suspended"
STATUS_CANCELLED = "cancelled"

PREDICTABLE_STATUSES = {STATUS_SCHEDULED}
SIMULATABLE_STATUSES = {
    STATUS_SCHEDULED,
    STATUS_IN_PROGRESS,
    STATUS_POSTPONED,
    STATUS_SUSPENDED,
}

RESULT_HOME_WIN = "HOME_WIN"
RESULT_AWAY_WIN = "AWAY_WIN"
RESULT_DRAW = "DRAW"


def utc_now() -> datetime:
    return datetime.now(UTC)


def isoformat_z(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_iso_z(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def result_key(score_home: int, score_away: int) -> str:
    if score_home > score_away:
        return RESULT_HOME_WIN
    if score_away > score_home:
        return RESULT_AWAY_WIN
    return RESULT_DRAW


def winner_from_score(home_team: str | None, away_team: str | None, score_home: int, score_away: int) -> str | None:
    result = result_key(score_home, score_away)
    if result == RESULT_HOME_WIN:
        return home_team
    if result == RESULT_AWAY_WIN:
        return away_team
    return None


def display_team(fixture: dict[str, Any], side: str) -> str:
    value = fixture.get(f"{side}_team_short_name") or fixture.get(f"{side}_team")
    return str(value) if value else "TBD"


def is_resolved_fixture(fixture: dict[str, Any]) -> bool:
    return bool(fixture.get("home_team") and fixture.get("away_team"))


def is_completed_fixture(fixture: dict[str, Any]) -> bool:
    return (
        fixture.get("status") == STATUS_COMPLETED
        and fixture.get("score_home") is not None
        and fixture.get("score_away") is not None
    )


def fixture_sort_key(fixture: dict[str, Any]) -> tuple[str, int, int]:
    kickoff = str(fixture.get("kickoff_at") or "9999-12-31T23:59:59Z")
    matchday = _int_value(fixture.get("matchday"), 999)
    source_id = _int_value(fixture.get("source_match_id"), 2**31 - 1)
    return kickoff, matchday, source_id


def prediction_lock_at(fixture: dict[str, Any], lock_minutes: int = 30) -> datetime | None:
    kickoff = fixture.get("kickoff_at")
    if not kickoff:
        return None
    try:
        return parse_iso_z(str(kickoff)) - timedelta(minutes=lock_minutes)
    except ValueError:
        return None


def prediction_is_public(
    fixture: dict[str, Any],
    *,
    now: datetime | None = None,
    lock_minutes: int = 30,
) -> bool:
    if is_completed_fixture(fixture):
        return True
    lock_at = prediction_lock_at(fixture, lock_minutes)
    if lock_at is None:
        return False
    return (now or utc_now()).astimezone(UTC) >= lock_at


def team_payload(fixture: dict[str, Any], side: str) -> dict[str, Any]:
    return {
        "id": fixture.get(f"{side}_team_id"),
        "name": fixture.get(f"{side}_team"),
        "short_name": fixture.get(f"{side}_team_short_name"),
        "tla": fixture.get(f"{side}_team_tla"),
    }


def prediction_request_payload(fixture: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "competition": fixture.get("competition_code", "PL"),
        "season": fixture.get("season"),
        "match_id": fixture["match_id"],
        "source_match_id": fixture.get("source_match_id"),
        "matchday": fixture.get("matchday"),
        "kickoff_at": fixture.get("kickoff_at"),
        "home_team": team_payload(fixture, "home"),
        "away_team": team_payload(fixture, "away"),
    }


def _int_value(value: Any, fallback: int) -> int:
    if isinstance(value, bool):
        return fallback
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback
