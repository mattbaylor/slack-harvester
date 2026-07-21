#!/usr/bin/env python3
"""Hermetic unit tests for the reaction reconciler (ISSUES #16).

FULLY HERMETIC: no live Slack, no opencode, no Chrome, no network, no
credentials. Exercises the reconciler's PURE logic directly —

  - message_has_my_trigger_reaction  (does this message carry MY trigger?)
  - select_new_and_mine              (which messages in a batch are new AND mine?)
  - compute_new_watermark            (watermark advance math)
  - reconcile_dedup_key              (parity with the poller's channel:ts key)

plus a fake-injected end-to-end pass of Reconciler._reconcile_channel to prove
the enqueue + is_seen skip + watermark advance wiring, with a fake SlackClient /
CaptureWorker / state so nothing touches the network.

Bug being guarded: the poller depended SOLELY on Slack's `hasmy::<emoji>:`
search filter, whose index wedged for ~40h (2026-07-15→17) and silently froze
captures. The reconciler is the independent fallback via conversations.history;
these tests pin its match/dedup/watermark correctness and its dedup-key parity
with the primary path (shared seen.json → no duplicates).

Run (from repo root):
    python3 tests/test_reconciler.py
or with pytest:
    pytest tests/test_reconciler.py

Exit codes (script mode): 0 all pass, 1 one or more failures.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from harvester import (  # noqa: E402
    DEFAULT_RECONCILE_INTERVAL_MINUTES,
    DEFAULT_RECONCILE_WINDOW_HOURS,
    Reconciler,
    _resolve_reconcile_interval_seconds,
    _resolve_reconcile_window_seconds,
    compute_new_watermark,
    message_has_my_trigger_reaction,
    reconcile_dedup_key,
    select_new_and_mine,
)

ME = "U0ATR90VBMJ"          # the authed user id (Matt), per the plan
OTHER = "U9999OTHER"
TRIGGERS = {"eyes", "cap", "bookmark"}


def _msg(ts: float, reactions=None, text: str = "") -> dict:
    m = {"ts": f"{ts:.6f}", "text": text}
    if reactions is not None:
        m["reactions"] = reactions
    return m


def _reaction(name: str, users: list) -> dict:
    return {"name": name, "users": users, "count": len(users)}


# ---------------------------------------------------------------------------
# message_has_my_trigger_reaction — the core match rule (AC4)
# ---------------------------------------------------------------------------

def test_match_my_trigger_reaction_true() -> None:
    """My trigger emoji, my id in users[] → match."""
    m = _msg(100.0, [_reaction("eyes", [ME])])
    assert message_has_my_trigger_reaction(m, ME, TRIGGERS) is True


def test_match_ignores_others_reactions() -> None:
    """A trigger emoji reacted by SOMEONE ELSE (not me) → no match."""
    m = _msg(100.0, [_reaction("eyes", [OTHER])])
    assert message_has_my_trigger_reaction(m, ME, TRIGGERS) is False


def test_match_ignores_my_non_trigger_reaction() -> None:
    """My reaction, but a NON-trigger emoji → no match."""
    m = _msg(100.0, [_reaction("thumbsup", [ME])])
    assert message_has_my_trigger_reaction(m, ME, TRIGGERS) is False


def test_match_mixed_reactions_picks_mine() -> None:
    """Mixed: someone else's trigger + my trigger on same message → match."""
    m = _msg(100.0, [
        _reaction("eyes", [OTHER]),         # not mine
        _reaction("cap", [OTHER, ME]),      # mine (I'm in users[])
    ])
    assert message_has_my_trigger_reaction(m, ME, TRIGGERS) is True


def test_match_no_reactions_field() -> None:
    """A message with no reactions[] at all → no match, no crash."""
    assert message_has_my_trigger_reaction(_msg(100.0), ME, TRIGGERS) is False
    assert message_has_my_trigger_reaction(
        _msg(100.0, reactions=[]), ME, TRIGGERS) is False


def test_match_strips_skin_tone_suffix() -> None:
    """A trigger emoji with a skin-tone modifier still matches on the base name."""
    m = _msg(100.0, [_reaction("eyes::skin-tone-3", [ME])])
    assert message_has_my_trigger_reaction(m, ME, TRIGGERS) is True


def test_match_empty_authed_id_is_false() -> None:
    """No authed id → never match (defensive; before user_id is discovered)."""
    m = _msg(100.0, [_reaction("eyes", [ME])])
    assert message_has_my_trigger_reaction(m, "", TRIGGERS) is False


def test_match_malformed_reaction_entries_skipped() -> None:
    """Non-dict / missing-name reaction entries don't crash; still find the good one."""
    m = _msg(100.0, [
        "not-a-dict",
        {"users": [ME]},                    # no name
        {"name": 123, "users": [ME]},       # non-str name
        _reaction("cap", [ME]),             # the real one
    ])
    assert message_has_my_trigger_reaction(m, ME, TRIGGERS) is True


# ---------------------------------------------------------------------------
# select_new_and_mine — new AND mine filtering (AC8c)
# ---------------------------------------------------------------------------

def test_select_new_and_mine_basic() -> None:
    """Only messages newer than the watermark AND carrying my trigger are kept."""
    batch = [
        _msg(90.0, [_reaction("eyes", [ME])]),      # old (<= watermark) → drop
        _msg(110.0, [_reaction("eyes", [ME])]),     # new + mine → keep
        _msg(120.0, [_reaction("eyes", [OTHER])]),  # new but not mine → drop
        _msg(130.0, [_reaction("thumbsup", [ME])]), # new + mine but non-trigger → drop
        _msg(140.0, [_reaction("cap", [ME])]),      # new + mine → keep
    ]
    got = select_new_and_mine(batch, ME, TRIGGERS, oldest_ts=100.0)
    kept_ts = [m["ts"] for m in got]
    assert kept_ts == ["110.000000", "140.000000"], kept_ts


def test_select_strict_watermark_excludes_equal_ts() -> None:
    """A message EXACTLY at the watermark is not re-selected (second-cycle no-op)."""
    batch = [_msg(100.0, [_reaction("eyes", [ME])])]
    assert select_new_and_mine(batch, ME, TRIGGERS, oldest_ts=100.0) == []


def test_select_first_scan_no_watermark_keeps_all_mine() -> None:
    """oldest_ts=None (first scan): no lower bound here; keep all my triggers."""
    batch = [
        _msg(10.0, [_reaction("eyes", [ME])]),
        _msg(20.0, [_reaction("eyes", [OTHER])]),
        _msg(30.0, [_reaction("cap", [ME])]),
    ]
    got = select_new_and_mine(batch, ME, TRIGGERS, oldest_ts=None)
    assert [m["ts"] for m in got] == ["10.000000", "30.000000"]


def test_select_malformed_ts_skipped() -> None:
    """A message with a missing/malformed ts is skipped (can't build a dedup key)."""
    batch = [
        {"reactions": [_reaction("eyes", [ME])]},          # no ts
        {"ts": "not-a-float", "reactions": [_reaction("eyes", [ME])]},
        _msg(110.0, [_reaction("eyes", [ME])]),            # good
    ]
    got = select_new_and_mine(batch, ME, TRIGGERS, oldest_ts=100.0)
    assert [m["ts"] for m in got] == ["110.000000"]


# ---------------------------------------------------------------------------
# compute_new_watermark — advance math (AC8b)
# ---------------------------------------------------------------------------

def test_watermark_advances_to_newest_scanned() -> None:
    """Watermark advances to the newest message SCANNED, not just matched."""
    batch = [
        _msg(110.0, [_reaction("thumbsup", [ME])]),  # non-trigger, still scanned
        _msg(150.0, [_reaction("eyes", [OTHER])]),   # newest, not mine, still scanned
    ]
    assert compute_new_watermark(batch, current_watermark=100.0) == 150.0


def test_watermark_never_regresses() -> None:
    """An older batch (all ts < watermark) does NOT move the watermark back."""
    batch = [_msg(50.0), _msg(60.0)]
    assert compute_new_watermark(batch, current_watermark=100.0) == 100.0


def test_watermark_empty_batch_unchanged() -> None:
    """Empty batch → watermark unchanged (None stays None, value stays value)."""
    assert compute_new_watermark([], current_watermark=None) is None
    assert compute_new_watermark([], current_watermark=100.0) == 100.0


def test_watermark_first_scan_from_none() -> None:
    """First scan (watermark None) sets it to the newest ts in the batch."""
    batch = [_msg(10.0), _msg(30.0), _msg(20.0)]
    assert compute_new_watermark(batch, current_watermark=None) == 30.0


def test_watermark_ignores_malformed_ts() -> None:
    """Malformed ts entries don't corrupt the advance."""
    batch = [{"ts": "bad"}, _msg(120.0), {"no": "ts"}]
    assert compute_new_watermark(batch, current_watermark=100.0) == 120.0


# ---------------------------------------------------------------------------
# reconcile_dedup_key — parity with the poller (AC2)
# ---------------------------------------------------------------------------

def test_dedup_key_shape() -> None:
    assert reconcile_dedup_key("C123", "1784148148.166149") == "C123:1784148148.166149"


def test_dedup_key_parity_with_poller() -> None:
    """The reconciler's dedup key MUST be byte-identical to the poller's.

    The poller builds `f"{channel}:{ts}"` inline (ReactionPoller._poll). If that
    shape ever diverges from reconcile_dedup_key, the two paths would use
    different keys against the shared seen.json and could double-capture. This
    asserts the exact string the poller would produce equals the factored helper.
    """
    channel, ts = "C0ABC", "1784148148.166149"
    poller_key = f"{channel}:{ts}"          # the literal poller expression
    assert reconcile_dedup_key(channel, ts) == poller_key


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------

def test_resolve_interval_default_and_override() -> None:
    assert _resolve_reconcile_interval_seconds(None) == \
        DEFAULT_RECONCILE_INTERVAL_MINUTES * 60.0
    assert _resolve_reconcile_interval_seconds({}) == \
        DEFAULT_RECONCILE_INTERVAL_MINUTES * 60.0
    assert _resolve_reconcile_interval_seconds(
        {"reconcile_interval_minutes": 10}) == 600.0
    # invalid / non-positive → default
    assert _resolve_reconcile_interval_seconds(
        {"reconcile_interval_minutes": 0}) == DEFAULT_RECONCILE_INTERVAL_MINUTES * 60.0
    assert _resolve_reconcile_interval_seconds(
        {"reconcile_interval_minutes": "nope"}) == \
        DEFAULT_RECONCILE_INTERVAL_MINUTES * 60.0


def test_resolve_window_default_and_override() -> None:
    assert _resolve_reconcile_window_seconds(None) == \
        DEFAULT_RECONCILE_WINDOW_HOURS * 3600.0
    assert _resolve_reconcile_window_seconds(
        {"reconcile_window_hours": 24}) == 24 * 3600.0
    assert _resolve_reconcile_window_seconds(
        {"reconcile_window_hours": -1}) == DEFAULT_RECONCILE_WINDOW_HOURS * 3600.0


# ---------------------------------------------------------------------------
# Fake-injected end-to-end: _reconcile_channel wiring (AC1/AC2/AC3)
# ---------------------------------------------------------------------------

class _FakeState:
    """Minimal stand-in for HarvesterState: seen ledger + watermark map, no I/O."""

    def __init__(self, config=None):
        self.config = config or {}
        self.seen: dict = {}
        self.watermarks: dict = {}

    # seen ledger (shared-dedup surface)
    def is_seen(self, key: str) -> bool:
        return key in self.seen

    def mark_seen(self, key: str):
        self.seen[key] = "marked"

    # watermark persistence
    def get_reconcile_watermark(self, channel):
        return self.watermarks.get(channel)

    def set_reconcile_watermark(self, channel, ts):
        self.watermarks[channel] = ts

    # config-resolution helpers read these via Reconciler.__init__
    def has_credentials(self) -> bool:
        return True


class _FakeClient:
    """Returns a scripted conversations.history batch; records calls."""

    def __init__(self, batch):
        self._batch = batch
        self.scan_calls = []

    def scan_channel_reactions(self, channel, oldest_ts, window_seconds, limit=200):
        self.scan_calls.append((channel, oldest_ts, window_seconds))
        return self._batch


class _FakeWorker:
    def __init__(self):
        self.enqueued = []

    def enqueue(self, event):
        self.enqueued.append(event)


def _make_reconciler(state, client, worker):
    r = Reconciler(state, client, worker, reactions=list(TRIGGERS))
    r.user_id = ME     # skip get_authed_user (that's the poll-time discovery)
    return r


def test_reconcile_channel_enqueues_new_and_advances_watermark() -> None:
    """A new trigger reaction is enqueued with the shared dedup key + marked seen,
    and the watermark advances to the newest scanned ts."""
    batch = [
        _msg(110.0, [_reaction("eyes", [ME])], text="capture me"),
        _msg(120.0, [_reaction("thumbsup", [ME])]),   # non-trigger; advances wm only
    ]
    state, client, worker = _FakeState(), _FakeClient(batch), _FakeWorker()
    r = _make_reconciler(state, client, worker)

    n = r._reconcile_channel("C1")

    assert n == 1, "exactly one new capture"
    assert worker.enqueued == [
        {"channel": "C1", "ts": "110.000000", "workspace_domain": "app.slack.com"}
    ]
    # dedup key marked seen — the SAME key the poller would use.
    assert state.is_seen("C1:110.000000")
    assert state.is_seen(reconcile_dedup_key("C1", "110.000000"))
    # watermark advanced to newest SCANNED (120), not newest matched (110).
    assert state.watermarks["C1"] == 120.0


def test_reconcile_skips_already_seen_no_double_capture() -> None:
    """If the primary path already marked this dedup key seen, the reconciler
    does NOT re-enqueue it (O2 — shared seen.json prevents duplicates)."""
    batch = [_msg(110.0, [_reaction("eyes", [ME])])]
    state, client, worker = _FakeState(), _FakeClient(batch), _FakeWorker()
    state.mark_seen("C1:110.000000")   # pretend the poller already captured it
    r = _make_reconciler(state, client, worker)

    n = r._reconcile_channel("C1")

    assert n == 0, "already-seen reaction must not be re-captured"
    assert worker.enqueued == []
    # watermark still advances (we scanned it) so we don't re-scan next cycle.
    assert state.watermarks["C1"] == 110.0


def test_reconcile_second_cycle_is_noop() -> None:
    """A second immediate cycle over the same batch enqueues nothing new
    (watermark from cycle 1 excludes the same messages)."""
    batch = [_msg(110.0, [_reaction("eyes", [ME])])]
    state, client, worker = _FakeState(), _FakeClient(batch), _FakeWorker()
    r = _make_reconciler(state, client, worker)

    first = r._reconcile_channel("C1")
    # Cycle 2 scans with the advanced watermark; the batch's only msg is at the
    # watermark now, so select_new_and_mine (strict >) drops it.
    second = r._reconcile_channel("C1")

    assert first == 1
    assert second == 0, "second immediate cycle must be a no-op"
    assert len(worker.enqueued) == 1
    # cycle 2 called scan with the advanced watermark (110.0), not None.
    assert client.scan_calls[1][1] == 110.0


def test_reconcile_first_scan_uses_window_not_watermark() -> None:
    """A channel's first scan passes oldest_ts=None (window bound applies)."""
    batch = [_msg(110.0, [_reaction("eyes", [ME])])]
    state, client, worker = _FakeState(), _FakeClient(batch), _FakeWorker()
    r = _make_reconciler(state, client, worker)

    r._reconcile_channel("C1")

    # first scan_channel_reactions call had oldest_ts=None → the client uses the
    # window internally (verified in the real client; here we assert the None).
    assert client.scan_calls[0][1] is None


# ---------------------------------------------------------------------------
# Graceful degradation of channel enumeration (AC9 / SQ3)
# ---------------------------------------------------------------------------

class _RaisingClient:
    def __init__(self, override_batch=None):
        self.override_batch = override_batch or []

    def list_conversations(self, max_pages=10):
        raise RuntimeError("Slack API error: not_allowed_token_type")

    def scan_channel_reactions(self, channel, oldest_ts, window_seconds, limit=200):
        return self.override_batch


def test_channels_to_scan_falls_back_to_config_list() -> None:
    """users.conversations failure → fall back to reconcile_channels, no crash."""
    state = _FakeState(config={"reconcile_channels": ["Cfallback1", "Cfallback2"]})
    r = Reconciler(state, _RaisingClient(), _FakeWorker(), reactions=list(TRIGGERS))
    r.user_id = ME

    channels = r._channels_to_scan()
    assert channels == ["Cfallback1", "Cfallback2"]


def test_channels_to_scan_no_fallback_returns_empty() -> None:
    """users.conversations failure with NO configured fallback → empty (no crash)."""
    state = _FakeState(config={})   # no reconcile_channels
    r = Reconciler(state, _RaisingClient(), _FakeWorker(), reactions=list(TRIGGERS))
    r.user_id = ME
    assert r._channels_to_scan() == []


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
