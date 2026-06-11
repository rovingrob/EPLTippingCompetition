from fastapi.testclient import TestClient

from world_cup_tipping.main import app, encrypt_admin_cookie
from world_cup_tipping.storage import JsonStore


def test_root_redirects_to_tipping_prefix() -> None:
    client = TestClient(app)
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/tipping/"


def test_tipping_pages_and_assets_are_served_under_prefix() -> None:
    client = TestClient(app)
    response = client.get("/tipping/")
    assert response.status_code == 200
    assert 'href="/tipping/static/styles.css?v=' in response.text
    assert 'src="/tipping/static/app.js?v=' in response.text
    assert 'href="/tipping/static/favicon.svg"' in response.text
    assert 'href="/tipping/schedule.json"' in response.text
    assert 'href="/tipping/results"' not in response.text
    assert 'href="/tipping/admin"' not in response.text

    assert client.get("/tipping/results").status_code == 404
    assert client.get("/tipping/leaderboard").status_code == 200
    assert client.get("/tipping/admin").status_code == 200
    assert client.get("/tipping/schedule.json").status_code == 200
    assert client.get("/tipping/api/fixtures").status_code == 200
    assert client.get("/tipping/api/results").status_code == 404
    assert client.get("/tipping/static/styles.css").status_code == 200
    assert client.get("/tipping/static/favicon.svg").status_code == 200
    assert client.get("/favicon.ico").headers["content-type"].startswith("image/svg+xml")
    assert client.get("/tipping/favicon.ico").headers["content-type"].startswith("image/svg+xml")


def test_healthz_reports_basic_runtime_status(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("WCT_DATA_DIR", str(tmp_path))
    store = JsonStore(tmp_path)
    store.ensure_defaults()
    store.write(
        "fixtures.json",
        [
            {"match_id": "2026-001", "status": "scheduled"},
            {"match_id": "2026-002", "status": "completed", "score_a": 1, "score_b": 0},
        ],
    )
    store.write(
        "registry.json",
        [
            {"id": "active", "status": "active"},
            {"id": "inactive", "status": "inactive"},
        ],
    )

    client = TestClient(app)
    response = client.get("/tipping/healthz")

    assert response.status_code == 200
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.json() == {"status": "ok", "fixtures": 2, "completed": 1, "active_endpoints": 1, "simulations": 0}


def test_schedule_json_downloads_current_fixtures(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("WCT_DATA_DIR", str(tmp_path))
    store = JsonStore(tmp_path)
    store.ensure_defaults()
    store.write(
        "fixtures.json",
        [
            {
                "match_id": "2026-002",
                "match_number": 2,
                "stage": "group",
                "team_a": "Canada",
                "team_b": "Switzerland",
                "kickoff_at": "2026-06-12T00:00:00Z",
                "status": "scheduled",
            },
            {
                "match_id": "2026-001",
                "match_number": 1,
                "stage": "group",
                "team_a": "Mexico",
                "team_b": "South Africa",
                "kickoff_at": "2026-06-11T19:00:00Z",
                "status": "scheduled",
            },
        ],
    )

    client = TestClient(app)
    response = client.get("/tipping/schedule.json")

    assert response.status_code == 200
    assert response.headers["content-disposition"] == 'attachment; filename="schedule.json"'
    assert response.headers["content-type"].startswith("application/json")
    assert [fixture["match_number"] for fixture in response.json()] == [1, 2]


def test_schedule_page_shows_fixture_prediction_details(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("WCT_DATA_DIR", str(tmp_path))
    store = JsonStore(tmp_path)
    store.ensure_defaults()
    store.write(
        "fixtures.json",
        [
            {
                "match_id": "2026-001",
                "match_number": 1,
                "stage": "group",
                "group": "A",
                "team_a": "Mexico",
                "team_b": "South Africa",
                "team_a_placeholder": None,
                "team_b_placeholder": None,
                "kickoff_at": "2026-06-11T19:00:00Z",
                "score_a": 2,
                "score_b": 1,
                "winner": "Mexico",
                "status": "completed",
            }
        ],
    )
    store.write(
        "registry.json",
        [
            {"id": "checked-bot", "name": "Checked Bot", "url": "http://example.test/predict", "contact": "", "status": "active"},
            {"id": "quiet-bot", "name": "Quiet Bot", "url": "http://quiet.test/predict", "contact": "", "status": "active"},
        ],
    )
    store.write(
        "predictions.json",
        [
            {
                "contestant_id": "checked-bot",
                "match_id": "2026-001",
                "valid": True,
                "prediction": {"predicted_score_a": 2, "predicted_score_b": 1, "predicted_winner": "Mexico", "confidence": 0.8},
            }
        ],
    )
    store.write(
        "scores.json",
        [{"contestant_id": "checked-bot", "match_id": "2026-001", "points": 1.5, "reason": "exact_score", "scored_at": "2026-06-12T00:00:00Z"}],
    )

    client = TestClient(app)
    response = client.get("/tipping/")

    assert response.status_code == 200
    assert "data-expandable-row" in response.text
    assert "data-fixture-toggle" in response.text
    assert "fixture-predictions-2026-001" in response.text
    assert "1 / 2 submitted" in response.text
    assert "Checked Bot" in response.text
    assert "2 - 1" in response.text
    assert "80%" in response.text
    assert "exact_score" in response.text
    assert "Quiet Bot" in response.text
    assert "No prediction" in response.text


def test_admin_clear_workflow_data_uses_temp_store(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("WCT_DATA_DIR", str(tmp_path))
    store = JsonStore(tmp_path)
    store.ensure_defaults()
    store.write("predictions.json", [{"id": "prediction"}])
    store.write("scores.json", [{"id": "score"}])
    store.write("run_log.json", [{"id": "run"}])

    client = TestClient(app)
    client.cookies.set("admin_session", encrypt_admin_cookie())
    response = client.post(
        "/tipping/admin/clear",
        data={"section": "workflow"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert store.read("predictions.json") == []
    assert store.read("scores.json") == []
    assert store.read("run_log.json") == []


def test_admin_endpoint_save_records_validation_result(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("WCT_DATA_DIR", str(tmp_path))
    store = JsonStore(tmp_path)
    store.ensure_defaults()

    async def fake_validate_endpoint(url, fixtures):
        return {"valid": True, "checked_at": "2026-06-05T00:00:00Z", "error": None}

    monkeypatch.setattr("world_cup_tipping.main.validate_endpoint", fake_validate_endpoint)

    client = TestClient(app)
    client.cookies.set("admin_session", encrypt_admin_cookie())
    response = client.post(
        "/tipping/admin/endpoints",
        data={
            "name": "Checked Bot",
            "url": "http://127.0.0.1:8001/predict",
            "contact": "local",
            "status": "active",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    registry = store.read("registry.json")
    assert registry[0]["status"] == "active"
    assert registry[0]["last_check_status"] == "valid"


def test_admin_login_sets_encrypted_session_cookie(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_TOKEN", "admin")
    monkeypatch.setenv("ADMIN_COOKIE_SECRET", "test-cookie-secret")
    client = TestClient(app)

    response = client.post("/tipping/admin/login", data={"token": "admin"}, follow_redirects=False)

    assert response.status_code == 303
    cookie = response.cookies.get("admin_session")
    assert cookie
    assert "admin" not in cookie
    assert response.cookies.get("admin_token") is None


def test_admin_login_can_mark_cookie_secure(monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_TOKEN", "admin")
    monkeypatch.setenv("ADMIN_COOKIE_SECRET", "test-cookie-secret")
    monkeypatch.setenv("ADMIN_COOKIE_SECURE", "true")
    client = TestClient(app)

    response = client.post("/tipping/admin/login", data={"token": "admin"}, follow_redirects=False)

    assert response.status_code == 303
    set_cookie = response.headers["set-cookie"]
    assert "Secure" in set_cookie
    assert "Path=/tipping" in set_cookie


def test_admin_run_simulation_saves_bracket(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("WCT_DATA_DIR", str(tmp_path))
    store = JsonStore(tmp_path)
    store.ensure_defaults()
    store.write("fixtures.json", [{"match_id": "2026-001", "match_number": 1}])
    store.write("groups.json", {"A": ["A1", "A2", "A3", "A4"]})
    store.write(
        "registry.json",
        [{"id": "sim-bot", "name": "Sim Bot", "url": "http://example.test/predict", "contact": "", "status": "active"}],
    )

    async def fake_simulate_contestant(contestant, fixtures, groups, config):
        return {
            "id": "simulation-1",
            "contestant_id": contestant["id"],
            "contestant_name": contestant["name"],
            "simulated_at": "2026-06-07T00:00:00Z",
            "status": "completed",
            "error_count": 0,
            "champion": "A1",
            "runner_up": "A2",
            "third_place": "A3",
            "fourth_place": "A4",
            "group_standings": {},
            "matches": [],
            "bracket": {
                "round_of_32": [],
                "round_of_16": [],
                "quarterfinal": [],
                "semifinal": [],
                "third_place": [],
                "final": [],
            },
        }

    monkeypatch.setattr("world_cup_tipping.main.simulate_contestant", fake_simulate_contestant)

    client = TestClient(app)
    client.cookies.set("admin_session", encrypt_admin_cookie())
    response = client.post(
        "/tipping/admin/simulations/run",
        data={"contestant_id": "sim-bot"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/tipping/leaderboard/sim-bot/bracket")
    simulations = store.read("simulations.json")
    assert simulations[0]["contestant_id"] == "sim-bot"
    assert simulations[0]["champion"] == "A1"


def test_bracket_page_shows_latest_simulation(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("WCT_DATA_DIR", str(tmp_path))
    store = JsonStore(tmp_path)
    store.ensure_defaults()
    store.write(
        "registry.json",
        [{"id": "sim-bot", "name": "Sim Bot", "url": "http://example.test/predict", "contact": "", "status": "active"}],
    )
    store.write(
        "simulations.json",
        [
            {
                "id": "simulation-1",
                "contestant_id": "sim-bot",
                "contestant_name": "Sim Bot",
                "simulated_at": "2026-06-07T00:00:00Z",
                "status": "completed",
                "error_count": 0,
                "champion": "Mexico",
                "runner_up": "Canada",
                "third_place": "Brazil",
                "fourth_place": "France",
                "group_standings": {},
                "matches": [],
                "bracket": {
                    "round_of_32": [],
                    "round_of_16": [],
                    "quarterfinal": [],
                    "semifinal": [],
                    "third_place": [],
                    "final": [
                        {
                            "match_number": 104,
                            "team_a": "Mexico",
                            "team_b": "Canada",
                            "score_a": 2,
                            "score_b": 1,
                            "winner": "Mexico",
                            "confidence": 0.9,
                            "valid": True,
                            "error": None,
                        }
                    ],
                },
            }
        ],
    )

    client = TestClient(app)
    response = client.get("/tipping/leaderboard/sim-bot/bracket")

    assert response.status_code == 200
    assert "Sim Bot Bracket" in response.text
    assert "data-bracket-scroll" in response.text
    assert 'class="bracket-center"' in response.text
    assert 'class="bracket-round is-left"' in response.text
    assert 'class="bracket-round is-right"' in response.text
    assert "Mexico" in response.text
    assert "Match 104" in response.text


def test_api_test_page_shows_runner_payload(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("WCT_DATA_DIR", str(tmp_path))
    store = JsonStore(tmp_path)
    store.ensure_defaults()
    store.write(
        "fixtures.json",
        [
            {
                "match_id": "2026-001",
                "match_number": 1,
                "stage": "group",
                "group": "A",
                "team_a": "Mexico",
                "team_b": "South Africa",
                "kickoff_at": "2026-06-11T19:00:00Z",
                "score_a": None,
                "score_b": None,
                "winner": None,
                "status": "scheduled",
            },
            {
                "match_id": "2026-002",
                "match_number": 2,
                "stage": "group",
                "group": "B",
                "team_a": "Canada",
                "team_b": "Switzerland",
                "kickoff_at": "2026-06-12T00:00:00Z",
                "score_a": None,
                "score_b": None,
                "winner": None,
                "status": "scheduled",
            }
        ],
    )
    store.write(
        "registry.json",
        [{"id": "api-bot", "name": "API Bot", "url": "http://example.test/predict", "contact": "", "status": "active"}],
    )

    client = TestClient(app)
    response = client.get("/tipping/leaderboard/api-bot/api-test?preset=group")

    assert response.status_code == 200
    assert "API Bot API Test" in response.text
    assert "2026-001" in response.text
    assert "2026-002" in response.text
    assert "previous_results" in response.text
    assert "Send POST" in response.text
    assert "Load</button>" not in response.text


def test_api_test_knockout_preset_resolves_real_fixture_teams(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("WCT_DATA_DIR", str(tmp_path))
    store = JsonStore(tmp_path)
    store.ensure_defaults()
    store.write(
        "groups.json",
        {
            "A": ["Mexico", "Korea Republic", "Czech Republic", "South Africa"],
            "B": ["Canada", "Switzerland", "Australia", "Norway"],
        },
    )
    store.write(
        "fixtures.json",
        [
            {
                "match_id": "2026-001",
                "match_number": 1,
                "stage": "group",
                "group": "A",
                "team_a": "Mexico",
                "team_b": "South Africa",
                "kickoff_at": "2026-06-11T19:00:00Z",
                "score_a": None,
                "score_b": None,
                "winner": None,
                "status": "scheduled",
            },
            {
                "match_id": "2026-073",
                "match_number": 73,
                "stage": "round_of_32",
                "group": None,
                "team_a": None,
                "team_b": None,
                "team_a_placeholder": "2A",
                "team_b_placeholder": "2B",
                "kickoff_at": "2026-06-28T19:00:00Z",
                "score_a": None,
                "score_b": None,
                "winner": None,
                "status": "scheduled",
            },
        ],
    )
    store.write(
        "registry.json",
        [{"id": "api-bot", "name": "API Bot", "url": "http://example.test/predict", "contact": "", "status": "active"}],
    )

    client = TestClient(app)
    response = client.get("/tipping/leaderboard/api-bot/api-test?preset=knockout")

    assert response.status_code == 200
    assert "2026-073" in response.text
    assert "round_of_32" in response.text
    assert "Korea Republic" in response.text
    assert "Switzerland" in response.text
    assert "2026-001" in response.text


def test_api_test_page_posts_from_browser_not_server(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("WCT_DATA_DIR", str(tmp_path))
    store = JsonStore(tmp_path)
    store.ensure_defaults()
    store.write(
        "registry.json",
        [{"id": "api-bot", "name": "API Bot", "url": "http://example.test/predict", "contact": "", "status": "active"}],
    )

    client = TestClient(app)
    response = client.get("/tipping/leaderboard/api-bot/api-test?preset=group")
    assert response.status_code == 200
    assert 'data-api-test data-endpoint-url="http://example.test/predict"' in response.text
    assert 'data-api-test-send' in response.text
    assert "No request sent yet." in response.text

    post_response = client.post("/tipping/leaderboard/api-bot/api-test", data={"payload_text": "{}"})
    assert post_response.status_code == 405


def test_plaintext_admin_token_cookie_is_rejected(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("WCT_DATA_DIR", str(tmp_path))
    JsonStore(tmp_path).ensure_defaults()
    client = TestClient(app)
    client.cookies.set("admin_token", "admin")

    response = client.post("/tipping/admin/clear", data={"section": "workflow"})

    assert response.status_code == 401


def test_leaderboard_detail_page_joins_predictions_and_scores(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("WCT_DATA_DIR", str(tmp_path))
    store = JsonStore(tmp_path)
    store.ensure_defaults()
    store.write(
        "fixtures.json",
        [
            {
                "match_id": "2026-001",
                "match_number": 1,
                "stage": "group",
                "group": "A",
                "team_a": "Mexico",
                "team_b": "South Africa",
                "team_a_placeholder": None,
                "team_b_placeholder": None,
                "kickoff_at": "2026-06-11T19:00:00Z",
                "score_a": 2,
                "score_b": 1,
                "winner": "Mexico",
                "status": "completed",
            }
        ],
    )
    store.write(
        "registry.json",
        [{"id": "checked-bot", "name": "Checked Bot", "url": "http://example.test/predict", "contact": "", "status": "active"}],
    )
    store.write(
        "predictions.json",
        [
            {
                "contestant_id": "checked-bot",
                "match_id": "2026-001",
                "valid": True,
                "prediction": {"predicted_score_a": 2, "predicted_score_b": 1, "predicted_winner": "Mexico", "confidence": 0.8},
            }
        ],
    )
    store.write(
        "scores.json",
        [{"contestant_id": "checked-bot", "match_id": "2026-001", "points": 1.5, "reason": "exact_score", "scored_at": "2026-06-12T00:00:00Z"}],
    )

    client = TestClient(app)
    response = client.get("/tipping/leaderboard/checked-bot")

    assert response.status_code == 200
    assert "Checked Bot" in response.text
    assert "exact_score" in response.text
    assert "1.5" in response.text
