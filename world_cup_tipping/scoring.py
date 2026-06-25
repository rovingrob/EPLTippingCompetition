from __future__ import annotations

import math
from collections.abc import Callable
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
SNAKE_COLORS = [
    "#0f766e",
    "#2563eb",
    "#d97706",
    "#db2777",
    "#7c3aed",
    "#16a34a",
    "#dc2626",
    "#0891b2",
    "#be123c",
    "#4f46e5",
    "#65a30d",
    "#c2410c",
]
SNAKE_POINT_SCALE_RANK_WEIGHT = 0.45


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


def _format_chart_number(value: float) -> str:
    text = f"{value:.1f}"
    return text.rstrip("0").rstrip(".")


def _format_points(points: float) -> str:
    return f"{points:.1f}"


def _estimated_label_width(label: str) -> int:
    return max(42, len(label) * 7 + 18)


def _max_fixture_points(fixture: dict[str, Any] | None) -> float:
    stage = fixture.get("stage") if fixture else None
    if stage in KNOCKOUT_STAGE_POINTS:
        return KNOCKOUT_STAGE_POINTS[stage] + EXACT_SCORE_BONUS
    return GROUP_RESULT_POINTS + EXACT_SCORE_BONUS


def _point_axis(max_points: float) -> tuple[float, list[float]]:
    target = max(1.0, max_points)
    for step in [0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0]:
        if target / step <= 6:
            break
    ceiling = math.ceil(target / step) * step
    tick_count = int(round(ceiling / step))
    return ceiling, [round(index * step, 1) for index in range(tick_count + 1)]


def _snake_point_scale(point_axis_max: float, current_points: list[float]) -> Callable[[float], float]:
    distinct_points = sorted({round(float(points), 6) for points in current_points})
    anchors = {
        0.0: 0.0,
        point_axis_max: 1.0,
    }
    if len(distinct_points) >= 2:
        final_index = len(distinct_points) - 1
        for index, points in enumerate(distinct_points):
            linear_position = points / point_axis_max
            rank_position = index / final_index
            anchors[points] = (
                linear_position * (1 - SNAKE_POINT_SCALE_RANK_WEIGHT)
                + rank_position * SNAKE_POINT_SCALE_RANK_WEIGHT
            )

    ordered_anchors = sorted(anchors.items())

    def scaled_position(points: float) -> float:
        clamped_points = min(max(float(points), 0.0), point_axis_max)
        if clamped_points <= ordered_anchors[0][0]:
            return ordered_anchors[0][1]
        for (left_points, left_position), (right_points, right_position) in zip(
            ordered_anchors,
            ordered_anchors[1:],
            strict=False,
        ):
            if clamped_points <= right_points:
                if right_points == left_points:
                    return right_position
                segment_ratio = (clamped_points - left_points) / (right_points - left_points)
                return left_position + segment_ratio * (right_position - left_position)
        return ordered_anchors[-1][1]

    return scaled_position


def _svg_snake_path(points: list[tuple[float, float]]) -> str:
    if not points:
        return ""

    path = [f"M {_format_chart_number(points[0][0])} {_format_chart_number(points[0][1])}"]
    for previous, current in zip(points, points[1:], strict=False):
        middle_x = (previous[0] + current[0]) / 2
        path.append(
            "C "
            f"{_format_chart_number(middle_x)} {_format_chart_number(previous[1])}, "
            f"{_format_chart_number(middle_x)} {_format_chart_number(current[1])}, "
            f"{_format_chart_number(current[0])} {_format_chart_number(current[1])}"
        )
    return " ".join(path)


def _contestants_from_scores(registry: list[dict[str, Any]], scores: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    contestants = {
        contestant["id"]: {
            "contestant_id": contestant["id"],
            "name": contestant.get("name", contestant["id"]),
            "status": contestant.get("status", "active"),
        }
        for contestant in registry
    }
    for score in scores:
        contestant_id = score.get("contestant_id")
        if not contestant_id:
            continue
        contestants.setdefault(
            contestant_id,
            {
                "contestant_id": contestant_id,
                "name": contestant_id,
                "status": "unknown",
            },
        )
    return contestants


def _match_sort_key(match_id: str, fixture_by_id: dict[str, dict[str, Any]]) -> tuple[int, int | str, str]:
    fixture = fixture_by_id.get(match_id, {})
    match_number = fixture.get("match_number")
    if isinstance(match_number, bool):
        match_number = None
    try:
        if match_number is not None:
            return (0, int(match_number), match_id)
    except (TypeError, ValueError):
        pass
    return (1, match_id, match_id)


def _checkpoint_for_match(match_id: str | None, fixture: dict[str, Any] | None, index: int) -> dict[str, Any]:
    if match_id is None:
        label = "Kickoff"
        short_label = "Start"
    else:
        match_number = fixture.get("match_number") if fixture else None
        if match_number is not None:
            label = f"Match {match_number}"
            short_label = f"M{match_number}"
        else:
            label = match_id
            short_label = match_id
    return {
        "index": index,
        "match_id": match_id,
        "label": label,
        "short_label": short_label,
    }


def _rank_snapshot(
    contestants: dict[str, dict[str, Any]],
    totals: dict[str, float],
) -> dict[str, dict[str, Any]]:
    ordered = sorted(
        contestants.values(),
        key=lambda item: (
            -totals.get(item["contestant_id"], 0.0),
            item["name"].casefold(),
            item["contestant_id"],
        ),
    )
    snapshot = {}
    previous_points: float | None = None
    rank = 0
    for place, contestant in enumerate(ordered, start=1):
        contestant_id = contestant["contestant_id"]
        points = totals.get(contestant_id, 0.0)
        if previous_points is None or points != previous_points:
            rank = place
            previous_points = points
        snapshot[contestant_id] = {
            "place": place,
            "rank": rank,
            "points": points,
        }
    return snapshot


def leaderboard_snake(
    registry: list[dict[str, Any]],
    fixtures: list[dict[str, Any]],
    scores: list[dict[str, Any]],
) -> dict[str, Any]:
    contestants = _contestants_from_scores(registry, scores)
    contestant_count = len(contestants)
    empty_snake = {
        "checkpoints": [],
        "contestants": [],
        "movers": [],
        "rank_guides": [],
        "point_guides": [],
        "contestant_count": contestant_count,
        "movement_count": 0,
        "scored_count": 0,
        "remaining_count": len(fixtures),
        "total_match_count": len(fixtures),
        "remaining_possible_points": sum(_max_fixture_points(fixture) for fixture in fixtures),
        "latest_scored_checkpoint": None,
        "future_zone": None,
        "chart": {
            "width": 920,
            "height": 240,
            "plot_left": 58,
            "plot_right": 74,
            "plot_top": 30,
            "plot_bottom": 44,
        },
    }
    if not contestants:
        return empty_snake

    fixture_by_id = {fixture["match_id"]: fixture for fixture in fixtures if fixture.get("match_id")}
    scores_by_match: dict[str, list[dict[str, Any]]] = {}
    for score in scores:
        match_id = score.get("match_id")
        contestant_id = score.get("contestant_id")
        if not match_id or contestant_id not in contestants:
            continue
        scores_by_match.setdefault(match_id, []).append(score)

    if not scores_by_match:
        return empty_snake

    scored_match_ids = sorted(scores_by_match, key=lambda match_id: _match_sort_key(match_id, fixture_by_id))
    axis_match_ids = sorted(
        {*fixture_by_id, *scores_by_match},
        key=lambda match_id: _match_sort_key(match_id, fixture_by_id),
    )
    checkpoints = [_checkpoint_for_match(None, None, 0)]
    for match_id in axis_match_ids:
        checkpoints.append(_checkpoint_for_match(match_id, fixture_by_id.get(match_id), len(checkpoints)))
    checkpoint_index_by_match_id = {
        checkpoint["match_id"]: checkpoint["index"]
        for checkpoint in checkpoints
        if checkpoint["match_id"] is not None
    }
    latest_scored_index = checkpoint_index_by_match_id[scored_match_ids[-1]]
    latest_scored_checkpoint = checkpoints[latest_scored_index]
    remaining_match_ids = [
        match_id
        for match_id in axis_match_ids
        if checkpoint_index_by_match_id[match_id] > latest_scored_index
    ]
    remaining_possible_points = sum(
        _max_fixture_points(fixture_by_id.get(match_id))
        for match_id in remaining_match_ids
    )
    totals = {contestant_id: 0.0 for contestant_id in contestants}
    history_by_id: dict[str, list[dict[str, Any]]] = {contestant_id: [] for contestant_id in contestants}

    def capture_checkpoint(checkpoint_index: int) -> None:
        snapshot = _rank_snapshot(contestants, totals)
        for contestant_id, point in snapshot.items():
            history_by_id[contestant_id].append(
                {
                    "checkpoint_index": checkpoint_index,
                    "place": point["place"],
                    "rank": point["rank"],
                    "points": point["points"],
                    "points_label": _format_points(point["points"]),
                }
            )

    capture_checkpoint(0)
    for match_id in scored_match_ids:
        for score in scores_by_match[match_id]:
            contestant_id = score["contestant_id"]
            totals[contestant_id] += float(score.get("points", 0.0))
        capture_checkpoint(checkpoint_index_by_match_id[match_id])

    label_offset_by_id: dict[str, int] = {}
    label_stem_by_id: dict[str, bool] = {}
    label_groups: dict[str, list[str]] = {}
    for contestant_id in sorted(
        contestants,
        key=lambda item: (
            -totals.get(item, 0.0),
            contestants[item]["name"].casefold(),
            item,
        ),
    ):
        label_groups.setdefault(_format_points(totals.get(contestant_id, 0.0)), []).append(contestant_id)

    widest_label_group = 0
    for contestant_ids in label_groups.values():
        offset = 0
        for index, contestant_id in enumerate(contestant_ids):
            label_offset_by_id[contestant_id] = offset
            label_stem_by_id[contestant_id] = index == 0
            offset += _estimated_label_width(contestants[contestant_id]["name"])
        widest_label_group = max(widest_label_group, offset)

    label_area = min(900, max(150, widest_label_group + 36))
    chart = {
        "width": max(920, 760 + label_area),
        "height": max(240, min(560, 92 + contestant_count * 28)),
        "plot_left": 58,
        "plot_right": label_area,
        "plot_top": 30,
        "plot_bottom": 44,
    }
    x_span = chart["width"] - chart["plot_left"] - chart["plot_right"]
    y_span = chart["height"] - chart["plot_top"] - chart["plot_bottom"]
    checkpoint_count = len(checkpoints)
    label_step = max(1, (max(1, checkpoint_count - 1) + 7) // 8)
    remaining_count = max(0, len(axis_match_ids) - len(scored_match_ids))
    plot_right_edge = chart["width"] - chart["plot_right"]
    point_axis_max, point_guide_values = _point_axis(max(totals.values()))
    point_scale = _snake_point_scale(point_axis_max, list(totals.values()))

    for checkpoint in checkpoints:
        if checkpoint_count == 1:
            x = chart["plot_left"]
        else:
            x = chart["plot_left"] + (checkpoint["index"] / (checkpoint_count - 1)) * x_span
        checkpoint["x"] = round(x, 1)
        checkpoint["is_scored"] = checkpoint["index"] <= latest_scored_index
        checkpoint["is_future"] = checkpoint["index"] > latest_scored_index
        checkpoint["show_label"] = (
            checkpoint["index"] == 0
            or checkpoint["index"] == checkpoint_count - 1
            or checkpoint["index"] == latest_scored_index
            or checkpoint["index"] % label_step == 0
        )

    future_zone = None
    if remaining_count:
        latest_scored_x = latest_scored_checkpoint["x"]
        future_zone = {
            "x": latest_scored_x,
            "y": chart["plot_top"],
            "width": round(plot_right_edge - latest_scored_x, 1),
            "height": chart["height"] - chart["plot_top"] - chart["plot_bottom"],
            "label_x": round((latest_scored_x + plot_right_edge) / 2, 1),
            "label_y": chart["plot_top"] + 17,
            "label": f"{remaining_count} games left / {_format_points(remaining_possible_points)} pts available",
        }

    def y_for_points(points: float) -> float:
        return chart["plot_top"] + (1 - point_scale(points)) * y_span

    point_guides = [
        {
            "value": value,
            "label": _format_chart_number(value),
            "y": round(y_for_points(value), 1),
        }
        for value in point_guide_values
    ]

    color_by_id = {
        contestant_id: SNAKE_COLORS[index % len(SNAKE_COLORS)]
        for index, contestant_id in enumerate(
            sorted(
                contestants,
                key=lambda item: (
                    contestants[item]["name"].casefold(),
                    item,
                ),
            )
        )
    }
    contestant_rows = []
    for contestant_id, contestant in contestants.items():
        history = history_by_id[contestant_id]
        for point in history:
            checkpoint = checkpoints[point["checkpoint_index"]]
            point["x"] = checkpoint["x"]
            point["y"] = round(y_for_points(point["points"]), 1)
            point["label_x"] = round(point["x"] + 14, 1)
            point["label_y"] = round(point["y"] + 4, 1)
            point["show_label_stem"] = True
            point["label"] = checkpoint["label"]
        previous = history[-2] if len(history) > 1 else history[-1]
        current = history[-1]
        current["label_x"] = round(current["x"] + 14 + label_offset_by_id[contestant_id], 1)
        current["show_label_stem"] = label_stem_by_id[contestant_id]
        last_move = previous["place"] - current["place"]
        if last_move > 0:
            last_move_direction = "up"
            last_move_label = f"up {last_move}"
        elif last_move < 0:
            last_move_direction = "down"
            last_move_label = f"down {abs(last_move)}"
        else:
            last_move_direction = "steady"
            last_move_label = "steady"
        path_points = [(point["x"], point["y"]) for point in history]
        contestant_rows.append(
            {
                "contestant_id": contestant_id,
                "name": contestant["name"],
                "status": contestant["status"],
                "color": color_by_id[contestant_id],
                "path": _svg_snake_path(path_points),
                "history": history,
                "current": current,
                "current_place": current["place"],
                "current_rank": current["rank"],
                "current_points": current["points"],
                "current_points_label": current["points_label"],
                "last_move": last_move,
                "last_move_direction": last_move_direction,
                "last_move_label": last_move_label,
                "aria_label": (
                    f"{contestant['name']} rank {current['rank']} with "
                    f"{current['points_label']} points after {latest_scored_checkpoint['label']}; {last_move_label}"
                ),
            }
        )

    contestant_rows.sort(key=lambda item: (item["current_place"], item["name"].casefold(), item["contestant_id"]))
    movers = [
        contestant
        for contestant in sorted(
            contestant_rows,
            key=lambda item: (-abs(item["last_move"]), item["current_place"], item["name"].casefold()),
        )
        if contestant["last_move"] != 0
    ][:3]

    return {
        "checkpoints": checkpoints,
        "contestants": contestant_rows,
        "movers": movers,
        "rank_guides": [],
        "point_guides": point_guides,
        "contestant_count": contestant_count,
        "movement_count": sum(1 for contestant in contestant_rows if contestant["last_move"] != 0),
        "scored_count": len(scored_match_ids),
        "remaining_count": remaining_count,
        "total_match_count": len(axis_match_ids),
        "remaining_possible_points": remaining_possible_points,
        "latest_scored_checkpoint": latest_scored_checkpoint,
        "future_zone": future_zone,
        "chart": chart,
    }
