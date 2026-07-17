from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from epl_tipping.storage import JsonStore


@pytest.fixture
def store(tmp_path: Path) -> JsonStore:
    result = JsonStore(tmp_path)
    result.ensure_defaults()
    return result


@pytest.fixture
def make_fixture() -> Callable[..., dict[str, Any]]:
    def factory(**overrides: Any) -> dict[str, Any]:
        source_id = overrides.pop("source_match_id", 1001)
        fixture = {
            "match_id": f"fd-{source_id}",
            "source": "football-data.org",
            "source_match_id": source_id,
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
        fixture.update(overrides)
        return fixture

    return factory
