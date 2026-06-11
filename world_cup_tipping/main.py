from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

import httpx
from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Cookie, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .import_schedule import save_imported_schedule
from .models import (
    STAGE_LABELS,
    completed_results,
    display_team,
    is_completed_fixture,
    is_resolved_fixture,
    parse_iso_z,
    utc_now,
    winner_from_score,
)
from .runner import RunnerConfig, run_due_once
from .scoring import leaderboard, validate_prediction
from .simulation import SimulationConfig, simulate_contestant
from .storage import PROJECT_ROOT, get_store


PACKAGE_DIR = Path(__file__).resolve().parent
BASE_PATH = "/tipping"
app = FastAPI(title="World Cup Tipping")


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def allowed_hosts() -> list[str]:
    return [host.strip() for host in os.getenv("WCT_ALLOWED_HOSTS", "").split(",") if host.strip()]


if allowed_hosts():
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts())


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("Content-Security-Policy", "default-src 'self'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'; object-src 'none'; img-src 'self' data:; style-src 'self'; script-src 'self'; connect-src 'self' http: https:")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    if env_bool("WCT_ENABLE_HSTS"):
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains; preload")
    return response


app.mount(f"{BASE_PATH}/static", StaticFiles(directory=PACKAGE_DIR / "static"), name="static")
router = APIRouter(prefix=BASE_PATH)
templates = Jinja2Templates(directory=PACKAGE_DIR / "templates")


def stage_label(stage: str) -> str:
    return STAGE_LABELS.get(stage, stage.replace("_", " ").title())


def app_path(path: str = "/") -> str:
    normalized = path if path.startswith("/") else f"/{path}"
    if normalized == "/":
        return f"{BASE_PATH}/"
    return f"{BASE_PATH}{normalized}"


def is_active_path(request: Request, path: str) -> bool:
    return request.url.path.rstrip("/") == app_path(path).rstrip("/")


templates.env.filters["stage_label"] = stage_label
templates.env.globals["display_team"] = display_team
templates.env.globals["is_completed_fixture"] = is_completed_fixture
templates.env.globals["app_path"] = app_path
templates.env.globals["is_active_path"] = is_active_path


def admin_token() -> str:
    return os.getenv("ADMIN_TOKEN", "admin")


def admin_cookie_secret() -> str:
    return os.getenv("ADMIN_COOKIE_SECRET") or admin_token()


def admin_cookie_ttl_seconds() -> int:
    return int(os.getenv("ADMIN_COOKIE_TTL_SECONDS", "86400"))


def admin_cookie_secure() -> bool:
    return env_bool("ADMIN_COOKIE_SECURE")


def admin_cookie_path() -> str:
    return BASE_PATH


def admin_token_hash() -> str:
    return hashlib.sha256(admin_token().encode("utf-8")).hexdigest()


def admin_cookie_cipher() -> Fernet:
    key = hashlib.sha256(admin_cookie_secret().encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_admin_cookie() -> str:
    payload = json.dumps({"admin": True, "token_hash": admin_token_hash()}, separators=(",", ":")).encode("utf-8")
    return admin_cookie_cipher().encrypt(payload).decode("ascii")


def decrypt_admin_cookie(cookie_value: str | None) -> dict[str, Any] | None:
    if not cookie_value:
        return None
    try:
        decrypted = admin_cookie_cipher().decrypt(cookie_value.encode("ascii"), ttl=admin_cookie_ttl_seconds())
        payload = json.loads(decrypted.decode("utf-8"))
    except (InvalidToken, ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if payload.get("admin") is not True or payload.get("token_hash") != admin_token_hash():
        return None
    return payload


def is_admin(request: Request, admin_session_cookie: str | None = None) -> bool:
    cookie_value = admin_session_cookie or request.cookies.get("admin_session")
    return decrypt_admin_cookie(cookie_value) is not None


def require_admin(request: Request, admin_session_cookie: str | None = None) -> None:
    if not is_admin(request, admin_session_cookie):
        raise HTTPException(status_code=401, detail="Admin token required")


def redirect_to_admin(message: str | None = None) -> RedirectResponse:
    suffix = f"?message={quote(message)}" if message else ""
    return RedirectResponse(f"{app_path('/admin')}{suffix}", status_code=303)


def load_context(request: Request) -> dict[str, Any]:
    store = get_store()
    fixtures = sorted(store.read("fixtures.json"), key=lambda item: item["match_number"])
    groups = store.read("groups.json")
    registry = store.read("registry.json")
    predictions = store.read("predictions.json")
    scores = store.read("scores.json")
    simulations = store.read("simulations.json")
    run_log = list(reversed(store.read("run_log.json")[-10:]))
    return {
        "request": request,
        "fixtures": fixtures,
        "groups": groups,
        "registry": registry,
        "predictions": predictions,
        "scores": scores,
        "simulations": simulations,
        "latest_simulations": {
            contestant["id"]: latest_simulation(simulations, contestant["id"])
            for contestant in registry
        },
        "fixture_prediction_rows": fixture_prediction_rows_by_match(fixtures, registry, predictions, scores),
        "leaderboard": leaderboard(registry, scores),
        "summary": schedule_summary(fixtures),
        "run_log": run_log,
        "is_admin": is_admin(request),
        "message": request.query_params.get("message"),
    }


def schedule_summary(fixtures: list[dict[str, Any]]) -> dict[str, int]:
    completed = sum(1 for fixture in fixtures if is_completed_fixture(fixture))
    scheduled = len(fixtures) - completed
    return {"total": len(fixtures), "completed": completed, "scheduled": scheduled}


def score_lookup(scores: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(score["contestant_id"], score["match_id"]): score for score in scores}


def prediction_lookup(predictions: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(prediction["contestant_id"], prediction["match_id"]): prediction for prediction in predictions}


def contestant_prediction_rows(
    contestant_id: str,
    fixtures: list[dict[str, Any]],
    predictions: list[dict[str, Any]],
    scores: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    predictions_by_key = prediction_lookup(predictions)
    scores_by_key = score_lookup(scores)
    rows = []
    for fixture in fixtures:
        key = (contestant_id, fixture["match_id"])
        prediction = predictions_by_key.get(key)
        score = scores_by_key.get(key)
        if not prediction and not score:
            continue
        rows.append(
            {
                "fixture": fixture,
                "prediction_record": prediction,
                "prediction": prediction.get("prediction") if prediction else None,
                "score": score,
            }
        )
    return rows


def fixture_prediction_rows_by_match(
    fixtures: list[dict[str, Any]],
    registry: list[dict[str, Any]],
    predictions: list[dict[str, Any]],
    scores: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    predictions_by_key = prediction_lookup(predictions)
    scores_by_key = score_lookup(scores)
    contestants_by_id = {contestant["id"]: contestant for contestant in registry}
    registered_ids = [contestant["id"] for contestant in registry]
    extra_ids = sorted(
        {
            item["contestant_id"]
            for item in [*predictions, *scores]
            if item.get("contestant_id") and item["contestant_id"] not in contestants_by_id
        }
    )
    contestant_ids = registered_ids + extra_ids

    rows_by_match = {}
    for fixture in fixtures:
        match_id = fixture["match_id"]
        rows = []
        submission_count = 0
        for contestant_id in contestant_ids:
            contestant = contestants_by_id.get(
                contestant_id,
                {"id": contestant_id, "name": contestant_id, "status": "unknown"},
            )
            prediction_record = predictions_by_key.get((contestant_id, match_id))
            prediction = prediction_record.get("prediction") if prediction_record else None
            if prediction_record:
                submission_count += 1
            rows.append(
                {
                    "contestant": contestant,
                    "registered": contestant_id in contestants_by_id,
                    "prediction_record": prediction_record,
                    "prediction": prediction,
                    "score": scores_by_key.get((contestant_id, match_id)),
                }
            )
        rows_by_match[match_id] = {
            "rows": rows,
            "contestant_count": len(rows),
            "submission_count": submission_count,
        }
    return rows_by_match


def latest_simulation(simulations: list[dict[str, Any]], contestant_id: str) -> dict[str, Any] | None:
    contestant_simulations = [
        simulation
        for simulation in simulations
        if simulation.get("contestant_id") == contestant_id
    ]
    if not contestant_simulations:
        return None
    return sorted(contestant_simulations, key=lambda item: item.get("simulated_at", ""), reverse=True)[0]


def save_simulation_result(store, simulation: dict[str, Any]) -> None:
    with store.locked():
        simulations = store.read("simulations.json")
        simulations.append(simulation)
        by_contestant: dict[str, list[dict[str, Any]]] = {}
        for item in simulations:
            by_contestant.setdefault(item.get("contestant_id", ""), []).append(item)

        pruned = []
        for rows in by_contestant.values():
            pruned.extend(sorted(rows, key=lambda item: item.get("simulated_at", ""), reverse=True)[:3])
        pruned.sort(key=lambda item: item.get("simulated_at", ""), reverse=True)
        store.write("simulations.json", pruned[:100])


@app.get("/")
def root_redirect():
    return RedirectResponse(app_path("/"), status_code=307)


@app.get("/favicon.ico")
def root_favicon():
    return FileResponse(PACKAGE_DIR / "static" / "favicon.svg", media_type="image/svg+xml")


@router.get("/")
def schedule_page(request: Request):
    return templates.TemplateResponse(request, "schedule.html", load_context(request))


@router.get("/favicon.ico")
def prefixed_favicon():
    return FileResponse(PACKAGE_DIR / "static" / "favicon.svg", media_type="image/svg+xml")


@router.get("/leaderboard")
def leaderboard_page(request: Request):
    return templates.TemplateResponse(request, "leaderboard.html", load_context(request))


@router.get("/leaderboard/{contestant_id}")
def contestant_page(request: Request, contestant_id: str):
    context = load_context(request)
    contestant = next((item for item in context["registry"] if item["id"] == contestant_id), None)
    if contestant is None:
        raise HTTPException(status_code=404, detail="Contestant not found")
    context["contestant"] = contestant
    context["prediction_rows"] = contestant_prediction_rows(
        contestant_id,
        context["fixtures"],
        context["predictions"],
        context["scores"],
    )
    context["contestant_summary"] = next(
        (row for row in context["leaderboard"] if row["contestant_id"] == contestant_id),
        {
            "contestant_id": contestant_id,
            "name": contestant.get("name", contestant_id),
            "status": contestant.get("status", "active"),
            "total_points": 0.0,
            "scored_matches": 0,
        },
    )
    context["latest_simulation"] = latest_simulation(context["simulations"], contestant_id)
    return templates.TemplateResponse(request, "contestant.html", context)


@router.get("/leaderboard/{contestant_id}/bracket")
def contestant_bracket_page(request: Request, contestant_id: str):
    context = load_context(request)
    contestant = next((item for item in context["registry"] if item["id"] == contestant_id), None)
    if contestant is None:
        raise HTTPException(status_code=404, detail="Contestant not found")
    context["contestant"] = contestant
    context["simulation"] = latest_simulation(context["simulations"], contestant_id)
    return templates.TemplateResponse(request, "bracket.html", context)


@router.get("/leaderboard/{contestant_id}/api-test")
def contestant_api_test_page(request: Request, contestant_id: str, preset: str = "next"):
    context = load_context(request)
    contestant = next((item for item in context["registry"] if item["id"] == contestant_id), None)
    if contestant is None:
        raise HTTPException(status_code=404, detail="Contestant not found")
    payloads = api_test_payloads(context["fixtures"], context["groups"])
    selected_preset = preset if preset in payloads else "next"
    context["contestant"] = contestant
    context["api_test_payloads"] = payloads
    context["selected_preset"] = selected_preset
    context["payload_text"] = json.dumps(payloads[selected_preset], ensure_ascii=False, indent=2)
    context["api_test_result"] = None
    return templates.TemplateResponse(request, "api_test.html", context)


@router.get("/admin")
def admin_page(request: Request):
    return templates.TemplateResponse(request, "admin.html", load_context(request))


@router.post("/admin/login")
def login(token: str = Form(...)):
    if token != admin_token():
        return RedirectResponse(f"{app_path('/admin')}?message=Invalid%20admin%20token", status_code=303)
    response = RedirectResponse(f"{app_path('/admin')}?message=Signed%20in", status_code=303)
    response.set_cookie(
        "admin_session",
        encrypt_admin_cookie(),
        httponly=True,
        samesite="lax",
        secure=admin_cookie_secure(),
        path=admin_cookie_path(),
        max_age=admin_cookie_ttl_seconds(),
    )
    response.delete_cookie("admin_token")
    response.delete_cookie("admin_token", path=app_path("/admin"))
    response.delete_cookie("admin_token", path=admin_cookie_path())
    return response


@router.post("/admin/logout")
def logout():
    response = RedirectResponse(f"{app_path('/admin')}?message=Signed%20out", status_code=303)
    response.delete_cookie("admin_session")
    response.delete_cookie("admin_session", path=app_path("/admin"))
    response.delete_cookie("admin_session", path=admin_cookie_path())
    response.delete_cookie("admin_token")
    response.delete_cookie("admin_token", path=app_path("/admin"))
    response.delete_cookie("admin_token", path=admin_cookie_path())
    return response


@router.post("/admin/import-schedule")
def import_schedule(request: Request, admin_session_cookie: str | None = Cookie(default=None, alias="admin_session")):
    require_admin(request, admin_session_cookie)
    fixture_count, group_count = save_imported_schedule(PROJECT_ROOT / "world_cup_2026_v1.3.xlsx")
    return redirect_to_admin(f"Imported {fixture_count} fixtures and {group_count} groups")


@router.post("/admin/clear")
def clear_section(
    request: Request,
    section: str = Form(...),
    admin_session_cookie: str | None = Cookie(default=None, alias="admin_session"),
):
    require_admin(request, admin_session_cookie)
    store = get_store()
    with store.locked():
        if section == "schedule":
            store.write("fixtures.json", [])
            store.write("groups.json", {})
            store.write("predictions.json", [])
            store.write("scores.json", [])
            store.write("run_log.json", [])
            store.write("simulations.json", [])
            message = "Cleared schedule and dependent workflow data"
        elif section == "workflow":
            store.write("predictions.json", [])
            store.write("scores.json", [])
            store.write("run_log.json", [])
            message = "Cleared predictions, scores, and run log"
        elif section == "results":
            fixtures = store.read("fixtures.json")
            for fixture in fixtures:
                fixture["score_a"] = None
                fixture["score_b"] = None
                fixture["winner"] = None
                fixture["status"] = "scheduled"
            store.write("fixtures.json", fixtures)
            store.write("scores.json", [])
            message = "Cleared all results and scores"
        elif section == "fixture_teams":
            fixtures = store.read("fixtures.json")
            for fixture in fixtures:
                if fixture.get("stage") != "group":
                    fixture["team_a"] = None
                    fixture["team_b"] = None
            store.write("fixtures.json", fixtures)
            message = "Cleared knockout fixture teams"
        elif section == "groups":
            store.write("groups.json", {})
            message = "Cleared groups"
        elif section == "endpoints":
            store.write("registry.json", [])
            store.write("predictions.json", [])
            store.write("scores.json", [])
            store.write("simulations.json", [])
            message = "Cleared endpoints and dependent scores"
        elif section == "run_log":
            store.write("run_log.json", [])
            message = "Cleared run log"
        elif section == "simulations":
            store.write("simulations.json", [])
            message = "Cleared simulated brackets"
        else:
            raise HTTPException(status_code=400, detail="Unknown section")
    return redirect_to_admin(message)


@router.post("/admin/results")
def save_result(
    request: Request,
    match_id: str = Form(...),
    score_a: int = Form(...),
    score_b: int = Form(...),
    winner: str = Form(""),
    admin_session_cookie: str | None = Cookie(default=None, alias="admin_session"),
):
    require_admin(request, admin_session_cookie)
    store = get_store()
    with store.locked():
        fixtures = store.read("fixtures.json")
        fixture = _fixture_by_id(fixtures, match_id)
        if score_a < 0 or score_b < 0:
            raise HTTPException(status_code=400, detail="Scores must be non-negative")
        computed_winner = winner_from_score(fixture.get("team_a"), fixture.get("team_b"), score_a, score_b)
        fixture["score_a"] = score_a
        fixture["score_b"] = score_b
        fixture["winner"] = computed_winner if computed_winner is not None else (winner or None)
        fixture["status"] = "completed"
        store.write("fixtures.json", fixtures)
    return redirect_to_admin(f"Saved result for {match_id}")


@router.post("/admin/results/clear")
def clear_result(
    request: Request,
    match_id: str = Form(...),
    admin_session_cookie: str | None = Cookie(default=None, alias="admin_session"),
):
    require_admin(request, admin_session_cookie)
    store = get_store()
    with store.locked():
        fixtures = store.read("fixtures.json")
        fixture = _fixture_by_id(fixtures, match_id)
        fixture["score_a"] = None
        fixture["score_b"] = None
        fixture["winner"] = None
        fixture["status"] = "scheduled"
        store.write("fixtures.json", fixtures)
    return redirect_to_admin(f"Cleared result for {match_id}")


@router.post("/admin/fixtures/teams")
def save_fixture_teams(
    request: Request,
    match_id: str = Form(...),
    team_a: str = Form(""),
    team_b: str = Form(""),
    admin_session_cookie: str | None = Cookie(default=None, alias="admin_session"),
):
    require_admin(request, admin_session_cookie)
    store = get_store()
    with store.locked():
        fixtures = store.read("fixtures.json")
        fixture = _fixture_by_id(fixtures, match_id)
        fixture["team_a"] = team_a.strip() or None
        fixture["team_b"] = team_b.strip() or None
        store.write("fixtures.json", fixtures)
    return redirect_to_admin(f"Updated teams for {match_id}")


@router.post("/admin/groups")
def save_group(
    request: Request,
    group: str = Form(...),
    team_1: str = Form(""),
    team_2: str = Form(""),
    team_3: str = Form(""),
    team_4: str = Form(""),
    admin_session_cookie: str | None = Cookie(default=None, alias="admin_session"),
):
    require_admin(request, admin_session_cookie)
    group_key = group.strip().upper()
    teams = [team.strip() for team in [team_1, team_2, team_3, team_4] if team.strip()]
    if not group_key or len(teams) != 4:
        raise HTTPException(status_code=400, detail="A group needs a name and four teams")
    store = get_store()
    with store.locked():
        groups = store.read("groups.json")
        groups[group_key] = teams
        store.write("groups.json", groups)
    return redirect_to_admin(f"Saved Group {group_key}")


@router.post("/admin/endpoints")
async def save_endpoint(
    request: Request,
    contestant_id: str = Form(""),
    name: str = Form(...),
    url: str = Form(...),
    contact: str = Form(""),
    status: str = Form("active"),
    admin_session_cookie: str | None = Cookie(default=None, alias="admin_session"),
):
    require_admin(request, admin_session_cookie)
    store = get_store()
    fixtures = store.read("fixtures.json")
    validation = await validate_endpoint(url.strip(), fixtures)
    final_status = status if validation["valid"] else "inactive"
    with store.locked():
        registry = store.read("registry.json")
        contestant_id = contestant_id.strip() or _new_contestant_id(name, registry)
        existing = next((contestant for contestant in registry if contestant["id"] == contestant_id), None)
        payload = {
            "id": contestant_id,
            "name": name.strip(),
            "url": url.strip(),
            "contact": contact.strip(),
            "status": final_status,
            "last_checked_at": validation["checked_at"],
            "last_check_status": "valid" if validation["valid"] else "invalid",
            "last_check_error": validation["error"],
        }
        if existing:
            existing.clear()
            existing.update(payload)
        else:
            registry.append(payload)
        registry.sort(key=lambda item: item["name"].lower())
        store.write("registry.json", registry)
    if validation["valid"]:
        return redirect_to_admin(f"Saved endpoint {contestant_id}: validation passed")
    return redirect_to_admin(f"Saved endpoint {contestant_id}: validation failed and endpoint was set inactive")


@router.post("/admin/run-due")
async def run_due(
    request: Request,
    admin_session_cookie: str | None = Cookie(default=None, alias="admin_session"),
):
    require_admin(request, admin_session_cookie)
    result = await run_due_once(get_store(), RunnerConfig())
    return redirect_to_admin(f"Run attempted {result['jobs_attempted']} jobs")


@router.post("/admin/simulations/run")
async def run_simulation(
    request: Request,
    contestant_id: str = Form(...),
    admin_session_cookie: str | None = Cookie(default=None, alias="admin_session"),
):
    require_admin(request, admin_session_cookie)
    store = get_store()
    with store.locked():
        fixtures = store.read("fixtures.json")
        groups = store.read("groups.json")
        registry = store.read("registry.json")
    contestant = next((item for item in registry if item["id"] == contestant_id), None)
    if contestant is None:
        raise HTTPException(status_code=404, detail="Contestant not found")

    simulation = await simulate_contestant(contestant, fixtures, groups, SimulationConfig())
    save_simulation_result(store, simulation)
    message = f"Simulated bracket for {contestant.get('name', contestant_id)}"
    if simulation["error_count"]:
        message += f" with {simulation['error_count']} fallback predictions"
    return RedirectResponse(
        f"{app_path('/leaderboard/' + contestant_id + '/bracket')}?message={quote(message)}",
        status_code=303,
    )


@router.get("/schedule.json")
def schedule_download():
    fixtures = sorted(get_store().read("fixtures.json"), key=lambda item: item["match_number"])
    return Response(
        content=json.dumps(fixtures, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="schedule.json"'},
    )


@router.get("/api/fixtures")
def api_fixtures():
    return JSONResponse(get_store().read("fixtures.json"))


@router.get("/api/groups")
def api_groups():
    return JSONResponse(get_store().read("groups.json"))


@router.get("/api/leaderboard")
def api_leaderboard():
    store = get_store()
    return JSONResponse(leaderboard(store.read("registry.json"), store.read("scores.json")))


@router.get("/healthz")
def healthz():
    store = get_store()
    fixtures = store.read("fixtures.json")
    registry = store.read("registry.json")
    simulations = store.read("simulations.json")
    return JSONResponse(
        {
            "status": "ok",
            "fixtures": len(fixtures),
            "completed": schedule_summary(fixtures)["completed"],
            "active_endpoints": sum(1 for contestant in registry if contestant.get("status", "active") == "active"),
            "simulations": len(simulations),
        }
    )


def _fixture_by_id(fixtures: list[dict[str, Any]], match_id: str) -> dict[str, Any]:
    fixture = next((item for item in fixtures if item["match_id"] == match_id), None)
    if fixture is None:
        raise HTTPException(status_code=404, detail="Fixture not found")
    return fixture


def _new_contestant_id(name: str, registry: list[dict[str, Any]]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "contestant"
    existing = {contestant["id"] for contestant in registry}
    if base not in existing:
        return base
    return f"{base}-{uuid4().hex[:6]}"


def endpoint_validation_fixture(fixtures: list[dict[str, Any]]) -> dict[str, Any]:
    resolved = [fixture for fixture in fixtures if is_resolved_fixture(fixture)]
    now = utc_now()
    future = [fixture for fixture in resolved if parse_iso_z(fixture["kickoff_at"]) >= now]
    candidates = future or resolved
    if candidates:
        return sorted(candidates, key=lambda item: item["kickoff_at"])[0]
    return {
        "match_id": "validation-dummy",
        "stage": "group",
        "team_a": "Validation Team A",
        "team_b": "Validation Team B",
        "kickoff_at": "",
    }


def api_test_payloads(fixtures: list[dict[str, Any]], groups: dict[str, list[str]]) -> dict[str, dict[str, Any]]:
    resolved = [fixture for fixture in sorted(fixtures, key=lambda item: item.get("match_number", 0)) if is_resolved_fixture(fixture)]
    now = utc_now()
    future = [
        fixture
        for fixture in resolved
        if fixture.get("kickoff_at") and parse_iso_z(fixture["kickoff_at"]) >= now
    ]
    group_fixtures = [fixture for fixture in resolved if fixture.get("stage") == "group"]
    knockout_fixtures = [fixture for fixture in fixtures if fixture.get("stage") != "group"]
    resolved_knockout_fixtures = [fixture for fixture in knockout_fixtures if is_resolved_fixture(fixture)]

    next_fixture = sorted(future or resolved, key=lambda item: item.get("kickoff_at") or "")[0] if (future or resolved) else None
    group_fixture = group_fixtures[0] if group_fixtures else next_fixture
    knockout_fixture = resolved_knockout_fixtures[0] if resolved_knockout_fixtures else resolved_knockout_test_fixture(knockout_fixtures, groups)

    return {
        "next": payload_for_fixture(next_fixture or fallback_test_fixture("group", groups), sample_previous_results(fixtures, (next_fixture or {}).get("match_id"))),
        "group": payload_for_fixture(group_fixture or fallback_test_fixture("group", groups), sample_previous_results(fixtures, (group_fixture or {}).get("match_id"))),
        "knockout": payload_for_fixture(knockout_fixture, sample_previous_results(fixtures, knockout_fixture.get("match_id"))),
        "knockout_draw": payload_for_fixture(knockout_fixture, sample_previous_results(fixtures, knockout_fixture.get("match_id"))),
    }


def fallback_test_fixture(stage: str, groups: dict[str, list[str]] | None = None) -> dict[str, Any]:
    teams = [team for group_teams in (groups or {}).values() for team in group_teams]
    team_a = teams[0] if len(teams) >= 1 else "Mexico"
    team_b = teams[1] if len(teams) >= 2 else "South Africa"
    match_id = "2026-001" if stage == "group" else "2026-073"
    return {
        "match_id": match_id,
        "match_number": 0,
        "stage": stage,
        "group": "A" if stage == "group" else None,
        "team_a": team_a,
        "team_b": team_b,
        "kickoff_at": "",
    }


def resolved_knockout_test_fixture(knockout_fixtures: list[dict[str, Any]], groups: dict[str, list[str]]) -> dict[str, Any]:
    fixture = dict(knockout_fixtures[0]) if knockout_fixtures else fallback_test_fixture("round_of_32", groups)
    fixture["team_a"] = fixture.get("team_a") or team_from_test_placeholder(fixture.get("team_a_placeholder"), groups, 0)
    fixture["team_b"] = fixture.get("team_b") or team_from_test_placeholder(fixture.get("team_b_placeholder"), groups, 1)
    if not fixture.get("team_a") or not fixture.get("team_b") or fixture["team_a"] == fixture["team_b"]:
        fallback = fallback_test_fixture(fixture.get("stage", "round_of_32"), groups)
        fixture["team_a"] = fallback["team_a"]
        fixture["team_b"] = fallback["team_b"]
    return fixture


def team_from_test_placeholder(placeholder: Any, groups: dict[str, list[str]], fallback_index: int) -> str | None:
    teams = [team for group_teams in groups.values() for team in group_teams]
    if placeholder is None:
        return teams[fallback_index] if len(teams) > fallback_index else None
    value = str(placeholder)
    rank_match = re.match(r"^([123])([A-L])$", value)
    if rank_match:
        rank = int(rank_match.group(1))
        group = rank_match.group(2)
        group_teams = groups.get(group, [])
        if len(group_teams) >= rank:
            return group_teams[rank - 1]
    if value.startswith("3rd Group "):
        _, _, suffix = value.partition("3rd Group ")
        for group in [item.strip() for item in suffix.split("/") if item.strip()]:
            group_teams = groups.get(group, [])
            if len(group_teams) >= 3:
                return group_teams[2]
    return teams[fallback_index] if len(teams) > fallback_index else value


def sample_previous_results(fixtures: list[dict[str, Any]], exclude_match_id: str | None = None) -> list[dict[str, Any]]:
    real_results = completed_results(fixtures)
    if real_results:
        return real_results

    rows = []
    for fixture in sorted(fixtures, key=lambda item: item.get("match_number", 0)):
        if fixture.get("match_id") == exclude_match_id or not is_resolved_fixture(fixture):
            continue
        rows.append(
            {
                "match_id": fixture["match_id"],
                "stage": fixture["stage"],
                "team_a": fixture.get("team_a"),
                "team_b": fixture.get("team_b"),
                "score_a": 2,
                "score_b": 1,
                "winner": fixture.get("team_a"),
            }
        )
        if len(rows) == 3:
            break
    return rows


def payload_for_fixture(fixture: dict[str, Any], previous_results: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "match_id": fixture["match_id"],
        "stage": fixture["stage"],
        "team_a": fixture["team_a"],
        "team_b": fixture["team_b"],
        "previous_results": previous_results,
    }


async def validate_endpoint(url: str, fixtures: list[dict[str, Any]]) -> dict[str, Any]:
    checked_at = utc_now().isoformat(timespec="seconds").replace("+00:00", "Z")
    if not url:
        return {"valid": False, "checked_at": checked_at, "error": "URL is required"}
    fixture = endpoint_validation_fixture(fixtures)
    payload = {
        "match_id": fixture["match_id"],
        "stage": fixture["stage"],
        "team_a": fixture["team_a"],
        "team_b": fixture["team_b"],
        "previous_results": completed_results(fixtures),
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            response_json = response.json()
    except Exception as exc:
        return {"valid": False, "checked_at": checked_at, "error": f"{type(exc).__name__}: {exc}"}

    valid, _, error = validate_prediction(fixture, response_json)
    return {"valid": valid, "checked_at": checked_at, "error": error}


app.include_router(router)
