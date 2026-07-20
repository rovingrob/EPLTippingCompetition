from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

from .models import (
    display_team,
    fixture_sort_key,
    is_completed_fixture,
    isoformat_z,
    result_key,
    utc_now,
)


CORRECT_RESULT_POINTS = 1.0
EXACT_SCORE_POINTS = 1.5
SNAKE_COLORS = [
    "#00ff87",
    "#ff2882",
    "#41c7ff",
    "#ffd23f",
    "#ff7a45",
    "#7c5cff",
    "#23c4c4",
    "#fb4e6d",
    "#a78bfa",
    "#2dd4bf",
    "#f59e0b",
    "#f472b6",
]
SNAKE_POINT_SCALE_RANK_WEIGHT = 0.45


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


def _format_chart_number(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _format_points(points: float) -> str:
    return f"{points:.1f}"


def _estimated_label_width(label: str) -> int:
    return max(42, len(label) * 7 + 18)


def _point_axis(max_points: float) -> tuple[float, list[float]]:
    target = max(1.0, max_points)
    step = 0.5
    for candidate in [0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0, 100.0]:
        step = candidate
        if target / candidate <= 6:
            break
    ceiling = math.ceil(target / step) * step
    tick_count = int(round(ceiling / step))
    return ceiling, [round(index * step, 1) for index in range(tick_count + 1)]


def _snake_point_scale(point_axis_max: float, current_points: list[float]) -> Callable[[float], float]:
    distinct_points = sorted({round(float(points), 6) for points in current_points})
    anchors = {0.0: 0.0, point_axis_max: 1.0}
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
        clamped = min(max(float(points), 0.0), point_axis_max)
        for (left_points, left_position), (right_points, right_position) in zip(
            ordered_anchors,
            ordered_anchors[1:],
            strict=False,
        ):
            if clamped <= right_points:
                if right_points == left_points:
                    return right_position
                ratio = (clamped - left_points) / (right_points - left_points)
                return left_position + ratio * (right_position - left_position)
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


def _contestants_from_scores(
    registry: list[dict[str, Any]],
    scores: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
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
        if contestant_id:
            contestants.setdefault(
                contestant_id,
                {"contestant_id": contestant_id, "name": contestant_id, "status": "unknown"},
            )
    return contestants


def _match_sort_key(
    match_id: str,
    fixture_by_id: dict[str, dict[str, Any]],
) -> tuple[str, int, int, str]:
    return (*fixture_sort_key(fixture_by_id.get(match_id, {})), match_id)


def _checkpoint_for_match(
    match_id: str | None,
    fixture: dict[str, Any] | None,
    index: int,
) -> dict[str, Any]:
    if match_id is None:
        label = "Season start"
        short_label = "Start"
    else:
        fixture = fixture or {}
        matchday = fixture.get("matchday")
        home = display_team(fixture, "home")
        away = display_team(fixture, "away")
        label = f"Matchday {matchday}: {home} vs {away}" if matchday else f"{home} vs {away}"
        short_label = f"MD {matchday}" if matchday else f"Game {index}"
    return {"index": index, "match_id": match_id, "label": label, "short_label": short_label}


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
    snapshot: dict[str, dict[str, Any]] = {}
    previous_points: float | None = None
    rank = 0
    for place, contestant in enumerate(ordered, start=1):
        contestant_id = contestant["contestant_id"]
        points = totals.get(contestant_id, 0.0)
        if previous_points is None or points != previous_points:
            rank = place
            previous_points = points
        snapshot[contestant_id] = {"place": place, "rank": rank, "points": points}
    return snapshot


def leaderboard_snake(
    registry: list[dict[str, Any]],
    fixtures: list[dict[str, Any]],
    scores: list[dict[str, Any]],
) -> dict[str, Any]:
    contestants = _contestants_from_scores(registry, scores)
    contestant_count = len(contestants)
    base_chart = {
        "width": 920,
        "height": 240,
        "plot_left": 58,
        "plot_right": 150,
        "plot_top": 30,
        "plot_bottom": 44,
    }
    empty_snake = {
        "checkpoints": [],
        "contestants": [],
        "movers": [],
        "point_guides": [],
        "contestant_count": contestant_count,
        "movement_count": 0,
        "scored_count": 0,
        "remaining_count": len(fixtures),
        "total_match_count": len(fixtures),
        "remaining_possible_points": len(fixtures) * EXACT_SCORE_POINTS,
        "latest_scored_checkpoint": None,
        "future_zone": None,
        "chart": base_chart,
    }
    if not contestants:
        return empty_snake

    fixture_by_id = {fixture["match_id"]: fixture for fixture in fixtures if fixture.get("match_id")}
    scores_by_match: dict[str, list[dict[str, Any]]] = {}
    for score in scores:
        match_id = score.get("match_id")
        contestant_id = score.get("contestant_id")
        if match_id and contestant_id in contestants:
            scores_by_match.setdefault(match_id, []).append(score)
    if not scores_by_match:
        return empty_snake

    scored_match_ids = sorted(scores_by_match, key=lambda item: _match_sort_key(item, fixture_by_id))
    axis_match_ids = sorted(
        {*fixture_by_id, *scores_by_match},
        key=lambda item: _match_sort_key(item, fixture_by_id),
    )
    checkpoints = [_checkpoint_for_match(None, None, 0)]
    for match_id in axis_match_ids:
        checkpoints.append(_checkpoint_for_match(match_id, fixture_by_id.get(match_id), len(checkpoints)))
    checkpoint_index = {
        checkpoint["match_id"]: checkpoint["index"]
        for checkpoint in checkpoints
        if checkpoint["match_id"] is not None
    }
    latest_scored_index = max(checkpoint_index[match_id] for match_id in scored_match_ids)
    latest_scored_checkpoint = checkpoints[latest_scored_index]
    remaining_count = max(0, len(axis_match_ids) - len(scored_match_ids))
    remaining_possible_points = remaining_count * EXACT_SCORE_POINTS
    totals = {contestant_id: 0.0 for contestant_id in contestants}
    history_by_id: dict[str, list[dict[str, Any]]] = {contestant_id: [] for contestant_id in contestants}

    def capture(checkpoint_index_value: int) -> None:
        snapshot = _rank_snapshot(contestants, totals)
        for contestant_id, point in snapshot.items():
            history_by_id[contestant_id].append(
                {
                    "checkpoint_index": checkpoint_index_value,
                    "place": point["place"],
                    "rank": point["rank"],
                    "points": point["points"],
                    "points_label": _format_points(point["points"]),
                }
            )

    capture(0)
    for match_id in scored_match_ids:
        for score in scores_by_match[match_id]:
            totals[score["contestant_id"]] += float(score.get("points", 0.0))
        capture(checkpoint_index[match_id])

    label_offsets: dict[str, int] = {}
    label_stems: dict[str, bool] = {}
    label_groups: dict[str, list[str]] = {}
    for contestant_id in sorted(
        contestants,
        key=lambda item: (-totals[item], contestants[item]["name"].casefold(), item),
    ):
        label_groups.setdefault(_format_points(totals[contestant_id]), []).append(contestant_id)
    widest_label_group = 0
    for contestant_ids in label_groups.values():
        offset = 0
        for index, contestant_id in enumerate(contestant_ids):
            label_offsets[contestant_id] = offset
            label_stems[contestant_id] = index == 0
            offset += _estimated_label_width(contestants[contestant_id]["name"])
        widest_label_group = max(widest_label_group, offset)

    chart_width = max(1000, min(1800, widest_label_group + 180))
    latest_progress = latest_scored_index / max(1, len(checkpoints) - 1)
    label_width = widest_label_group + 18
    natural_right_space = (1 - latest_progress) * (chart_width - 58)
    required_plot_right = (
        math.ceil(max(0, label_width - natural_right_space) / latest_progress)
        if latest_progress
        else 0
    )
    plot_right = min(chart_width - 158, max(36, required_plot_right))
    chart = {
        "width": chart_width,
        "height": max(240, min(560, 92 + contestant_count * 28)),
        "plot_left": 58,
        "plot_right": plot_right,
        "plot_top": 30,
        "plot_bottom": 44,
    }
    x_span = chart["width"] - chart["plot_left"] - chart["plot_right"]
    y_span = chart["height"] - chart["plot_top"] - chart["plot_bottom"]
    checkpoint_count = len(checkpoints)
    label_step = max(1, math.ceil(max(1, checkpoint_count - 1) / 8))
    protected_label_indexes = {0, latest_scored_index, checkpoint_count - 1}
    label_clearance = max(2, label_step // 3)
    label_indexes = protected_label_indexes | {
        index
        for index in range(label_step, checkpoint_count - 1, label_step)
        if all(abs(index - protected) >= label_clearance for protected in protected_label_indexes)
    }
    plot_right_edge = chart["width"] - chart["plot_right"]
    point_axis_max, point_values = _point_axis(max(totals.values(), default=0.0))
    point_scale = _snake_point_scale(point_axis_max, list(totals.values()))

    for checkpoint in checkpoints:
        x = chart["plot_left"] + (checkpoint["index"] / max(1, checkpoint_count - 1)) * x_span
        checkpoint["x"] = round(x, 1)
        checkpoint["is_future"] = checkpoint["index"] > latest_scored_index
        checkpoint["show_label"] = checkpoint["index"] in label_indexes

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
        {"value": value, "label": _format_chart_number(value), "y": round(y_for_points(value), 1)}
        for value in point_values
    ]
    color_by_id = {
        contestant_id: SNAKE_COLORS[index % len(SNAKE_COLORS)]
        for index, contestant_id in enumerate(
            sorted(
                contestants,
                key=lambda item: (
                    history_by_id[item][-1]["place"],
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
        current["label_x"] = round(current["x"] + 14 + label_offsets[contestant_id], 1)
        current["show_label_stem"] = label_stems[contestant_id]
        last_move = previous["place"] - current["place"]
        direction = "up" if last_move > 0 else "down" if last_move < 0 else "steady"
        move_label = f"up {last_move}" if last_move > 0 else f"down {abs(last_move)}" if last_move < 0 else "steady"
        contestant_rows.append(
            {
                "contestant_id": contestant_id,
                "name": contestant["name"],
                "status": contestant["status"],
                "color": color_by_id[contestant_id],
                "path": _svg_snake_path([(point["x"], point["y"]) for point in history]),
                "history": history,
                "current": current,
                "current_place": current["place"],
                "current_rank": current["rank"],
                "current_points": current["points"],
                "last_move": last_move,
                "last_move_direction": direction,
                "last_move_label": move_label,
                "aria_label": (
                    f"{contestant['name']} rank {current['rank']} with {current['points_label']} points "
                    f"after {latest_scored_checkpoint['label']}; {move_label}"
                ),
            }
        )
    contestant_rows.sort(key=lambda item: (item["current_place"], item["name"].casefold()))
    movers = sorted(
        (row for row in contestant_rows if row["last_move"]),
        key=lambda item: (-abs(item["last_move"]), item["current_place"], item["name"].casefold()),
    )[:3]
    return {
        "checkpoints": checkpoints,
        "contestants": contestant_rows,
        "movers": movers,
        "point_guides": point_guides,
        "contestant_count": contestant_count,
        "movement_count": sum(1 for row in contestant_rows if row["last_move"]),
        "scored_count": len(scored_match_ids),
        "remaining_count": remaining_count,
        "total_match_count": len(axis_match_ids),
        "remaining_possible_points": remaining_possible_points,
        "latest_scored_checkpoint": latest_scored_checkpoint,
        "future_zone": future_zone,
        "chart": chart,
    }
