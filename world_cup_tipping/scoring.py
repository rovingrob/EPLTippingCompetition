from __future__ import annotations

from typing import Any

from .models import KNOCKOUT_STAGES, STAGE_GROUP, result_key, utc_now, isoformat_z


KNOCKOUT_STAGE_POINTS = {
    "round_of_32": 1.0,
    "round_of_16": 2.0,
    "quarterfinal": 3.0,
    "semifinal": 4.0,
    "third_place": 5.0,
    "final": 6.0,
}
EXACT_SCORE_BONUS = 0.5
GROUP_RESULT_POINTS = 1.0


def validate_prediction(fixture: dict[str, Any], payload: dict[str, Any]) -> tuple[bool, dict[str, Any] | None, str | None]:
    required = ["predicted_score_a", "predicted_score_b"]
    for field in required:
        if field not in payload:
            return False, None, f"Missing field: {field}"

    score_a = payload["predicted_score_a"]
    score_b = payload["predicted_score_b"]
    if isinstance(score_a, bool) or isinstance(score_b, bool) or not isinstance(score_a, int) or not isinstance(score_b, int):
        return False, None, "Predicted scores must be integers"
    if score_a < 0 or score_b < 0:
        return False, None, "Predicted scores must be non-negative"

    confidence = payload.get("confidence")
    if confidence is not None:
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            return False, None, "Confidence must be a number"
        if confidence < 0 or confidence > 1:
            return False, None, "Confidence must be between 0 and 1"

    team_a = fixture.get("team_a")
    team_b = fixture.get("team_b")
    predicted_winner = payload.get("predicted_winner")
    if predicted_winner == "":
        predicted_winner = None

    group_stage_draw = fixture["stage"] == STAGE_GROUP and score_a == score_b
    if group_stage_draw:
        predicted_winner = None

    winner_required = fixture["stage"] in KNOCKOUT_STAGES or score_a != score_b
    if winner_required and predicted_winner not in {team_a, team_b}:
        return False, None, "Predicted winner must be one of the fixture teams"

    return (
        True,
        {
            "predicted_score_a": score_a,
            "predicted_score_b": score_b,
            "predicted_winner": predicted_winner,
            "confidence": float(confidence) if confidence is not None else None,
        },
        None,
    )


def score_prediction(fixture: dict[str, Any], prediction: dict[str, Any] | None, valid: bool = True) -> tuple[float, str]:
    if not valid or prediction is None:
        return 0.0, "invalid_or_missing"
    actual_score_a = fixture.get("score_a")
    actual_score_b = fixture.get("score_b")
    if actual_score_a is None or actual_score_b is None:
        return 0.0, "fixture_not_completed"

    predicted_score_a = prediction["predicted_score_a"]
    predicted_score_b = prediction["predicted_score_b"]
    exact_score = predicted_score_a == actual_score_a and predicted_score_b == actual_score_b
    predicted_result = result_key(predicted_score_a, predicted_score_b)
    actual_result = result_key(actual_score_a, actual_score_b)

    stage = fixture.get("stage")
    if stage in KNOCKOUT_STAGE_POINTS:
        points = EXACT_SCORE_BONUS if exact_score else 0.0
        # Knockout draws can earn the exact-score bonus, but no round result points.
        if predicted_result == actual_result and actual_result != "draw":
            points += KNOCKOUT_STAGE_POINTS[stage]
        if exact_score:
            return points, "exact_score"
        if points:
            return points, "correct_result"
        return 0.0, "incorrect_result"

    if exact_score:
        return GROUP_RESULT_POINTS + EXACT_SCORE_BONUS, "exact_score"
    if predicted_result == actual_result:
        return GROUP_RESULT_POINTS, "correct_result"
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
        if fixture.get("score_a") is None or fixture.get("score_b") is None:
            continue
        for contestant_id in sorted(contestant_ids):
            key = (contestant_id, fixture["match_id"])
            if key in existing:
                continue
            prediction = prediction_by_key.get(key)
            if prediction is None:
                points, reason = 0.0, "missing_prediction"
            else:
                points, reason = score_prediction(fixture, prediction.get("prediction"), bool(prediction.get("valid")))
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
        }
        for contestant in registry
    }
    for score in scores:
        contestant = contestants.setdefault(
            score["contestant_id"],
            {
                "contestant_id": score["contestant_id"],
                "name": score["contestant_id"],
                "status": "unknown",
                "total_points": 0.0,
                "scored_matches": 0,
            },
        )
        contestant["total_points"] += float(score.get("points", 0))
        contestant["scored_matches"] += 1
    return sorted(contestants.values(), key=lambda item: (-item["total_points"], item["name"].lower()))
