from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Cookie, FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .football_data import FootballDataConfig, SOURCE_STATUS_MAP, sync_matches_once
from .models import (
    STATUS_COMPLETED,
    STATUS_IN_PROGRESS,
    STATUS_SCHEDULED,
    display_team,
    fixture_sort_key,
    is_completed_fixture,
    isoformat_z,
    parse_iso_z,
    prediction_is_public,
    prediction_lock_at,
    prediction_request_payload,
    result_key,
    utc_now,
    winner_from_score,
)
from .runner import RunnerConfig, run_due_once
from .scoring import leaderboard, leaderboard_snake, validate_prediction
from .simulation import (
    SimulationError,
    enqueue_simulation,
    latest_simulation,
    latest_simulation_run,
)
from .storage import get_store


PACKAGE_DIR = Path(__file__).resolve().parent
BASE_PATH = "/tipping"
DEFAULT_COMPETITION_TIMEZONE = "Australia/Sydney"
DEFAULT_LOCK_MINUTES = 30
LEADERBOARD_PAGE_SIZE = 10

app = FastAPI(title="EPL Tipping Competition")


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def allowed_hosts() -> list[str]:
    return [host.strip() for host in os.getenv("TIPPING_ALLOWED_HOSTS", "").split(",") if host.strip()]


if allowed_hosts():
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts())


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'; "
        "object-src 'none'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        "connect-src 'self' http: https:",
    )
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    if env_bool("TIPPING_ENABLE_HSTS"):
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains; preload")
    return response


app.mount(f"{BASE_PATH}/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
router = APIRouter(prefix=BASE_PATH)
templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")


def app_path(path: str = "/") -> str:
    normalized = path if path.startswith("/") else f"/{path}"
    return f"{BASE_PATH}/" if normalized == "/" else f"{BASE_PATH}{normalized}"


def is_active_path(request: Request, path: str) -> bool:
    return request.url.path.rstrip("/") == app_path(path).rstrip("/")


templates.env.globals.update(
    display_team=display_team,
    is_completed_fixture=is_completed_fixture,
    app_path=app_path,
    is_active_path=is_active_path,
)


def admin_token() -> str | None:
    value = os.getenv("ADMIN_TOKEN", "").strip()
    return value or None


def admin_cookie_secret() -> str | None:
    value = os.getenv("ADMIN_COOKIE_SECRET", "").strip()
    return value or None


def admin_auth_configured() -> bool:
    token = admin_token()
    secret = admin_cookie_secret()
    return bool(token and secret and not hmac.compare_digest(token, secret))


def admin_cookie_ttl_seconds() -> int:
    return int(os.getenv("ADMIN_COOKIE_TTL_SECONDS", "86400"))


def admin_cookie_secure() -> bool:
    return env_bool("ADMIN_COOKIE_SECURE")


def admin_token_hash() -> str:
    token = admin_token()
    if token is None:
        raise RuntimeError("ADMIN_TOKEN is not configured")
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def admin_cookie_cipher() -> Fernet:
    secret = admin_cookie_secret()
    if secret is None:
        raise RuntimeError("ADMIN_COOKIE_SECRET is not configured")
    key = hashlib.sha256(secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_admin_cookie() -> str:
    if not admin_auth_configured():
        raise RuntimeError("Admin authentication is not configured")
    payload = json.dumps({"admin": True, "token_hash": admin_token_hash()}, separators=(",", ":")).encode()
    return admin_cookie_cipher().encrypt(payload).decode("ascii")


def decrypt_admin_cookie(cookie_value: str | None) -> dict[str, Any] | None:
    if not cookie_value:
        return None
    try:
        decrypted = admin_cookie_cipher().decrypt(cookie_value.encode("ascii"), ttl=admin_cookie_ttl_seconds())
        payload = json.loads(decrypted.decode("utf-8"))
    except (InvalidToken, ValueError, json.JSONDecodeError, UnicodeDecodeError, RuntimeError):
        return None
    if payload.get("admin") is not True or payload.get("token_hash") != admin_token_hash():
        return None
    return payload


def is_admin(request: Request, cookie: str | None = None) -> bool:
    return decrypt_admin_cookie(cookie or request.cookies.get("admin_session")) is not None


def require_admin(request: Request, cookie: str | None = None) -> None:
    if not is_admin(request, cookie):
        raise HTTPException(status_code=401, detail="Admin login required")


def redirect_to_admin(message: str | None = None) -> RedirectResponse:
    suffix = f"?message={quote(message)}" if message else ""
    return RedirectResponse(f"{app_path('/admin')}{suffix}", status_code=303)


def competition_timezone() -> ZoneInfo:
    name = (
        os.getenv("TIPPING_COMPETITION_TIMEZONE", DEFAULT_COMPETITION_TIMEZONE).strip()
        or DEFAULT_COMPETITION_TIMEZONE
    )
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def user_timezone(name: str | None) -> ZoneInfo:
    candidate = (name or "").strip()
    if not candidate or len(candidate) > 100:
        return competition_timezone()
    try:
        return ZoneInfo(candidate)
    except (ValueError, ZoneInfoNotFoundError):
        return competition_timezone()


def load_context(request: Request) -> dict[str, Any]:
    store = get_store()
    with store.locked():
        fixtures = sorted(store.read("fixtures.json"), key=fixture_sort_key)
        registry = store.read("registry.json")
        predictions = store.read("predictions.json")
        scores = store.read("scores.json")
        simulations = store.read("season_simulations.json")
        simulation_runs = store.read("simulation_runs.json")
        source_state = store.read("source_state.json")
        run_log = list(reversed(store.read("run_log.json")[-10:]))
    admin = is_admin(request)
    season = int(source_state.get("season") or os.getenv("TIPPING_SEASON", "2026"))
    current_leaderboard = leaderboard(registry, scores)
    return {
        "request": request,
        "fixtures": fixtures,
        "registry": registry,
        "predictions": predictions,
        "scores": scores,
        "leaderboard": current_leaderboard,
        "leaderboard_snake": leaderboard_snake(registry, fixtures, scores),
        "summary": schedule_summary(fixtures),
        "source_state": source_state,
        "season": season,
        "season_label": f"{season}/{str(season + 1)[-2:]}",
        "run_log": run_log,
        "latest_simulations": {row["id"]: latest_simulation(simulations, row["id"]) for row in registry},
        "latest_simulation_runs": {row["id"]: latest_simulation_run(simulation_runs, row["id"]) for row in registry},
        "is_admin": admin,
        "admin_auth_configured": admin_auth_configured(),
        "message": request.query_params.get("message"),
        "competition_timezone": getattr(competition_timezone(), "key", "UTC"),
    }


def schedule_summary(fixtures: list[dict[str, Any]]) -> dict[str, int]:
    completed = sum(1 for fixture in fixtures if is_completed_fixture(fixture))
    return {
        "total": len(fixtures),
        "completed": completed,
        "remaining": len(fixtures) - completed,
        "postponed": sum(1 for fixture in fixtures if fixture.get("status") == "postponed"),
    }


def fixture_prediction_rows(
    fixtures: list[dict[str, Any]],
    registry: list[dict[str, Any]],
    predictions: list[dict[str, Any]],
    scores: list[dict[str, Any]],
    admin: bool,
) -> dict[str, list[dict[str, Any]]]:
    prediction_by_key = {(row["contestant_id"], row["match_id"]): row for row in predictions}
    score_by_key = {(row["contestant_id"], row["match_id"]): row for row in scores}
    rows: dict[str, list[dict[str, Any]]] = {}
    for fixture in fixtures:
        visible = admin or prediction_is_public(fixture, lock_minutes=DEFAULT_LOCK_MINUTES)
        fixture_rows = []
        for contestant in registry:
            key = (contestant["id"], fixture["match_id"])
            record = prediction_by_key.get(key) if visible else None
            fixture_rows.append(
                {
                    "contestant": contestant,
                    "prediction_record": record,
                    "prediction": record.get("prediction") if record else None,
                    "score": score_by_key.get(key),
                    "hidden": not visible,
                }
            )
        rows[fixture["match_id"]] = fixture_rows
    return rows


def fixture_prediction_insights(
    fixture: dict[str, Any],
    prediction_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    hidden = bool(prediction_rows and prediction_rows[0]["hidden"])
    total_contestants = len(prediction_rows)
    submitted_count = sum(1 for row in prediction_rows if row["prediction_record"] is not None)
    visible_predictions = [
        row
        for row in prediction_rows
        if not row["hidden"]
        and row["prediction_record"] is not None
        and row["prediction_record"].get("valid")
        and row["prediction"] is not None
    ]

    outcome_counts = {"home": 0, "draw": 0, "away": 0}
    scoreline_counts: Counter[tuple[int, int]] = Counter()
    confidence_values: list[float] = []
    for row in visible_predictions:
        prediction = row["prediction"]
        home_score = int(prediction["predicted_score_home"])
        away_score = int(prediction["predicted_score_away"])
        scoreline_counts[(home_score, away_score)] += 1
        if home_score > away_score:
            outcome_counts["home"] += 1
        elif away_score > home_score:
            outcome_counts["away"] += 1
        else:
            outcome_counts["draw"] += 1
        confidence = prediction.get("confidence")
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool):
            confidence_values.append(float(confidence))

    valid_count = len(visible_predictions)
    home_name = display_team(fixture, "home")
    away_name = display_team(fixture, "away")
    outcome_labels = {
        "home": home_name,
        "draw": "Draw",
        "away": away_name,
    }
    outcome_order = ["home", "draw", "away"]
    consensus_key = max(outcome_order, key=lambda key: (outcome_counts[key], -outcome_order.index(key)))
    consensus_count = outcome_counts[consensus_key]
    consensus_percent = round(consensus_count * 100 / valid_count) if valid_count else 0
    consensus_value = outcome_labels[consensus_key] if valid_count else "No data"
    consensus_detail = (
        f"{consensus_percent}% picked {'draw' if consensus_key == 'draw' else 'win'}"
        if valid_count
        else "No valid predictions"
    )

    most_scoreline = None
    most_scoreline_count = 0
    if scoreline_counts:
        most_scoreline, most_scoreline_count = min(
            scoreline_counts.items(),
            key=lambda item: (-item[1], item[0][0] + item[0][1], item[0]),
        )

    average_confidence = round(sum(confidence_values) * 100 / len(confidence_values)) if confidence_values else None
    exact_count = sum(1 for row in prediction_rows if (row.get("score") or {}).get("reason") == "exact_score")
    correct_count = sum(
        1
        for row in prediction_rows
        if (row.get("score") or {}).get("reason") in {"exact_score", "correct_result"}
    )
    points_awarded = sum(float((row.get("score") or {}).get("points") or 0) for row in prediction_rows)
    completed = is_completed_fixture(fixture)
    actual_outcome = None
    if completed:
        actual_home = int(fixture["score_home"])
        actual_away = int(fixture["score_away"])
        actual_outcome = "home" if actual_home > actual_away else "away" if actual_away > actual_home else "draw"

    outcomes = []
    for key in outcome_order:
        count = outcome_counts[key]
        outcomes.append(
            {
                "key": key,
                "label": outcome_labels[key],
                "count": count,
                "percent": round(count * 100 / valid_count) if valid_count else 0,
                "is_actual": key == actual_outcome,
            }
        )

    bucket_labels = ["0", "1", "2", "3", "4+"]
    bucket_counts = [[0 for _ in bucket_labels] for _ in bucket_labels]
    for (home_score, away_score), count in scoreline_counts.items():
        bucket_counts[min(home_score, 4)][min(away_score, 4)] += count
    maximum_bucket = max((count for row in bucket_counts for count in row), default=0)
    actual_bucket = (
        (min(int(fixture["score_home"]), 4), min(int(fixture["score_away"]), 4))
        if completed
        else None
    )
    heatmap_rows = []
    for home_index, row_counts in enumerate(bucket_counts):
        cells = []
        for away_index, count in enumerate(row_counts):
            if count == 0 or maximum_bucket == 0:
                level = 0
            elif count == maximum_bucket:
                level = 4
            else:
                ratio = count / maximum_bucket
                level = 3 if ratio >= 0.6 else 2 if ratio >= 0.35 else 1
            cells.append(
                {
                    "count": count,
                    "level": level,
                    "is_actual": actual_bucket == (home_index, away_index),
                }
            )
        heatmap_rows.append({"label": bucket_labels[home_index], "cells": cells})

    return {
        "hidden": hidden,
        "total_contestants": total_contestants,
        "submitted_count": submitted_count,
        "valid_count": valid_count,
        "predictions_detail": f"{submitted_count} of {total_contestants} submitted",
        "consensus_value": consensus_value,
        "consensus_detail": consensus_detail,
        "most_scoreline": f"{most_scoreline[0]}–{most_scoreline[1]}" if most_scoreline else "—",
        "most_scoreline_detail": (
            f"{most_scoreline_count} {'bot' if most_scoreline_count == 1 else 'bots'}"
            if most_scoreline
            else "No valid predictions"
        ),
        "average_confidence": f"{average_confidence}%" if average_confidence is not None else "—",
        "average_confidence_detail": (
            f"across {len(confidence_values)} {'bot' if len(confidence_values) == 1 else 'bots'}"
            if confidence_values
            else "No confidence values"
        ),
        "correct_count": str(correct_count) if completed else "—",
        "correct_detail": f"incl. {exact_count} exact" if completed else "Awaiting result",
        "exact_count": exact_count,
        "points_awarded": points_awarded,
        "actual_result_label": (
            f"Actual result: {home_name} {fixture['score_home']}–{fixture['score_away']} · {points_awarded:.1f} pts awarded"
            if completed
            else "Actual result pending"
        ),
        "outcomes": outcomes,
        "heatmap_labels": bucket_labels,
        "heatmap_rows": heatmap_rows,
    }


def fixture_prediction_sort_key(row: dict[str, Any]) -> tuple[float, int, float, str]:
    score = row.get("score") or {}
    prediction = row.get("prediction") or {}
    points = float(score.get("points") or 0)
    submitted = 1 if row.get("prediction_record") is not None else 0
    confidence = prediction.get("confidence")
    confidence_value = float(confidence) if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) else -1
    return (-points, -submitted, -confidence_value, str(row["contestant"].get("name") or "").casefold())


def contestant_rows(
    contestant_id: str,
    fixtures: list[dict[str, Any]],
    predictions: list[dict[str, Any]],
    scores: list[dict[str, Any]],
    admin: bool,
) -> list[dict[str, Any]]:
    prediction_by_match = {row["match_id"]: row for row in predictions if row.get("contestant_id") == contestant_id}
    score_by_match = {row["match_id"]: row for row in scores if row.get("contestant_id") == contestant_id}
    rows = []
    for fixture in fixtures:
        visible = admin or prediction_is_public(fixture, lock_minutes=DEFAULT_LOCK_MINUTES)
        record = prediction_by_match.get(fixture["match_id"]) if visible else None
        rows.append(
            {
                "fixture": fixture,
                "prediction_record": record,
                "prediction": record.get("prediction") if record else None,
                "score": score_by_match.get(fixture["match_id"]),
                "hidden": not visible,
            }
        )
    return rows


def fixtures_for_date(
    fixtures: list[dict[str, Any]],
    target: date,
    timezone: ZoneInfo,
) -> list[dict[str, Any]]:
    result = []
    for fixture in fixtures:
        kickoff = fixture.get("kickoff_at")
        if not kickoff:
            continue
        try:
            if parse_iso_z(str(kickoff)).astimezone(timezone).date() == target:
                result.append(fixture)
        except ValueError:
            continue
    return sorted(result, key=fixture_sort_key)


@app.get("/")
def root_redirect():
    return RedirectResponse(app_path("/"), status_code=307)


@app.get("/favicon.ico")
def root_favicon():
    return FileResponse(PACKAGE_DIR / "static" / "favicon.svg", media_type="image/svg+xml")


@router.get("/")
def schedule_page(request: Request):
    return templates.TemplateResponse(request, "schedule.html", load_context(request))


@router.get("/today")
def today_page(
    request: Request,
    selected_date: str | None = Query(default=None, alias="date"),
    timezone_name: str | None = Query(default=None, alias="tz"),
):
    context = load_context(request)
    timezone = user_timezone(timezone_name)
    today = utc_now().astimezone(timezone).date()
    try:
        target = date.fromisoformat(selected_date) if selected_date else today
    except ValueError:
        target = today
    fixtures = fixtures_for_date(context["fixtures"], target, timezone)
    context.update(
        today_games={
            "date": target.isoformat(),
            "date_label": f"{target.day} {target.strftime('%B %Y')}",
            "is_today": target == today,
            "previous_date": (target - timedelta(days=1)).isoformat(),
            "next_date": (target + timedelta(days=1)).isoformat(),
            "timezone": getattr(timezone, "key", "UTC"),
            "fixtures": fixtures,
            "matchday": next((fixture.get("matchday") for fixture in fixtures if fixture.get("matchday")), None),
        },
    )
    return templates.TemplateResponse(request, "today.html", context)


@router.get("/favicon.ico")
def prefixed_favicon():
    return root_favicon()


@router.get("/fixtures/{match_id}")
def fixture_page(request: Request, match_id: str):
    context = load_context(request)
    fixture = next((row for row in context["fixtures"] if row.get("match_id") == match_id), None)
    if fixture is None:
        raise HTTPException(status_code=404, detail="Fixture not found")
    prediction_rows = fixture_prediction_rows(
        [fixture],
        context["registry"],
        context["predictions"],
        context["scores"],
        context["is_admin"],
    )[match_id]
    insights = fixture_prediction_insights(fixture, prediction_rows)
    if not insights["hidden"]:
        prediction_rows.sort(key=fixture_prediction_sort_key)
    context.update(fixture=fixture, prediction_rows=prediction_rows, fixture_insights=insights)
    return templates.TemplateResponse(request, "fixture.html", context)


@router.get("/leaderboard")
def leaderboard_page(request: Request, page: int = Query(1, ge=1)):
    context = load_context(request)
    rows = context["leaderboard"]
    total_items = len(rows)
    page_count = max(1, (total_items + LEADERBOARD_PAGE_SIZE - 1) // LEADERBOARD_PAGE_SIZE)
    current_page = min(page, page_count)
    offset = (current_page - 1) * LEADERBOARD_PAGE_SIZE
    context.update(
        leaderboard_page=rows[offset : offset + LEADERBOARD_PAGE_SIZE],
        leaderboard_pagination={
            "current_page": current_page,
            "first_item": offset + 1 if total_items else 0,
            "last_item": min(offset + LEADERBOARD_PAGE_SIZE, total_items),
            "next_page": current_page + 1 if current_page < page_count else None,
            "offset": offset,
            "page_count": page_count,
            "pages": list(range(1, page_count + 1)),
            "previous_page": current_page - 1 if current_page > 1 else None,
            "total_items": total_items,
        },
    )
    return templates.TemplateResponse(request, "leaderboard.html", context)


@router.get("/leaderboard/{contestant_id}")
def contestant_page(request: Request, contestant_id: str):
    context = load_context(request)
    contestant = next((row for row in context["registry"] if row.get("id") == contestant_id), None)
    if contestant is None:
        raise HTTPException(status_code=404, detail="Contestant not found")
    context.update(
        contestant=contestant,
        prediction_rows=contestant_rows(
            contestant_id,
            context["fixtures"],
            context["predictions"],
            context["scores"],
            context["is_admin"],
        ),
    )
    return templates.TemplateResponse(request, "contestant.html", context)


@router.get("/leaderboard/{contestant_id}/tips")
def contestant_tips_page(request: Request, contestant_id: str):
    context = load_context(request)
    contestant = next((row for row in context["registry"] if row.get("id") == contestant_id), None)
    if contestant is None:
        raise HTTPException(status_code=404, detail="Contestant not found")
    context.update(
        contestant=contestant,
        tip_rows=contestant_rows(
            contestant_id,
            context["fixtures"],
            context["predictions"],
            context["scores"],
            context["is_admin"],
        ),
    )
    return templates.TemplateResponse(request, "tips.html", context)


@router.get("/leaderboard/{contestant_id}/simulation")
def contestant_simulation_page(request: Request, contestant_id: str):
    context = load_context(request)
    contestant = next((row for row in context["registry"] if row.get("id") == contestant_id), None)
    if contestant is None:
        raise HTTPException(status_code=404, detail="Contestant not found")
    context.update(
        contestant=contestant,
        simulation=context["latest_simulations"].get(contestant_id),
        simulation_run=context["latest_simulation_runs"].get(contestant_id),
    )
    return templates.TemplateResponse(request, "simulation.html", context)


@router.get("/leaderboard/{contestant_id}/api-test")
def contestant_api_test_page(request: Request, contestant_id: str):
    require_admin(request)
    context = load_context(request)
    contestant = next((row for row in context["registry"] if row.get("id") == contestant_id), None)
    if contestant is None:
        raise HTTPException(status_code=404, detail="Contestant not found")
    fixture = endpoint_validation_fixture(context["fixtures"])
    context.update(
        contestant=contestant,
        payload_text=json.dumps(prediction_request_payload(fixture), indent=2),
    )
    return templates.TemplateResponse(request, "api_test.html", context)


@router.get("/admin")
def admin_page(request: Request):
    return templates.TemplateResponse(request, "admin.html", load_context(request))


@router.post("/admin/login")
def login(token: str = Form(...)):
    configured_token = admin_token()
    if not admin_auth_configured() or configured_token is None:
        return redirect_to_admin("Admin access is not configured")
    if not hmac.compare_digest(token, configured_token):
        return redirect_to_admin("Invalid token")
    response = redirect_to_admin("Signed in")
    response.set_cookie(
        "admin_session",
        encrypt_admin_cookie(),
        max_age=admin_cookie_ttl_seconds(),
        httponly=True,
        secure=admin_cookie_secure(),
        samesite="strict",
        path=BASE_PATH,
    )
    return response


@router.post("/admin/logout")
def logout():
    response = redirect_to_admin("Signed out")
    response.delete_cookie("admin_session", path=BASE_PATH)
    return response


@router.post("/admin/sync")
async def admin_sync(
    request: Request,
    dry_run: bool = Form(False),
    admin_session: str | None = Cookie(default=None),
):
    require_admin(request, admin_session)
    try:
        report = await sync_matches_once(
            get_store(),
            config=FootballDataConfig.from_env(),
            dry_run=dry_run,
        )
    except Exception as exc:
        return redirect_to_admin(f"Sync failed: {type(exc).__name__}: {exc}")
    label = "Dry run complete" if dry_run else "Sync complete"
    return redirect_to_admin(
        f"{label}: {report['inserted']} inserted, {report['updated']} updated, "
        f"{report['score_invalidations']} corrected scores, {report['fetched']} fetched"
    )


@router.post("/admin/run-due")
async def admin_run_due(
    request: Request,
    admin_session: str | None = Cookie(default=None),
):
    require_admin(request, admin_session)
    result = await run_due_once(get_store(), RunnerConfig(sync_source=False))
    return redirect_to_admin(f"Due run complete: {result['jobs_attempted']} jobs, {result['scores_added']} scores")


@router.post("/admin/results")
def save_manual_result(
    request: Request,
    match_id: str = Form(...),
    score_home: int = Form(...),
    score_away: int = Form(...),
    admin_session: str | None = Cookie(default=None),
):
    require_admin(request, admin_session)
    if score_home < 0 or score_away < 0:
        return redirect_to_admin("Scores must be non-negative")
    store = get_store()
    with store.locked():
        fixtures = store.read("fixtures.json")
        fixture = next((row for row in fixtures if row.get("match_id") == match_id), None)
        if fixture is None:
            return redirect_to_admin("Fixture not found")
        fixture.update(
            {
                "score_home": score_home,
                "score_away": score_away,
                "result": result_key(score_home, score_away),
                "winner": winner_from_score(
                    fixture.get("home_team"),
                    fixture.get("away_team"),
                    score_home,
                    score_away,
                ),
                "status": STATUS_COMPLETED,
                "result_source": "manual",
            }
        )
        scores = [row for row in store.read("scores.json") if row.get("match_id") != match_id]
        store.write("fixtures.json", fixtures)
        store.write("scores.json", scores)
    return redirect_to_admin("Manual result saved; run due workflow to rescore")


@router.post("/admin/results/clear")
def clear_manual_result(
    request: Request,
    match_id: str = Form(...),
    admin_session: str | None = Cookie(default=None),
):
    require_admin(request, admin_session)
    store = get_store()
    with store.locked():
        fixtures = store.read("fixtures.json")
        fixture = next((row for row in fixtures if row.get("match_id") == match_id), None)
        if fixture is None:
            return redirect_to_admin("Fixture not found")
        if fixture.get("result_source") != "manual":
            return redirect_to_admin("Fixture has no manual override")
        fixture.update(
            {
                "score_home": None,
                "score_away": None,
                "result": None,
                "winner": None,
                "status": source_status_after_clearing_override(fixture),
                "result_source": None,
            }
        )
        scores = [row for row in store.read("scores.json") if row.get("match_id") != match_id]
        store.write("fixtures.json", fixtures)
        store.write("scores.json", scores)
    return redirect_to_admin("Manual override cleared; sync to restore source result")


@router.post("/admin/predictions/reopen")
def reopen_predictions(
    request: Request,
    match_id: str = Form(...),
    contestant_id: str = Form(""),
    admin_session: str | None = Cookie(default=None),
):
    require_admin(request, admin_session)
    store = get_store()
    with store.locked():
        fixtures = store.read("fixtures.json")
        if not any(row.get("match_id") == match_id for row in fixtures):
            return redirect_to_admin("Fixture not found")
        predictions = store.read("predictions.json")
        scores = store.read("scores.json")
        run_log = store.read("run_log.json")

        def matches(row: dict[str, Any]) -> bool:
            return row.get("match_id") == match_id and (
                not contestant_id or row.get("contestant_id") == contestant_id
            )

        kept_predictions = [row for row in predictions if not matches(row)]
        kept_scores = [row for row in scores if not matches(row)]
        removed_predictions = len(predictions) - len(kept_predictions)
        removed_scores = len(scores) - len(kept_scores)
        run_log.append(
            {
                "id": str(uuid4()),
                "ran_at": isoformat_z(utc_now()),
                "action": "predictions_reopened",
                "match_id": match_id,
                "contestant_id": contestant_id or None,
                "predictions_removed": removed_predictions,
                "scores_removed": removed_scores,
            }
        )
        store.write("predictions.json", kept_predictions)
        store.write("scores.json", kept_scores)
        store.write("run_log.json", run_log[-200:])
    scope = contestant_id or "all contestants"
    return redirect_to_admin(f"Reopened {match_id} for {scope}; removed {removed_predictions} prediction(s)")


@router.post("/admin/endpoints")
async def save_endpoint(
    request: Request,
    name: str = Form(...),
    url: str = Form(...),
    contact: str = Form(""),
    status: str = Form("active"),
    contestant_id: str = Form(""),
    admin_session: str | None = Cookie(default=None),
):
    require_admin(request, admin_session)
    if status not in {"active", "inactive"}:
        return redirect_to_admin("Invalid endpoint status")
    store = get_store()
    fixtures = store.read("fixtures.json")
    validation = await validate_endpoint(url, fixtures)
    with store.locked():
        registry = store.read("registry.json")
        existing = next((row for row in registry if row.get("id") == contestant_id), None) if contestant_id else None
        if existing is None:
            existing = {"id": new_contestant_id(name, registry)}
            registry.append(existing)
        existing.update(
            {
                "name": name.strip(),
                "url": url.strip(),
                "contact": contact.strip(),
                "status": status if validation["valid"] else "inactive",
                "last_check_status": "valid" if validation["valid"] else "invalid",
                "last_check_error": validation["error"],
                "last_checked_at": isoformat_z(utc_now()),
            }
        )
        store.write("registry.json", registry)
    message = "Endpoint saved" if validation["valid"] else f"Endpoint saved inactive: {validation['error']}"
    return redirect_to_admin(message)


@router.post("/admin/clear")
def clear_admin_data(
    request: Request,
    section: str = Form(...),
    admin_session: str | None = Cookie(default=None),
):
    require_admin(request, admin_session)
    mapping = {
        "workflow": ["predictions.json", "scores.json", "run_log.json"],
        "endpoints": ["registry.json"],
        "simulations": ["season_simulations.json", "simulation_runs.json"],
    }
    filenames = mapping.get(section)
    if filenames is None:
        return redirect_to_admin("Unknown clear section")
    store = get_store()
    with store.locked():
        for filename in filenames:
            store.write(filename, [])
    return redirect_to_admin(f"Cleared {section}")


@router.post("/simulations/run")
def request_simulation(request: Request, contestant_id: str = Form(...)):
    try:
        enqueue_simulation(
            contestant_id,
            store=get_store(),
            requested_by="admin" if is_admin(request) else "public",
            enforce_daily_limit=not is_admin(request),
        )
        message = "Season simulation queued"
    except SimulationError as exc:
        message = str(exc)
    return RedirectResponse(
        f"{app_path('/leaderboard/' + contestant_id + '/simulation')}?message={quote(message)}",
        status_code=303,
    )


@router.get("/schedule.json")
def schedule_download():
    fixtures = sorted(get_store().read("fixtures.json"), key=fixture_sort_key)
    return JSONResponse(
        fixtures,
        headers={"Content-Disposition": 'attachment; filename="epl-schedule.json"'},
    )


@router.get("/api/fixtures")
def api_fixtures():
    return sorted(get_store().read("fixtures.json"), key=fixture_sort_key)


@router.get("/api/leaderboard")
def api_leaderboard():
    store = get_store()
    with store.locked():
        registry = store.read("registry.json")
        scores = store.read("scores.json")
    return leaderboard(registry, scores)


@router.get("/healthz")
def healthz():
    store = get_store()
    with store.locked():
        fixtures = store.read("fixtures.json")
        registry = store.read("registry.json")
        source_state = store.read("source_state.json")
    return {
        "status": "ok",
        "fixtures": len(fixtures),
        "completed": sum(1 for fixture in fixtures if is_completed_fixture(fixture)),
        "active_endpoints": sum(1 for row in registry if row.get("status", "active") == "active"),
        "last_source_sync": source_state.get("last_successful_at"),
    }


def endpoint_validation_fixture(fixtures: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = [fixture for fixture in fixtures if fixture.get("status") == STATUS_SCHEDULED]
    if candidates:
        return sorted(candidates, key=fixture_sort_key)[0]
    return {
        "match_id": "test-1",
        "source_match_id": 1,
        "competition_code": "PL",
        "season": int(os.getenv("TIPPING_SEASON", "2026")),
        "matchday": 1,
        "kickoff_at": isoformat_z(utc_now()),
        "home_team_id": 1,
        "home_team": "Home FC",
        "home_team_short_name": "Home",
        "home_team_tla": "HOM",
        "away_team_id": 2,
        "away_team": "Away FC",
        "away_team_short_name": "Away",
        "away_team_tla": "AWY",
        "status": STATUS_SCHEDULED,
    }


def source_status_after_clearing_override(fixture: dict[str, Any]) -> str:
    status = SOURCE_STATUS_MAP.get(str(fixture.get("source_status") or "").upper(), STATUS_SCHEDULED)
    if status == STATUS_COMPLETED:
        return STATUS_IN_PROGRESS
    return status


async def validate_endpoint(url: str, fixtures: list[dict[str, Any]]) -> dict[str, Any]:
    if not re.match(r"^https?://", url.strip()):
        return {"valid": False, "error": "URL must start with http:// or https://"}
    fixture = endpoint_validation_fixture(fixtures)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            response = await client.post(url.strip(), json=prediction_request_payload(fixture))
            response.raise_for_status()
            payload = response.json()
        valid, _prediction, error = validate_prediction(fixture, payload)
        return {"valid": valid, "error": error}
    except Exception as exc:
        return {"valid": False, "error": f"{type(exc).__name__}: {exc}"}


def new_contestant_id(name: str, registry: list[dict[str, Any]]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-") or "contestant"
    existing = {row.get("id") for row in registry}
    candidate = base
    index = 2
    while candidate in existing:
        candidate = f"{base}-{index}"
        index += 1
    return candidate


app.include_router(router)
