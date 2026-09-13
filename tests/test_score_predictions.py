"""scripts.score_predictions: calibration summary math (no DB)."""
from datetime import date, timedelta

import pytest

from scripts.score_predictions import guidance_anchor, summarize


def _row(version, ann, p50_off, actual_off, p75_off=None, p90_off=None,
         aj=None, co=None, stored_err=True):
    ann = date.fromisoformat(ann)
    p50 = ann + timedelta(days=p50_off)
    p75 = ann + timedelta(days=p75_off if p75_off is not None else p50_off + 30)
    p90 = ann + timedelta(days=p90_off if p90_off is not None else p50_off + 60)
    actual = ann + timedelta(days=actual_off)
    row = dict(
        model_version=version, deal_pk=hash((version, ann, p50_off)) % 10**6,
        p50_close_date=p50, p75_close_date=p75, p90_close_date=p90,
        actual_close_date=actual, date_announced=ann,
        closing_guidance_arbjournal=aj, closing_guidance_companies=co,
    )
    if stored_err:
        row.update(
            p50_error_days=(actual - p50).days,
            close_within_p50=actual <= p50,
            close_within_p75=actual <= p75,
            close_within_p90=actual <= p90,
        )
    else:
        row.update(p50_error_days=None, close_within_p50=None,
                   close_within_p75=None, close_within_p90=None)
    return row


def test_summarize_per_version_and_all():
    rows = [
        _row("0.1.0", "2025-01-01", p50_off=100, actual_off=130),   # +30 late, miss P50, within P75
        _row("0.1.0", "2025-02-01", p50_off=100, actual_off=90),    # -10 early, within P50
        _row("0.2.0", "2025-03-01", p50_off=100, actual_off=100),   # exact
        _row("0.2.0", "2025-04-01", p50_off=100, actual_off=200,    # +100, miss everything
             stored_err=False),                                      # flags recomputed
    ]
    rep = summarize(rows, anchor_fn=lambda r: None)

    v1, v2, all_ = rep["0.1.0"], rep["0.2.0"], rep["all"]
    assert v1["n"] == 2 and v1["mae"] == 20.0 and v1["medae"] == 20.0
    assert v1["bias"] == 10.0
    assert v1["within_p50"] == 50.0 and v1["within_p75"] == 100.0
    assert v2["n"] == 2 and v2["mae"] == 50.0 and v2["bias"] == 50.0
    assert v2["within_p50"] == 50.0 and v2["within_p90"] == 50.0
    assert all_["n"] == 4 and all_["mae"] == 35.0
    assert all_["guided_n"] == 0 and "guidance_mae" not in all_


def test_summarize_guidance_baseline():
    rows = [
        _row("0.2.0", "2025-01-01", p50_off=100, actual_off=110),
        _row("0.2.0", "2025-01-01", p50_off=100, actual_off=150),
        _row("0.2.0", "2025-01-01", p50_off=100, actual_off=105),
    ]
    # guidance midpoint 120 days out for every row -> errors -10, +30, -15
    anchors = iter([120, 120, None])

    def _anchor(r):
        off = next(anchors)
        return None if off is None else r["date_announced"] + timedelta(days=off)

    rep = summarize(rows, anchor_fn=_anchor)["0.2.0"]
    assert rep["guided_n"] == 2
    assert rep["guidance_mae"] == 20.0          # (10 + 30) / 2
    assert rep["model_mae_on_guided"] == 30.0   # (10 + 50) / 2
    assert rep["model_beats_guidance_pct"] == 0.0


def test_summarize_empty():
    assert summarize([], anchor_fn=lambda r: None) == {"all": {"n": 0}}


@pytest.mark.parametrize("aj, co, expected", [
    ("Q4 2026", None, date(2026, 11, 15)),
    (None, "H1 2027", date(2027, 4, 1)),
    ("Q4 2026", "Q1 2027", date(2027, 2, 14)),     # later wins
    ("TBD", None, None),
    (None, None, None),
])
def test_guidance_anchor_later_wins(aj, co, expected):
    row = dict(date_announced=date(2026, 9, 1),
               closing_guidance_arbjournal=aj, closing_guidance_companies=co)
    assert guidance_anchor(row) == expected


def test_guidance_anchor_without_announcement_date():
    assert guidance_anchor(dict(date_announced=None,
                                closing_guidance_arbjournal="Q4 2026")) is None
