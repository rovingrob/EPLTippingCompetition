from __future__ import annotations

from epl_tipping.scoring import recompute_scores, reconcile_report


def _pred(cid: str, mid: str, home: int, away: int, valid: bool = True) -> dict:
    return {
        "contestant_id": cid,
        "match_id": mid,
        "prediction": {"predicted_score_home": home, "predicted_score_away": away},
        "valid": valid,
    }


def _score(cid: str, mid: str, points: float, reason: str, scored_at: str = "2026-08-01T00:00:00Z") -> dict:
    return {"contestant_id": cid, "match_id": mid, "points": points, "reason": reason, "scored_at": scored_at}


def test_recompute_scores_reflects_current_result(make_fixture) -> None:
    fixtures = [make_fixture(source_match_id=1, status="completed", score_home=1, score_away=0)]
    registry = [{"id": "alpha", "status": "active"}]
    predictions = [_pred("alpha", "fd-1", 1, 0)]

    scores = recompute_scores(fixtures, registry, predictions)

    assert [(s["contestant_id"], s["points"], s["reason"]) for s in scores] == [("alpha", 1.5, "exact_score")]


def test_recompute_corrects_points_when_result_changes(make_fixture) -> None:
    # Result is now 2-0; alpha predicted 1-0 -> was stored as exact, should become correct_result.
    fixtures = [make_fixture(source_match_id=1, status="completed", score_home=2, score_away=0)]
    registry = [{"id": "alpha", "status": "active"}]
    predictions = [_pred("alpha", "fd-1", 1, 0)]
    stored = [_score("alpha", "fd-1", 1.5, "exact_score")]

    row = recompute_scores(fixtures, registry, predictions, previous_scores=stored)[0]

    assert (row["points"], row["reason"]) == (1.0, "correct_result")
    assert row["scored_at"] != "2026-08-01T00:00:00Z"  # changed -> fresh timestamp


def test_recompute_preserves_scored_at_when_unchanged(make_fixture) -> None:
    fixtures = [make_fixture(source_match_id=1, status="completed", score_home=1, score_away=0)]
    registry = [{"id": "alpha", "status": "active"}]
    predictions = [_pred("alpha", "fd-1", 1, 0)]
    stored = [_score("alpha", "fd-1", 1.5, "exact_score")]

    row = recompute_scores(fixtures, registry, predictions, previous_scores=stored)[0]

    assert row["scored_at"] == "2026-08-01T00:00:00Z"


def test_recompute_drops_scores_for_non_completed_fixtures(make_fixture) -> None:
    fixtures = [make_fixture(source_match_id=1, status="postponed")]
    registry = [{"id": "alpha", "status": "active"}]
    predictions = [_pred("alpha", "fd-1", 1, 0)]
    stored = [_score("alpha", "fd-1", 1.5, "exact_score")]

    assert recompute_scores(fixtures, registry, predictions, previous_scores=stored) == []


def test_reconcile_report_buckets_changed_missing_and_stale(make_fixture) -> None:
    fixtures = [make_fixture(source_match_id=1, status="completed", score_home=2, score_away=0)]
    registry = [{"id": "alpha", "status": "active"}, {"id": "bravo", "status": "active"}]
    predictions = [_pred("alpha", "fd-1", 1, 0)]  # correct_result; bravo has no prediction -> missing_prediction
    stored = [
        _score("alpha", "fd-1", 1.5, "exact_score"),  # changed: expected 1.0/correct_result
        _score("ghost", "fd-1", 1.0, "correct_result"),  # stale: not active, no prediction
        # bravo absent -> missing
    ]

    report = reconcile_report(stored, fixtures, registry, predictions)

    assert report["aligned"] is False
    assert report["counts"] == {"changed": 1, "missing": 1, "stale": 1}
    assert report["changed"][0]["contestant_id"] == "alpha"
    assert report["missing"][0]["contestant_id"] == "bravo"
    assert report["stale"][0]["contestant_id"] == "ghost"


def test_reconcile_report_aligned_when_scores_match(make_fixture) -> None:
    fixtures = [make_fixture(source_match_id=1, status="completed", score_home=1, score_away=0)]
    registry = [{"id": "alpha", "status": "active"}]
    predictions = [_pred("alpha", "fd-1", 1, 0)]
    stored = recompute_scores(fixtures, registry, predictions)

    report = reconcile_report(stored, fixtures, registry, predictions)

    assert report["aligned"] is True
    assert report["total"] == 1
    assert report["counts"] == {"changed": 0, "missing": 0, "stale": 0}
