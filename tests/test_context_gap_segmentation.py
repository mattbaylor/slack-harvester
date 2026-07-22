#!/usr/bin/env python3
"""Hermetic unit tests for channel-context time-gap segmentation (ISSUES #15).

Unlike the repo's other test (test_get_message_thread_reply.py, which hits live
Slack), this test is FULLY HERMETIC: no live Slack, no opencode, no Chrome, no
network, no credentials. It exercises the pure helpers
`_segment_context_by_gap`, `_resolve_context_max_gap_seconds`, and the factored
prompt builder `SlackClient._build_body_prompt` directly. It never constructs
`HarvesterState` (which would read Chrome creds) — it only imports the pure
functions and the static prompt builder.

Bug being guarded: in a quiet DM, `get_context` fetched the last N messages by
count with no time window, so a captured message pulled a days-old prior
conversation into its "relevant context". See ISSUES.md (count-only context bug).

Run (from repo root):
    python3 tests/test_context_gap_segmentation.py
or with pytest:
    pytest tests/test_context_gap_segmentation.py

Exit codes (script mode): 0 all pass, 1 one or more failures.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from harvester import (  # noqa: E402
    DEFAULT_CONTEXT_MAX_GAP_HOURS,
    CaptureWorker,
    _resolve_context_max_gap_seconds,
    _segment_context_by_gap,
)

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "context-window.json"
# A real seam-fetched window (gitignored) overrides the committed synthetic one
# when present — see the synthetic fixture's _fixture_note for the fetch command.
FIXTURE_REAL = REPO_ROOT / "tests" / "fixtures" / "context-window.real.json"

SIX_HOURS = 6 * 3600.0
ANCHOR_TS = 1784148148.166149  # the recent PR-comment ping (anchor)


def _msg(ts: float, text: str = "", user: str = "U") -> dict:
    return {"user": user, "ts": f"{ts:.6f}", "text": text}


# ---------------------------------------------------------------------------
# O1 — real-window (synthetic here) fixture: old burst is dropped
# ---------------------------------------------------------------------------

def test_o1_fixture_drops_old_burst_keeps_anchor_day() -> None:
    """The context window at 6h keeps only the anchor-day burst; no old burst."""
    src = FIXTURE_REAL if FIXTURE_REAL.exists() else FIXTURE
    data = json.loads(src.read_text())
    window = data["messages"]
    # Sanity: the synthetic fixture has a known shape (a real window may differ,
    # so only assert shape for the committed synthetic file).
    if src == FIXTURE:
        assert len(window) == 8, f"fixture shape changed: {len(window)} messages"

    trimmed = _segment_context_by_gap(window, SIX_HOURS)

    # None of the older merge-session messages survive. They all fall before
    # the anchor-day boundary epoch.
    boundary = 1784102400.0  # anchor-day start — anything before is a prior day
    for m in trimmed:
        assert float(m["ts"]) >= boundary, (
            f"old/pre-gap message leaked through: ts={m['ts']} "
            f"text={m.get('text', '')[:60]!r}"
        )
    # The earliest kept message is on the anchor's day.
    earliest_ts = float(trimmed[0]["ts"])
    assert earliest_ts >= boundary, "earliest kept message is not on anchor day"
    # The specific older-burst content must be gone.
    joined = " ".join(m.get("text", "") for m in trimmed)
    assert "Merged it" not in joined
    assert "PR-101" not in joined
    # The anchor-day burst content is retained.
    assert any("PR-202" in m.get("text", "") for m in trimmed)


# ---------------------------------------------------------------------------
# O2 — synthetic windows
# ---------------------------------------------------------------------------

def test_o2_five_day_gap_keeps_recent_burst() -> None:
    """[m@T-5d, m@T-5d+2m, m@T-3m, m@T-1m, anchor@T] @6h -> [T-3m, T-1m, anchor]."""
    T = ANCHOR_TS
    window = [
        _msg(T - 5 * 86400, "old-1"),
        _msg(T - 5 * 86400 + 120, "old-2"),
        _msg(T - 180, "recent-1"),
        _msg(T - 60, "recent-2"),
        _msg(T, "anchor"),
    ]
    trimmed = _segment_context_by_gap(window, SIX_HOURS)
    assert [m["text"] for m in trimmed] == ["recent-1", "recent-2", "anchor"]


def test_o2_contiguous_window_unchanged() -> None:
    """All messages < 6h apart -> window returned unchanged (chronological)."""
    T = ANCHOR_TS
    window = [
        _msg(T - 3 * 3600, "a"),
        _msg(T - 2 * 3600, "b"),
        _msg(T - 1 * 3600, "c"),
        _msg(T, "anchor"),
    ]
    trimmed = _segment_context_by_gap(window, SIX_HOURS)
    assert [m["text"] for m in trimmed] == ["a", "b", "c", "anchor"]
    assert len(trimmed) == len(window)


def test_o2_all_prior_beyond_threshold_yields_anchor_only() -> None:
    """Every prior message > 6h before the anchor -> just the anchor (len==1)."""
    T = ANCHOR_TS
    window = [
        _msg(T - 3 * 86400, "old-1"),
        _msg(T - 2 * 86400, "old-2"),
        _msg(T - 7 * 3600, "old-3"),  # 7h before anchor -> severed (>6h)
        _msg(T, "anchor"),
    ]
    trimmed = _segment_context_by_gap(window, SIX_HOURS)
    assert len(trimmed) == 1
    assert trimmed[0]["text"] == "anchor"


# ---------------------------------------------------------------------------
# O1 — invariants: anchor always last, never dropped
# ---------------------------------------------------------------------------

def test_anchor_always_last_and_never_dropped() -> None:
    """Across several shapes, the newest message is always kept and last."""
    T = ANCHOR_TS
    shapes = [
        [_msg(T, "anchor")],                                   # lone anchor
        [_msg(T - 60, "x"), _msg(T, "anchor")],                # tiny burst
        [_msg(T - 10 * 86400, "ancient"), _msg(T, "anchor")],  # long silence
    ]
    for window in shapes:
        trimmed = _segment_context_by_gap(window, SIX_HOURS)
        assert trimmed, "segmentation returned empty"
        assert trimmed[-1]["text"] == "anchor", "anchor is not last"
        assert any(m["text"] == "anchor" for m in trimmed), "anchor dropped"


def test_unsorted_input_is_sorted_before_segmenting() -> None:
    """Out-of-order input is handled: anchor is the max-ts element regardless."""
    T = ANCHOR_TS
    window = [
        _msg(T, "anchor"),
        _msg(T - 60, "recent"),
        _msg(T - 5 * 86400, "old"),
    ]  # deliberately NOT chronological
    trimmed = _segment_context_by_gap(window, SIX_HOURS)
    assert [m["text"] for m in trimmed] == ["recent", "anchor"]


def test_malformed_ts_treated_conservatively() -> None:
    """A message with a bad ts sorts to the far past and is cut by the gap."""
    T = ANCHOR_TS
    window = [
        {"user": "U", "ts": "not-a-number", "text": "malformed"},
        _msg(T - 60, "recent"),
        _msg(T, "anchor"),
    ]
    trimmed = _segment_context_by_gap(window, SIX_HOURS)
    # malformed -> ts 0.0 -> ancient -> severed; recent + anchor kept.
    assert [m["text"] for m in trimmed] == ["recent", "anchor"]


def test_empty_input_returns_empty() -> None:
    assert _segment_context_by_gap([], SIX_HOURS) == []


# ---------------------------------------------------------------------------
# Config-hook resolution
# ---------------------------------------------------------------------------

def test_config_default_when_key_absent() -> None:
    assert _resolve_context_max_gap_seconds({}) == DEFAULT_CONTEXT_MAX_GAP_HOURS * 3600.0
    assert _resolve_context_max_gap_seconds(None) == DEFAULT_CONTEXT_MAX_GAP_HOURS * 3600.0
    assert _resolve_context_max_gap_seconds({"other_key": 1}) == \
        DEFAULT_CONTEXT_MAX_GAP_HOURS * 3600.0


def test_config_honors_override() -> None:
    assert _resolve_context_max_gap_seconds({"context_max_gap_hours": 12}) == 12 * 3600.0
    assert _resolve_context_max_gap_seconds({"context_max_gap_hours": 2}) == 2 * 3600.0
    # string-numeric tolerated
    assert _resolve_context_max_gap_seconds({"context_max_gap_hours": "4"}) == 4 * 3600.0


def test_config_ignores_invalid_override() -> None:
    # non-positive or unparseable -> fall back to default (never disable).
    d = DEFAULT_CONTEXT_MAX_GAP_HOURS * 3600.0
    assert _resolve_context_max_gap_seconds({"context_max_gap_hours": 0}) == d
    assert _resolve_context_max_gap_seconds({"context_max_gap_hours": -5}) == d
    assert _resolve_context_max_gap_seconds({"context_max_gap_hours": "nope"}) == d


def test_override_actually_changes_segmentation() -> None:
    """A 2h override severs a 3h-gap window that 6h would keep."""
    T = ANCHOR_TS
    window = [_msg(T - 3 * 3600, "three-h-back"), _msg(T, "anchor")]
    kept_at_6h = _segment_context_by_gap(window, _resolve_context_max_gap_seconds({}))
    assert [m["text"] for m in kept_at_6h] == ["three-h-back", "anchor"]
    kept_at_2h = _segment_context_by_gap(
        window, _resolve_context_max_gap_seconds({"context_max_gap_hours": 2})
    )
    assert [m["text"] for m in kept_at_2h] == ["anchor"]


# ---------------------------------------------------------------------------
# AC6 — prompt boundary-rule assertion (no copy: builds via the real code path)
# ---------------------------------------------------------------------------

def test_prompt_contains_time_gap_boundary_rule() -> None:
    """The composed opencode prompt names the large-time-gap boundary rule.

    Asserts against the prompt built by the SAME helper _generate_body_via_opencode
    uses (CaptureWorker._build_body_prompt), not a hand-copied string.
    """
    prompt = CaptureWorker._build_body_prompt("{}", "")
    assert "large time gap" in prompt, "boundary rule wording missing"
    assert "DIFFERENT" in prompt, "the 'different conversation' framing is missing"
    # Narrowness guard: it must still instruct keeping adjacent context.
    assert "genuinely-adjacent context" in prompt, \
        "boundary rule must not discourage keeping adjacent context"
    # The instruction points at the ts fields as the gap signal.
    assert "`ts`" in prompt


# ---------------------------------------------------------------------------
# Runner (script mode) — mirrors the repo's plain def-main style.
# ---------------------------------------------------------------------------

def main() -> int:
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failures: list[str] = []
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failures.append(f"{t.__name__}: {e}")
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failures.append(f"{t.__name__}: {e!r}")
            print(f"ERROR {t.__name__}: {e!r}")

    print("\n=== summary")
    if failures:
        print(f"FAILED: {len(failures)}/{len(tests)}")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"PASSED: {len(tests)}/{len(tests)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
