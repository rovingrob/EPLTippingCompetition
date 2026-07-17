from __future__ import annotations

import pytest

from epl_tipping.scoring import (
    leaderboard,
    leaderboard_snake,
    score_completed_matches,
    score_prediction,
    validate_prediction,
)


def prediction(home: int, away: int, confidence: float | None = 0.7) -> dict:
    result = {"predicted_score_home": home, "predicted_score_away": away}
    if confidence is not None:
        result["confidence"] = confidence
    return result


def test_validation_normalizes_valid_prediction_and_optional_confidence(make_fixture) -> None:
    valid, normalized, error = validate_prediction(make_fixture(), prediction(2, 1))
    assert valid is True
    assert normalized == {"predicted_score_home": 2, "predicted_score_away": 1, "confidence": 0.7}
    assert error is None

    valid, normalized, error = validate_prediction(make_fixture(), prediction(0, 0, confidence=None))
    assert valid is True
    assert normalized == {"predicted_score_home": 0, "predicted_score_away": 0, "confidence": None}
    assert error is None


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"predicted_score_away": 1}, "Missing field: predicted_score_home"),
        ({"predicted_score_home": 1}, "Missing field: predicted_score_away"),
        (prediction(True, 1), "integers"),
        (prediction(1.5, 1), "integers"),
        (prediction(-1, 0), "non-negative"),
        (prediction(1, 0, confidence=True), "number"),
        (prediction(1, 0, confidence=1.01), "between 0 and 1"),
    ],
)
def test_validation_rejects_bad_prediction_payloads(make_fixture, payload, message: str) -> None:
    valid, normalized, error = validate_prediction(make_fixture(), payload)
    assert valid is False
    assert normalized is None
    assert message in error


@pytest.mark.parametrize(
    ("predicted_home", "predicted_away", "points", "reason"),
    [
        (2, 1, 1.5, "exact_score"),
        (1, 0, 1.0, "correct_result"),
        (3, 3, 0.0, "incorrect_result"),
        (0, 2, 0.0, "incorrect_result"),
    ],
)
def test_scoring_uses_exact_score_then_three_way_result(
    make_fixture,
    predicted_home: int,
    predicted_away: int,
    points: float,
    reason: str,
) -> None:
    fixture = make_fixture(status="completed", score_home=2, score_away=1)
    assert score_prediction(fixture, prediction(predicted_home, predicted_away)) == (points, reason)


def test_correct_draw_scores_one_point(make_fixture) -> None:
    fixture = make_fixture(status="completed", score_home=1, score_away=1)
    assert score_prediction(fixture, prediction(2, 2)) == (1.0, "correct_result")


def test_missing_invalid_and_unfinished_predictions_score_zero(make_fixture) -> None:
    assert score_prediction(make_fixture(), prediction(2, 1)) == (0.0, "fixture_not_completed")
    fixture = make_fixture(status="completed", score_home=2, score_away=1)
    assert score_prediction(fixture, None) == (0.0, "invalid_or_missing")
    assert score_prediction(fixture, prediction(2, 1), valid=False) == (0.0, "invalid_or_missing")


def test_score_completed_matches_covers_missing_invalid_and_inactive_predictors(make_fixture) -> None:
    fixtures = [make_fixture(status="completed", score_home=2, score_away=1)]
    registry = [
        {"id": "alpha", "name": "Alpha", "status": "active"},
        {"id": "bravo", "name": "Bravo", "status": "active"},
        {"id": "retired", "name": "Retired", "status": "inactive"},
        {"id": "never-entered", "name": "Never", "status": "inactive"},
    ]
    predictions = [
        {
            "contestant_id": "alpha",
            "match_id": "fd-1001",
            "valid": True,
            "prediction": prediction(2, 1),
        },
        {
            "contestant_id": "retired",
            "match_id": "fd-1001",
            "valid": False,
            "prediction": None,
        },
    ]

    scores = score_completed_matches(fixtures, registry, predictions, [])
    by_contestant = {row["contestant_id"]: row for row in scores}

    assert set(by_contestant) == {"alpha", "bravo", "retired"}
    assert (by_contestant["alpha"]["points"], by_contestant["alpha"]["reason"]) == (1.5, "exact_score")
    assert (by_contestant["bravo"]["points"], by_contestant["bravo"]["reason"]) == (0.0, "missing_prediction")
    assert (by_contestant["retired"]["points"], by_contestant["retired"]["reason"]) == (
        0.0,
        "invalid_or_missing",
    )
    assert score_completed_matches(fixtures, registry, predictions, scores) == scores


def test_leaderboard_aggregates_orders_and_assigns_competition_ranks() -> None:
    registry = [
        {"id": "bravo", "name": "Bravo", "status": "active"},
        {"id": "alpha", "name": "Alpha", "status": "active"},
        {"id": "charlie", "name": "Charlie", "status": "inactive"},
    ]
    scores = [
        {"contestant_id": "alpha", "match_id": "fd-1", "points": 1.5, "reason": "exact_score"},
        {"contestant_id": "alpha", "match_id": "fd-2", "points": 0.5, "reason": "correct_result"},
        {"contestant_id": "bravo", "match_id": "fd-1", "points": 1.0, "reason": "correct_result"},
        {"contestant_id": "bravo", "match_id": "fd-2", "points": 1.0, "reason": "correct_result"},
        {"contestant_id": "charlie", "match_id": "fd-1", "points": 1.5, "reason": "exact_score"},
        {"contestant_id": "orphan", "match_id": "fd-1", "points": 0.0, "reason": "incorrect_result"},
    ]

    table = leaderboard(registry, scores)

    assert [row["contestant_id"] for row in table] == ["alpha", "bravo", "charlie", "orphan"]
    assert [row["rank"] for row in table] == [1, 1, 3, 4]
    assert table[0]["total_points"] == 2.0
    assert table[0]["exact_scores"] == 1
    assert table[2]["status"] == "inactive"
    assert table[3]["status"] == "unknown"


def test_leaderboard_snake_tracks_scored_history_and_remaining_season(make_fixture) -> None:
    fixtures = [
        make_fixture(source_match_id=1, matchday=1, kickoff_at="2026-08-15T14:00:00Z"),
        make_fixture(source_match_id=2, matchday=1, kickoff_at="2026-08-16T14:00:00Z"),
        make_fixture(source_match_id=3, matchday=2, kickoff_at="2026-08-22T14:00:00Z"),
    ]
    registry = [
        {"id": "alpha", "name": "Alpha", "status": "active"},
        {"id": "bravo", "name": "Bravo", "status": "active"},
    ]
    scores = [
        {"contestant_id": "alpha", "match_id": "fd-1", "points": 1.5, "reason": "exact_score"},
        {"contestant_id": "bravo", "match_id": "fd-1", "points": 1.0, "reason": "correct_result"},
        {"contestant_id": "alpha", "match_id": "fd-2", "points": 0.0, "reason": "incorrect_result"},
        {"contestant_id": "bravo", "match_id": "fd-2", "points": 1.5, "reason": "exact_score"},
    ]

    snake = leaderboard_snake(registry, fixtures, scores)

    assert len(snake["checkpoints"]) == 4
    assert snake["scored_count"] == 2
    assert snake["remaining_count"] == 1
    assert snake["latest_scored_checkpoint"]["match_id"] == "fd-2"
    assert snake["latest_scored_checkpoint"]["label"] == "Matchday 1: Arsenal vs Chelsea"
    assert snake["future_zone"]["label"] == "1 games left / 1.5 pts available"
    by_id = {row["contestant_id"]: row for row in snake["contestants"]}
    assert by_id["alpha"]["current_points"] == 1.5
    assert by_id["bravo"]["current_points"] == 2.5
    assert by_id["bravo"]["current_rank"] == 1
    assert len(by_id["alpha"]["history"]) == 3
    assert by_id["alpha"]["path"].startswith("M ")


def test_leaderboard_snake_keeps_sparse_axis_labels_clear_of_latest_result(make_fixture) -> None:
    fixtures = [
        make_fixture(
            source_match_id=index,
            matchday=((index - 1) // 10) + 1,
            kickoff_at=f"2026-08-{((index - 1) % 28) + 1:02d}T{index % 24:02d}:00:00Z",
        )
        for index in range(1, 61)
    ]
    scores = [
        {"contestant_id": "alpha", "match_id": f"fd-{index}", "points": 1.0, "reason": "correct_result"}
        for index in range(1, 9)
    ]

    snake = leaderboard_snake([{"id": "alpha", "name": "Alpha", "status": "active"}], fixtures, scores)
    shown = [row["index"] for row in snake["checkpoints"] if row["show_label"]]

    assert snake["latest_scored_checkpoint"]["index"] in shown
    assert all(
        index == snake["latest_scored_checkpoint"]["index"]
        or abs(index - snake["latest_scored_checkpoint"]["index"]) >= 2
        for index in shown
    )
    plot_width = snake["chart"]["width"] - snake["chart"]["plot_left"] - snake["chart"]["plot_right"]
    assert plot_width / snake["chart"]["width"] > 0.85
