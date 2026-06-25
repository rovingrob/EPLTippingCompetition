from world_cup_tipping.scoring import leaderboard_snake, score_prediction, validate_prediction


def fixture(stage: str = "group", score_a: int = 2, score_b: int = 1, winner: str | None = "Mexico") -> dict:
    return {
        "match_id": "2026-001",
        "stage": stage,
        "team_a": "Mexico",
        "team_b": "South Africa",
        "score_a": score_a,
        "score_b": score_b,
        "winner": winner,
    }


def prediction(score_a: int, score_b: int, winner: str | None = "Mexico") -> dict:
    return {
        "predicted_score_a": score_a,
        "predicted_score_b": score_b,
        "predicted_winner": winner,
        "confidence": 0.7,
    }


def test_exact_score_is_one_and_half_points() -> None:
    points, reason = score_prediction(fixture(), prediction(2, 1))
    assert points == 1.5
    assert reason == "exact_score"


def test_correct_result_is_one_point() -> None:
    points, reason = score_prediction(fixture(), prediction(1, 0))
    assert points == 1.0
    assert reason == "correct_result"


def test_correct_draw_result_is_one_point() -> None:
    draw_fixture = fixture()
    draw_fixture["score_a"] = 0
    draw_fixture["score_b"] = 0
    points, reason = score_prediction(draw_fixture, prediction(1, 1, None))
    assert points == 1.0
    assert reason == "correct_result"


def test_group_stage_draw_prediction_ignores_supplied_winner_for_scoring() -> None:
    draw_fixture = fixture(score_a=0, score_b=0, winner=None)
    points, reason = score_prediction(draw_fixture, prediction(1, 1, "Mexico"))
    assert points == 1.0
    assert reason == "correct_result"


def test_group_stage_draw_prediction_with_winner_does_not_score_as_winner() -> None:
    points, reason = score_prediction(fixture(score_a=1, score_b=0), prediction(1, 1, "Mexico"))
    assert points == 0.0
    assert reason == "incorrect_result"


def test_incorrect_result_is_zero_points() -> None:
    points, reason = score_prediction(fixture(), prediction(0, 1, "South Africa"))
    assert points == 0.0
    assert reason == "incorrect_result"


def test_invalid_prediction_schema_rejected() -> None:
    valid, normalized, error = validate_prediction(fixture(), {"predicted_score_a": -1})
    assert valid is False
    assert normalized is None
    assert error


def test_confidence_is_optional() -> None:
    valid, normalized, error = validate_prediction(
        fixture(),
        {
            "predicted_score_a": 2,
            "predicted_score_b": 1,
            "predicted_winner": "Mexico",
        },
    )
    assert valid is True
    assert normalized["confidence"] is None
    assert error is None


def test_confidence_is_validated_when_supplied() -> None:
    valid, normalized, error = validate_prediction(fixture(), prediction(2, 1) | {"confidence": 1.5})
    assert valid is False
    assert normalized is None
    assert "Confidence" in error


def test_group_stage_draw_prediction_normalizes_winner_to_draw() -> None:
    valid, normalized, error = validate_prediction(fixture(), prediction(1, 1, "Mexico"))
    assert valid is True
    assert normalized["predicted_winner"] is None
    assert error is None


def test_knockout_draw_requires_winner() -> None:
    knockout = fixture("round_of_16")
    valid, normalized, error = validate_prediction(knockout, prediction(1, 1, None))
    assert valid is False
    assert normalized is None
    assert "winner" in error


def test_knockout_stage_points_increase_by_round() -> None:
    stage_points = {
        "round_of_32": 1.0,
        "round_of_16": 2.0,
        "quarterfinal": 3.0,
        "semifinal": 4.0,
        "third_place": 5.0,
        "final": 6.0,
    }
    for stage, expected_points in stage_points.items():
        points, reason = score_prediction(fixture(stage, score_a=2, score_b=0), prediction(2, 1))
        assert points == expected_points
        assert reason == "correct_result"


def test_knockout_exact_score_adds_half_point() -> None:
    points, reason = score_prediction(fixture("round_of_16", score_a=1, score_b=0), prediction(1, 0))
    assert points == 2.5
    assert reason == "exact_score"


def test_knockout_wrong_result_is_zero_points() -> None:
    points, reason = score_prediction(fixture("semifinal", score_a=1, score_b=0), prediction(0, 1, "South Africa"))
    assert points == 0.0
    assert reason == "incorrect_result"


def test_knockout_drawn_exact_score_only_gets_exact_bonus() -> None:
    points, reason = score_prediction(
        fixture("round_of_16", score_a=1, score_b=1, winner="Mexico"),
        prediction(1, 1, "South Africa"),
    )
    assert points == 0.5
    assert reason == "exact_score"


def test_leaderboard_snake_tracks_places_and_tied_ranks_over_matches() -> None:
    registry = [
        {"id": "alpha", "name": "Alpha", "status": "active"},
        {"id": "bravo", "name": "Bravo", "status": "active"},
        {"id": "charlie", "name": "Charlie", "status": "active"},
    ]
    fixtures = [
        {"match_id": "2026-001", "match_number": 1, "stage": "group"},
        {"match_id": "2026-002", "match_number": 2, "stage": "group"},
        {"match_id": "2026-003", "match_number": 3, "stage": "group"},
    ]
    scores = [
        {"contestant_id": "alpha", "match_id": "2026-001", "points": 1.0},
        {"contestant_id": "bravo", "match_id": "2026-001", "points": 0.0},
        {"contestant_id": "charlie", "match_id": "2026-001", "points": 1.5},
        {"contestant_id": "alpha", "match_id": "2026-002", "points": 1.0},
        {"contestant_id": "bravo", "match_id": "2026-002", "points": 2.0},
        {"contestant_id": "charlie", "match_id": "2026-002", "points": 0.0},
    ]

    snake = leaderboard_snake(registry, fixtures, scores)
    rows = {row["contestant_id"]: row for row in snake["contestants"]}

    assert [checkpoint["short_label"] for checkpoint in snake["checkpoints"]] == ["Start", "M1", "M2", "M3"]
    assert [point["place"] for point in rows["alpha"]["history"]] == [1, 2, 1]
    assert [point["place"] for point in rows["bravo"]["history"]] == [2, 3, 2]
    assert [point["place"] for point in rows["charlie"]["history"]] == [3, 1, 3]
    assert rows["alpha"]["current_rank"] == 1
    assert rows["bravo"]["current_rank"] == 1
    assert rows["charlie"]["current_rank"] == 3
    assert rows["alpha"]["last_move_label"] == "up 1"
    assert rows["charlie"]["last_move_label"] == "down 2"
    assert rows["alpha"]["current"]["y"] == rows["bravo"]["current"]["y"]
    assert rows["alpha"]["current"]["y"] < rows["charlie"]["current"]["y"]
    y_span = snake["chart"]["height"] - snake["chart"]["plot_top"] - snake["chart"]["plot_bottom"]
    linear_gap = (
        (rows["alpha"]["current_points"] - rows["charlie"]["current_points"])
        / snake["point_guides"][-1]["value"]
        * y_span
    )
    assert rows["charlie"]["current"]["y"] - rows["alpha"]["current"]["y"] > linear_gap
    assert rows["alpha"]["current"]["label_x"] > rows["alpha"]["current"]["x"]
    assert rows["bravo"]["current"]["label_x"] > rows["alpha"]["current"]["label_x"]
    assert rows["alpha"]["current"]["show_label_stem"] is True
    assert rows["bravo"]["current"]["show_label_stem"] is False
    assert snake["point_guides"][0]["label"] == "0"
    assert snake["remaining_count"] == 1
    assert snake["future_zone"]["label"] == "1 games left / 1.5 pts available"
    assert snake["chart"]["plot_right"] >= 150
