from __future__ import annotations

import html
import json

from fastapi.testclient import TestClient

from epl_tipping.main import app, encrypt_admin_cookie
from epl_tipping.storage import JsonStore


def configure_app_store(tmp_path, monkeypatch) -> JsonStore:
    monkeypatch.setenv("TIPPING_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TIPPING_DISPLAY_TIMEZONE", "UTC")
    monkeypatch.setenv("ADMIN_TOKEN", "route-test-token")
    monkeypatch.setenv("ADMIN_COOKIE_SECRET", "a-distinct-route-test-cookie-secret")
    store = JsonStore(tmp_path)
    store.ensure_defaults()
    return store


def endpoint(contestant_id: str = "alpha") -> dict:
    return {
        "id": contestant_id,
        "name": "Alpha Model" if contestant_id == "alpha" else contestant_id.title(),
        "url": f"https://{contestant_id}.example/predict",
        "contact": "owner@example.com",
        "status": "active",
    }


def prediction(match_id: str = "fd-1001") -> dict:
    return {
        "id": "prediction-1",
        "contestant_id": "alpha",
        "match_id": match_id,
        "requested_at": "2099-08-15T12:00:00Z",
        "valid": True,
        "prediction": {
            "predicted_score_home": 7,
            "predicted_score_away": 6,
            "confidence": 0.37,
        },
        "raw_response": {"predicted_score_home": 7, "predicted_score_away": 6, "confidence": 0.37},
        "error": None,
    }


def test_root_pages_assets_and_security_headers_are_served_under_prefix(tmp_path, monkeypatch) -> None:
    configure_app_store(tmp_path, monkeypatch)
    client = TestClient(app)

    root = client.get("/", follow_redirects=False)
    assert root.status_code == 307
    assert root.headers["location"] == "/tipping/"

    for path in [
        "/tipping/",
        "/tipping/today",
        "/tipping/leaderboard",
        "/tipping/admin",
        "/tipping/schedule.json",
        "/tipping/api/fixtures",
        "/tipping/api/leaderboard",
        "/tipping/healthz",
        "/tipping/static/styles.css",
        "/tipping/favicon.ico",
    ]:
        assert client.get(path).status_code == 200, path

    page = client.get("/tipping/")
    assert "Premier League Schedule" in page.text
    assert 'href="/tipping/static/styles.css"' in page.text
    assert 'src="/tipping/static/app.js"' in page.text
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["x-frame-options"] == "DENY"
    assert page.headers["x-content-type-options"] == "nosniff"


def test_schedule_health_and_leaderboard_apis_use_epl_store(tmp_path, monkeypatch, make_fixture) -> None:
    store = configure_app_store(tmp_path, monkeypatch)
    store.write(
        "fixtures.json",
        [
            make_fixture(source_match_id=2, kickoff_at="2026-08-16T14:00:00Z"),
            make_fixture(
                source_match_id=1,
                kickoff_at="2026-08-15T14:00:00Z",
                status="completed",
                source_status="FINISHED",
                score_home=2,
                score_away=1,
            ),
        ],
    )
    store.write("registry.json", [endpoint()])
    store.write(
        "scores.json",
        [{"contestant_id": "alpha", "match_id": "fd-1", "points": 1.5, "reason": "exact_score"}],
    )
    store.write("source_state.json", {"last_successful_at": "2026-08-15T16:00:00Z"})
    client = TestClient(app)

    schedule = client.get("/tipping/schedule.json")
    assert [row["match_id"] for row in schedule.json()] == ["fd-1", "fd-2"]
    assert schedule.headers["content-disposition"] == 'attachment; filename="epl-schedule.json"'
    assert client.get("/tipping/api/fixtures").json() == schedule.json()
    assert client.get("/tipping/api/leaderboard").json()[0] | {"rank": 1} == {
        "contestant_id": "alpha",
        "name": "Alpha Model",
        "status": "active",
        "total_points": 1.5,
        "scored_matches": 1,
        "exact_scores": 1,
        "rank": 1,
    }
    assert client.get("/tipping/healthz").json() == {
        "status": "ok",
        "fixtures": 2,
        "completed": 1,
        "active_endpoints": 1,
        "last_source_sync": "2026-08-15T16:00:00Z",
    }


def test_public_pages_redact_pre_lock_predictions_but_admin_can_see_them(
    tmp_path,
    monkeypatch,
    make_fixture,
) -> None:
    store = configure_app_store(tmp_path, monkeypatch)
    future = make_fixture(kickoff_at="2099-08-15T14:00:00Z")
    store.write("fixtures.json", [future])
    store.write("registry.json", [endpoint()])
    store.write("predictions.json", [prediction()])
    client = TestClient(app)

    public_paths = [
        "/tipping/",
        "/tipping/today?date=2099-08-15",
        "/tipping/leaderboard/alpha",
        "/tipping/leaderboard/alpha/tips",
    ]
    for path in public_paths:
        response = client.get(path)
        assert response.status_code == 200
        assert "7–6" not in response.text
        assert "37%" not in response.text
        assert "Hidden" in response.text

    client.cookies.set("admin_session", encrypt_admin_cookie(), path="/tipping")
    admin_schedule = client.get("/tipping/")
    admin_tips = client.get("/tipping/leaderboard/alpha/tips")
    assert "7–6" in admin_schedule.text
    assert "7–6" in admin_tips.text
    assert "37%" in admin_tips.text


def test_predictions_are_public_at_or_after_lock(tmp_path, monkeypatch, make_fixture) -> None:
    store = configure_app_store(tmp_path, monkeypatch)
    store.write("fixtures.json", [make_fixture(kickoff_at="2000-08-15T14:00:00Z")])
    store.write("registry.json", [endpoint()])
    store.write("predictions.json", [prediction()])

    response = TestClient(app).get("/tipping/leaderboard/alpha/tips")

    assert response.status_code == 200
    assert "7–6" in response.text
    assert "37%" in response.text
    assert "Hidden" not in response.text


def test_completed_future_dated_fixture_predictions_are_public_in_schedule(
    tmp_path,
    monkeypatch,
    make_fixture,
) -> None:
    store = configure_app_store(tmp_path, monkeypatch)
    store.write(
        "fixtures.json",
        [
            make_fixture(
                kickoff_at="2099-08-15T14:00:00Z",
                status="completed",
                source_status="FINISHED",
                score_home=2,
                score_away=1,
            )
        ],
    )
    store.write("registry.json", [endpoint()])
    store.write("predictions.json", [prediction()])
    store.write(
        "scores.json",
        [{"contestant_id": "alpha", "match_id": "fd-1001", "points": 0.0, "reason": "incorrect_result"}],
    )

    response = TestClient(app).get("/tipping/")

    assert response.status_code == 200
    assert "7–6" in response.text
    assert "37%" in response.text
    assert "Hidden until lock" not in response.text
    assert "Confidence" in response.text
    assert "Response" in response.text
    assert "Outcome" in response.text
    assert "Points" in response.text


def test_leaderboard_renders_interactive_snake(tmp_path, monkeypatch, make_fixture) -> None:
    store = configure_app_store(tmp_path, monkeypatch)
    store.write("fixtures.json", [make_fixture(status="completed", score_home=2, score_away=1)])
    store.write("registry.json", [endpoint()])
    store.write(
        "scores.json",
        [{"contestant_id": "alpha", "match_id": "fd-1001", "points": 1.5, "reason": "exact_score"}],
    )

    response = TestClient(app).get("/tipping/leaderboard")

    assert response.status_code == 200
    assert "Leaderboard snake" in response.text
    assert "snake-chart" in response.text
    assert 'data-snake-contestant="alpha"' in response.text
    assert "Matchday 1: Arsenal vs Chelsea" in response.text


def test_contestant_routes_and_api_test_render_fixture_only_payload(tmp_path, monkeypatch, make_fixture) -> None:
    store = configure_app_store(tmp_path, monkeypatch)
    fixture = make_fixture()
    store.write("fixtures.json", [fixture])
    store.write("registry.json", [endpoint()])
    client = TestClient(app)

    assert client.get("/tipping/leaderboard/missing").status_code == 404
    assert client.get("/tipping/leaderboard/missing/tips").status_code == 404
    assert client.get("/tipping/leaderboard/missing/simulation").status_code == 404
    assert client.get("/tipping/leaderboard/alpha/api-test").status_code == 401
    client.cookies.set("admin_session", encrypt_admin_cookie(), path="/tipping")
    api_test = client.get("/tipping/leaderboard/alpha/api-test")

    assert api_test.status_code == 200
    assert "fixture-only payload" in api_test.text
    assert "previous_results" not in api_test.text
    start = api_test.text.index("{", api_test.text.index("data-api-test-payload"))
    end = api_test.text.index("</textarea>", start)
    payload = json.loads(html.unescape(api_test.text[start:end]))
    assert payload["match_id"] == "fd-1001"
    assert payload["home_team"]["tla"] == "ARS"
    assert payload["away_team"]["tla"] == "CHE"


def test_admin_login_uses_secure_cookie_and_protected_routes_require_it(tmp_path, monkeypatch) -> None:
    configure_app_store(tmp_path, monkeypatch)
    client = TestClient(app)

    assert client.post("/tipping/admin/run-due").status_code == 401
    bad = client.post("/tipping/admin/login", data={"token": "wrong"}, follow_redirects=False)
    assert bad.status_code == 303
    assert "Invalid%20token" in bad.headers["location"]

    login = client.post(
        "/tipping/admin/login",
        data={"token": "route-test-token"},
        follow_redirects=False,
    )
    cookie = login.headers["set-cookie"]
    assert login.status_code == 303
    assert "admin_session=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "Path=/tipping" in cookie
    assert "route-test-token" not in cookie
    assert "Sign out" in client.get("/tipping/admin").text


def test_admin_login_is_disabled_when_secrets_are_missing_or_identical(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TIPPING_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    monkeypatch.delenv("ADMIN_COOKIE_SECRET", raising=False)
    client = TestClient(app)

    missing = client.post("/tipping/admin/login", data={"token": "admin"}, follow_redirects=False)
    assert missing.status_code == 303
    assert "not%20configured" in missing.headers["location"]
    assert "admin_session=" not in missing.headers.get("set-cookie", "")

    monkeypatch.setenv("ADMIN_TOKEN", "same-secret")
    monkeypatch.setenv("ADMIN_COOKIE_SECRET", "same-secret")
    identical = client.post(
        "/tipping/admin/login",
        data={"token": "same-secret"},
        follow_redirects=False,
    )
    assert identical.status_code == 303
    assert "not%20configured" in identical.headers["location"]
    assert "admin_session=" not in identical.headers.get("set-cookie", "")


def test_admin_manual_result_override_invalidates_score_and_can_be_cleared(
    tmp_path,
    monkeypatch,
    make_fixture,
) -> None:
    store = configure_app_store(tmp_path, monkeypatch)
    store.write("fixtures.json", [make_fixture()])
    store.write(
        "scores.json",
        [{"contestant_id": "alpha", "match_id": "fd-1001", "points": 1.5, "reason": "exact_score"}],
    )
    client = TestClient(app)
    client.cookies.set("admin_session", encrypt_admin_cookie(), path="/tipping")

    saved = client.post(
        "/tipping/admin/results",
        data={"match_id": "fd-1001", "score_home": 3, "score_away": 1},
        follow_redirects=False,
    )
    fixture = store.read("fixtures.json")[0]
    assert saved.status_code == 303
    assert fixture["status"] == "completed"
    assert fixture["score_home"] == 3
    assert fixture["score_away"] == 1
    assert fixture["result"] == "HOME_WIN"
    assert fixture["winner"] == "Arsenal FC"
    assert fixture["result_source"] == "manual"
    assert store.read("scores.json") == []

    cleared = client.post(
        "/tipping/admin/results/clear",
        data={"match_id": "fd-1001"},
        follow_redirects=False,
    )
    fixture = store.read("fixtures.json")[0]
    assert cleared.status_code == 303
    assert fixture["status"] == "scheduled"
    assert fixture["score_home"] is None
    assert fixture["score_away"] is None
    assert fixture["result_source"] is None


def test_admin_can_reopen_one_or_all_predictions_for_a_fixture(tmp_path, monkeypatch, make_fixture) -> None:
    store = configure_app_store(tmp_path, monkeypatch)
    store.write("fixtures.json", [make_fixture(), make_fixture(source_match_id=2)])
    store.write("registry.json", [endpoint(), endpoint("bravo")])
    store.write(
        "predictions.json",
        [
            prediction(),
            prediction() | {"id": "prediction-2", "contestant_id": "bravo"},
            prediction("fd-2") | {"id": "prediction-3"},
        ],
    )
    store.write(
        "scores.json",
        [
            {"contestant_id": "alpha", "match_id": "fd-1001", "points": 1.5},
            {"contestant_id": "bravo", "match_id": "fd-1001", "points": 1.0},
            {"contestant_id": "alpha", "match_id": "fd-2", "points": 1.0},
        ],
    )
    client = TestClient(app)
    assert client.post(
        "/tipping/admin/predictions/reopen",
        data={"match_id": "fd-1001", "contestant_id": "alpha"},
    ).status_code == 401
    client.cookies.set("admin_session", encrypt_admin_cookie(), path="/tipping")

    one = client.post(
        "/tipping/admin/predictions/reopen",
        data={"match_id": "fd-1001", "contestant_id": "alpha"},
        follow_redirects=False,
    )
    assert one.status_code == 303
    assert {(row["contestant_id"], row["match_id"]) for row in store.read("predictions.json")} == {
        ("bravo", "fd-1001"),
        ("alpha", "fd-2"),
    }
    assert {(row["contestant_id"], row["match_id"]) for row in store.read("scores.json")} == {
        ("bravo", "fd-1001"),
        ("alpha", "fd-2"),
    }

    all_contestants = client.post(
        "/tipping/admin/predictions/reopen",
        data={"match_id": "fd-1001", "contestant_id": ""},
        follow_redirects=False,
    )
    assert all_contestants.status_code == 303
    assert [(row["contestant_id"], row["match_id"]) for row in store.read("predictions.json")] == [
        ("alpha", "fd-2")
    ]
    assert [(row["contestant_id"], row["match_id"]) for row in store.read("scores.json")] == [
        ("alpha", "fd-2")
    ]
    audit_log = store.read("run_log.json")
    assert len(audit_log) == 2
    assert all(row.get("match_id") == "fd-1001" for row in audit_log)


def test_simulation_request_route_queues_public_run(tmp_path, monkeypatch, make_fixture) -> None:
    store = configure_app_store(tmp_path, monkeypatch)
    store.write("fixtures.json", [make_fixture()])
    store.write("registry.json", [endpoint()])

    response = TestClient(app).post(
        "/tipping/simulations/run",
        data={"contestant_id": "alpha"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/tipping/leaderboard/alpha/simulation?message=")
    run = store.read("simulation_runs.json")[0]
    assert run["contestant_id"] == "alpha"
    assert run["requested_by"] == "public"
    assert run["status"] == "queued"


def test_leaderboard_shows_latest_simulated_winner_and_simulate_action(
    tmp_path,
    monkeypatch,
    make_fixture,
) -> None:
    store = configure_app_store(tmp_path, monkeypatch)
    store.write("fixtures.json", [make_fixture()])
    store.write("registry.json", [endpoint()])
    store.write(
        "season_simulations.json",
        [
            {
                "id": "simulation-1",
                "contestant_id": "alpha",
                "simulated_at": "2026-08-15T14:00:00Z",
                "champion": "Arsenal",
            }
        ],
    )

    response = TestClient(app).get("/tipping/leaderboard")

    assert response.status_code == 200
    assert "Predicted winner:" in response.text
    assert "Arsenal" in response.text
    assert 'href="/tipping/leaderboard/alpha/simulation"' in response.text
    assert 'action="/tipping/simulations/run"' in response.text
    assert "Simulate" in response.text
