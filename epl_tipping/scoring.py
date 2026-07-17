from __future__ import annotations

from typing import Any

from .models import is_completed_fixture, isoformat_z, result_key, utc_now


CORRECT_RESULT_POINTS = 1.0
EXACT_SCORE_POINTS = 1.5


def validate_prediction(
    fixture: dict[str, Any],
    payload: dict[str, Any],
) -> tuple[bool, dict[str, Any] | None, str | None]:
    if not isinstance(payload, dict):
        return False, None, "Prediction response must be a JSON object"

    required = ["predicted_score_home", "predicted_score_away"]
    for field in required:
        if field not in payload:
            return False, None, f"Missing field: {field}"

    score_home = payload["predicted_score_home"]
    score_away = payload["predicted_score_away"]
    if (
        isinstance(score_home, bool)
        or isinstance(score_away, bool)
        or not isinstance(score_home, int)
        or not isinstance(score_away, int)
    ):
        return False, None, "Predicted scores must be integers"
    if score_home < 0 or score_away < 0:
        return False, None, "Predicted scores must be non-negative"

    confidence = payload.get("confidence")
    if confidence is not None:
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            return False, None, "Confidence must be a number"
        if not 0 <= confidence <= 1:
            return False, None, "Confidence must be between 0 and 1"

    return (
        True,
        {
            "predicted_score_home": score_home,
            "predicted_score_away": score_away,
            "confidence": float(confidence) if confidence is not None else None,
        },
        None,
    )


def score_prediction(
    fixture: dict[str, Any],
    prediction: dict[str, Any] | None,
    valid: bool = True,
) -> tuple[float, str]:
    if not valid or prediction is None:
        return 0.0, "invalid_or_missing"
    if not is_completed_fixture(fixture):
        return 0.0, "fixture_not_completed"

    actual_home = int(fixture["score_home"])
    actual_away = int(fixture["score_away"])
    predicted_home = int(prediction["predicted_score_home"])
    predicted_away = int(prediction["predicted_score_away"])

    if predicted_home == actual_home and predicted_away == actual_away:
        return EXACT_SCORE_POINTS, "exact_score"
    if result_key(predicted_home, predicted_away) == result_key(actual_home, actual_away):
        return CORRECT_RESULT_POINTS, "correct_result"
    return 0.0, "incorrect_result"


def score_completed_matches(
    fixtures: list[dict[str, Any]],
    registry: list[dict[str, Any]],
    predictions: list[dict[str, Any]],
    scores: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    existing = {(score["contestant_id"], score["match_id"]) for score in scores}
    prediction_by_key = {
        (prediction["contestant_id"], prediction["match_id"]): prediction
        for prediction in predictions
    }
    contestant_ids = {
        contestant["id"]
        for contestant in registry
        if contestant.get("status", "active") == "active"
    }
    contestant_ids.update(prediction["contestant_id"] for prediction in predictions)

    new_scores = list(scores)
    for fixture in fixtures:
        if not is_completed_fixture(fixture):
            continue
        for contestant_id in sorted(contestant_ids):
            key = (contestant_id, fixture["match_id"])
            if key in existing:
                continue
            record = prediction_by_key.get(key)
            if record is None:
                points, reason = 0.0, "missing_prediction"
            else:
                points, reason = score_prediction(
                    fixture,
                    record.get("prediction"),
                    bool(record.get("valid")),
                )
            new_scores.append(
                {
                    "contestant_id": contestant_id,
                    "match_id": fixture["match_id"],
                    "points": points,
                    "reason": reason,
                    "scored_at": isoformat_z(utc_now()),
                }
            )
    return new_scores


def leaderboard(registry: list[dict[str, Any]], scores: list[dict[str, Any]]) -> list[dict[str, Any]]:
    contestants = {
        contestant["id"]: {
            "contestant_id": contestant["id"],
            "name": contestant.get("name", contestant["id"]),
            "status": contestant.get("status", "active"),
            "total_points": 0.0,
            "scored_matches": 0,
            "exact_scores": 0,
        }
        for contestant in registry
    }
    for score in scores:
        contestant_id = score["contestant_id"]
        row = contestants.setdefault(
            contestant_id,
            {
                "contestant_id": contestant_id,
                "name": contestant_id,
                "status": "unknown",
                "total_points": 0.0,
                "scored_matches": 0,
                "exact_scores": 0,
            },
        )
        row["total_points"] += float(score.get("points", 0))
        row["scored_matches"] += 1
        if score.get("reason") == "exact_score":
            row["exact_scores"] += 1

    ordered = sorted(
        contestants.values(),
        key=lambda item: (-item["total_points"], item["name"].casefold()),
    )
    previous_points: float | None = None
    rank = 0
    for place, row in enumerate(ordered, start=1):
        if previous_points is None or row["total_points"] != previous_points:
            rank = place
            previous_points = row["total_points"]
        row["rank"] = rank
    return ordered
