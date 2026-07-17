from __future__ import annotations

import asyncio
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

import httpx

from .models import (
    RESULT_AWAY_WIN,
    RESULT_DRAW,
    RESULT_HOME_WIN,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_IN_PROGRESS,
    STATUS_POSTPONED,
    STATUS_SCHEDULED,
    STATUS_SUSPENDED,
    fixture_sort_key,
    isoformat_z,
    result_key,
    utc_now,
    winner_from_score,
)
from .storage import JsonStore, get_store


FOOTBALL_DATA_BASE_URL = "https://api.football-data.org/v4"
FOOTBALL_DATA_SOURCE = "football-data.org"

SOURCE_STATUS_MAP = {
    "SCHEDULED": STATUS_SCHEDULED,
    "TIMED": STATUS_SCHEDULED,
    "IN_PLAY": STATUS_IN_PROGRESS,
    "PAUSED": STATUS_IN_PROGRESS,
    "EXTRA_TIME": STATUS_IN_PROGRESS,
    "PENALTY_SHOOTOUT": STATUS_IN_PROGRESS,
    "FINISHED": STATUS_COMPLETED,
    "AWARDED": STATUS_COMPLETED,
    "POSTPONED": STATUS_POSTPONED,
    "SUSPENDED": STATUS_SUSPENDED,
    "CANCELLED": STATUS_CANCELLED,
}


@dataclass(frozen=True)
class FootballDataConfig:
    token: str
    base_url: str = FOOTBALL_DATA_BASE_URL
    competition_code: str = "PL"
    season: int = 2026
    timeout_seconds: float = 15.0
    retries: int = 2

    @classmethod
    def from_env(cls) -> "FootballDataConfig":
        token = os.getenv("FOOTBALL_DATA_TOKEN") or os.getenv("FOOTBALL_DATA_API_TOKEN") or ""
        return cls(
            token=token.strip(),
            base_url=os.getenv("FOOTBALL_DATA_BASE_URL", FOOTBALL_DATA_BASE_URL).rstrip("/"),
            competition_code=os.getenv("TIPPING_COMPETITION_CODE", "PL").strip() or "PL",
            season=int(os.getenv("TIPPING_SEASON", "2026")),
            timeout_seconds=float(os.getenv("FOOTBALL_DATA_TIMEOUT_SECONDS", "15")),
            retries=int(os.getenv("FOOTBALL_DATA_RETRIES", "2")),
        )


@dataclass(frozen=True)
class FootballDataResponse:
    payload: dict[str, Any]
    requests_available: str | None = None
    request_counter_reset: str | None = None


class MatchSource(Protocol):
    async def fetch_matches(self) -> FootballDataResponse:
        raise NotImplementedError


class FootballDataClient:
    def __init__(self, config: FootballDataConfig | None = None) -> None:
        self.config = config or FootballDataConfig.from_env()

    async def fetch_matches(self) -> FootballDataResponse:
        if not self.config.token:
            raise RuntimeError("FOOTBALL_DATA_TOKEN is required")

        headers = {
            "X-Auth-Token": self.config.token,
            "User-Agent": "epl-tipping/1.0",
        }
        timeout = httpx.Timeout(self.config.timeout_seconds)
        last_error: Exception | None = None
        async with httpx.AsyncClient(
            base_url=self.config.base_url,
            headers=headers,
            timeout=timeout,
        ) as client:
            for attempt in range(self.config.retries + 1):
                try:
                    response = await client.get(
                        f"/competitions/{self.config.competition_code}/matches",
                        params={"season": str(self.config.season)},
                    )
                    if response.status_code == 429 and attempt < self.config.retries:
                        await asyncio.sleep(_retry_delay(response, attempt))
                        continue
                    response.raise_for_status()
                    payload = response.json()
                    if not isinstance(payload, dict):
                        raise ValueError("football-data.org returned a non-object response")
                    return FootballDataResponse(
                        payload=payload,
                        requests_available=response.headers.get("X-RequestsAvailable"),
                        request_counter_reset=response.headers.get("X-RequestCounter-Reset"),
                    )
                except (httpx.HTTPError, ValueError) as exc:
                    last_error = exc
                    if attempt >= self.config.retries:
                        break
                    await asyncio.sleep(min(2**attempt, 4))
        assert last_error is not None
        raise last_error


@dataclass
class SyncReport:
    source: str = FOOTBALL_DATA_SOURCE
    competition_code: str = "PL"
    season: int = 2026
    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    missing_from_source: int = 0
    score_invalidations: int = 0
    changed_match_ids: list[str] = field(default_factory=list)
    requests_available: str | None = None
    request_counter_reset: str | None = None
    status_counts: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


async def sync_matches_once(
    store: JsonStore | None = None,
    source: MatchSource | None = None,
    config: FootballDataConfig | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    store = store or get_store()
    config = config or FootballDataConfig.from_env()
    source = source or FootballDataClient(config)
    attempted_at = isoformat_z(utc_now())

    try:
        response = await source.fetch_matches()
        _validate_response(response.payload, config)
        rows = response.payload.get("matches") or []
        mapped = [map_source_match(row, config) for row in rows]
        _validate_unique_source_ids(mapped)
    except Exception as exc:
        _record_sync_failure(store, config, attempted_at, exc, dry_run=dry_run)
        raise

    report = SyncReport(
        competition_code=config.competition_code,
        season=config.season,
        fetched=len(mapped),
        requests_available=response.requests_available,
        request_counter_reset=response.request_counter_reset,
    )
    for fixture in mapped:
        status = str(fixture["status"])
        report.status_counts[status] = report.status_counts.get(status, 0) + 1

    try:
        with store.locked():
            existing_fixtures = store.read("fixtures.json")
            scores = store.read("scores.json")
            existing_by_source_id = {
                fixture.get("source_match_id"): fixture
                for fixture in existing_fixtures
                if fixture.get("source_match_id") is not None
            }
            merged: list[dict[str, Any]] = []
            source_ids = {int(fixture["source_match_id"]) for fixture in mapped}
            invalidated_match_ids: set[str] = set()

            for fixture in mapped:
                source_id = int(fixture["source_match_id"])
                existing = existing_by_source_id.get(source_id)
                if existing is None:
                    merged.append(fixture)
                    report.inserted += 1
                    report.changed_match_ids.append(fixture["match_id"])
                    continue

                candidate = dict(fixture)
                if existing.get("result_source") == "manual":
                    for field in ["status", "score_home", "score_away", "result", "winner", "result_source"]:
                        candidate[field] = existing.get(field)

                if _score_signature(existing) != _score_signature(candidate):
                    invalidated_match_ids.add(candidate["match_id"])

                if candidate == existing:
                    report.unchanged += 1
                else:
                    report.updated += 1
                    report.changed_match_ids.append(candidate["match_id"])
                merged.append(candidate)

            missing = [
                fixture
                for fixture in existing_fixtures
                if fixture.get("source_match_id") not in source_ids
            ]
            report.missing_from_source = len(missing)
            merged.extend(missing)
            merged.sort(key=fixture_sort_key)

            filtered_scores = [score for score in scores if score.get("match_id") not in invalidated_match_ids]
            report.score_invalidations = len(scores) - len(filtered_scores)

            state = {
                "source": FOOTBALL_DATA_SOURCE,
                "competition_code": config.competition_code,
                "season": config.season,
                "last_attempted_at": attempted_at,
                "last_successful_at": attempted_at,
                "last_error": None,
                "fixture_count": len(mapped),
                "requests_available": response.requests_available,
                "request_counter_reset": response.request_counter_reset,
                "last_report": {
                    "inserted": report.inserted,
                    "updated": report.updated,
                    "unchanged": report.unchanged,
                    "missing_from_source": report.missing_from_source,
                    "score_invalidations": report.score_invalidations,
                    "status_counts": report.status_counts,
                },
            }

            if not dry_run:
                store.write("fixtures.json", merged)
                if report.score_invalidations:
                    store.write("scores.json", filtered_scores)
                store.write("source_state.json", state)
    except Exception as exc:
        _record_sync_failure(store, config, attempted_at, exc, dry_run=dry_run)
        raise

    return report.as_dict()


def map_source_match(row: dict[str, Any], config: FootballDataConfig) -> dict[str, Any]:
    source_id = _required_int(row.get("id"), "match id")
    source_status = str(row.get("status") or "SCHEDULED").upper()
    if source_status not in SOURCE_STATUS_MAP:
        raise ValueError(f"Unsupported football-data.org match status: {source_status}")

    home = row.get("homeTeam") if isinstance(row.get("homeTeam"), dict) else {}
    away = row.get("awayTeam") if isinstance(row.get("awayTeam"), dict) else {}
    score = row.get("score") if isinstance(row.get("score"), dict) else {}
    full_time = score.get("fullTime") if isinstance(score.get("fullTime"), dict) else {}
    source_score_home = _optional_int(full_time.get("home"))
    source_score_away = _optional_int(full_time.get("away"))
    status = SOURCE_STATUS_MAP[source_status]
    if status == STATUS_COMPLETED and (source_score_home is None or source_score_away is None):
        status = STATUS_IN_PROGRESS

    is_final = status == STATUS_COMPLETED
    score_home = source_score_home if is_final else None
    score_away = source_score_away if is_final else None
    result = result_key(score_home, score_away) if is_final else None
    if result is not None:
        _validate_source_winner(score.get("winner"), result)

    return {
        "match_id": f"fd-{source_id}",
        "source": FOOTBALL_DATA_SOURCE,
        "source_match_id": source_id,
        "source_last_updated_at": row.get("lastUpdated"),
        "competition_code": config.competition_code,
        "season": config.season,
        "stage": "regular_season",
        "matchday": _optional_int(row.get("matchday")),
        "kickoff_at": row.get("utcDate"),
        "source_status": source_status,
        "status": status,
        "home_team_id": _optional_int(home.get("id")),
        "home_team": home.get("name"),
        "home_team_short_name": home.get("shortName"),
        "home_team_tla": home.get("tla"),
        "away_team_id": _optional_int(away.get("id")),
        "away_team": away.get("name"),
        "away_team_short_name": away.get("shortName"),
        "away_team_tla": away.get("tla"),
        "score_home": score_home,
        "score_away": score_away,
        "result": result,
        "winner": (
            winner_from_score(home.get("name"), away.get("name"), score_home, score_away)
            if score_home is not None and score_away is not None
            else None
        ),
        "result_source": FOOTBALL_DATA_SOURCE if status == STATUS_COMPLETED else None,
    }


def _validate_response(payload: dict[str, Any], config: FootballDataConfig) -> None:
    competition = payload.get("competition") if isinstance(payload.get("competition"), dict) else {}
    code = competition.get("code")
    if code and str(code) != config.competition_code:
        raise ValueError(f"Expected competition {config.competition_code}, received {code}")
    filters = payload.get("filters") if isinstance(payload.get("filters"), dict) else {}
    season = filters.get("season")
    if season is not None and int(season) != config.season:
        raise ValueError(f"Expected season {config.season}, received {season}")
    if not isinstance(payload.get("matches"), list):
        raise ValueError("football-data.org response is missing matches")


def _validate_unique_source_ids(fixtures: list[dict[str, Any]]) -> None:
    seen: set[int] = set()
    for fixture in fixtures:
        source_id = int(fixture["source_match_id"])
        if source_id in seen:
            raise ValueError(f"Duplicate football-data.org match id: {source_id}")
        seen.add(source_id)


def _record_sync_failure(
    store: JsonStore,
    config: FootballDataConfig,
    attempted_at: str,
    exc: Exception,
    *,
    dry_run: bool,
) -> None:
    if dry_run:
        return
    with store.locked():
        state = store.read("source_state.json")
        state.update(
            {
                "source": FOOTBALL_DATA_SOURCE,
                "competition_code": config.competition_code,
                "season": config.season,
                "last_attempted_at": attempted_at,
                "last_error": f"{type(exc).__name__}: {exc}",
            }
        )
        store.write("source_state.json", state)


def _validate_source_winner(source_winner: Any, result: str) -> None:
    expected = {
        RESULT_HOME_WIN: "HOME_TEAM",
        RESULT_AWAY_WIN: "AWAY_TEAM",
        RESULT_DRAW: "DRAW",
    }[result]
    if source_winner is not None and str(source_winner) != expected:
        raise ValueError(f"Source winner {source_winner} conflicts with full-time score")


def _score_signature(fixture: dict[str, Any]) -> tuple[Any, ...]:
    return (
        fixture.get("status"),
        fixture.get("score_home"),
        fixture.get("score_away"),
        fixture.get("result"),
        fixture.get("winner"),
        fixture.get("result_source"),
    )


def _retry_delay(response: httpx.Response, attempt: int) -> float:
    value = response.headers.get("X-RequestCounter-Reset") or response.headers.get("Retry-After")
    try:
        return min(max(float(value), 0.2), 30.0)
    except (TypeError, ValueError):
        return min(2**attempt, 4)


def _required_int(value: Any, label: str) -> int:
    parsed = _optional_int(value)
    if parsed is None:
        raise ValueError(f"Missing or invalid {label}")
    return parsed


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None
