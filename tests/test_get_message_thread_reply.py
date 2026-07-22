#!/usr/bin/env python3
"""Hermetic regression test for the get_message thread-reply substitution bug.

Verifies that SlackClient.get_message returns the correct message for a
top-level message, a thread reply, and a non-threaded message.

Before the fix, get_message used conversations.history, which silently
returned an unrelated top-level message when asked for a thread reply's ts.
The fix routed get_message through conversations.replies, which returns the
correct message whether ts is a parent or a reply; get_message then selects
the element whose ts matches exactly. A defensive check in
CaptureWorker._process now raises if get_message ever returns the wrong ts,
so this class of failure can't be silent again.

FULLY HERMETIC: no live Slack, no opencode, no Chrome, no network, no
credentials. It stubs SlackClient._call (the single API seam get_message
uses) with a synthetic thread and asserts the selection logic directly.

Run (from repo root):
    python3 tests/test_get_message_thread_reply.py
or with pytest:
    pytest tests/test_get_message_thread_reply.py

Exit codes (script mode): 0 all pass, 1 one or more failures.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from harvester import SlackClient  # noqa: E402

CHANNEL = "C0000000000"
PARENT_TS = "1700000000.000100"
REPLY1_TS = "1700000100.000200"
REPLY2_TS = "1700000200.000300"
LONE_TS = "1700000300.000400"

# A synthetic thread as conversations.replies would return it: parent first,
# then replies in chronological order.
THREAD = [
    {"ts": PARENT_TS, "user": "U_AUTHOR", "text": "parent: please look at this"},
    {"ts": REPLY1_TS, "user": "U_ONE", "thread_ts": PARENT_TS,
     "text": "reply one with the rationale"},
    {"ts": REPLY2_TS, "user": "U_TWO", "thread_ts": PARENT_TS,
     "text": "reply two: no other cases to consider"},
]

# A non-threaded top-level message: conversations.replies returns a single
# element with no thread_ts.
LONE = [{"ts": LONE_TS, "user": "U_AUTHOR", "text": "a standalone message"}]


def _make_client(messages: list[dict]) -> SlackClient:
    """A SlackClient whose only API seam (_call) returns a fixed message list.

    get_message calls self._call("conversations.replies", ...); we stub that
    so no network/credentials are needed and the selection logic is isolated.
    """
    client = SlackClient.__new__(SlackClient)  # bypass __init__ (needs state)

    def fake_call(method: str, params: dict) -> dict:
        assert method == "conversations.replies", (
            f"get_message must route through conversations.replies, got {method}"
        )
        return {"ok": True, "messages": messages}

    client._call = fake_call  # type: ignore[method-assign]
    return client


CASES = [
    {
        "label": "thread parent",
        "messages": THREAD,
        "ts": PARENT_TS,
        "expect_text_contains": "please look at this",
        "expect_is_reply": False,
    },
    {
        "label": "thread reply #1",
        "messages": THREAD,
        "ts": REPLY1_TS,
        "expect_text_contains": "the rationale",
        "expect_is_reply": True,
        "expect_thread_ts": PARENT_TS,
    },
    {
        "label": "thread reply #2",
        "messages": THREAD,
        "ts": REPLY2_TS,
        "expect_text_contains": "no other cases",
        "expect_is_reply": True,
        "expect_thread_ts": PARENT_TS,
    },
    {
        "label": "non-threaded top-level message",
        "messages": LONE,
        "ts": LONE_TS,
        "expect_text_contains": "standalone",
        "expect_is_reply": False,
    },
]


def _run_case(case: dict) -> str | None:
    """Return None on pass, or a failure string on failure."""
    client = _make_client(case["messages"])
    try:
        msg = client.get_message(CHANNEL, case["ts"])
    except Exception as e:  # noqa: BLE001
        return f"{case['label']}: get_message raised {e!r}"

    got_ts = msg.get("ts")
    got_thread_ts = msg.get("thread_ts")

    if got_ts != case["ts"]:
        return f"{case['label']}: ts mismatch — asked {case['ts']}, got {got_ts}"

    if case["expect_text_contains"] not in (msg.get("text") or ""):
        return (f"{case['label']}: expected text to contain "
                f"{case['expect_text_contains']!r}, got {(msg.get('text') or '')[:80]!r}")

    if case["expect_is_reply"]:
        if got_thread_ts != case.get("expect_thread_ts"):
            return (f"{case['label']}: expected thread_ts="
                    f"{case.get('expect_thread_ts')!r}, got {got_thread_ts!r}")
    else:
        if got_thread_ts is not None and got_thread_ts != case["ts"]:
            return (f"{case['label']}: expected no thread_ts or thread_ts=="
                    f"{case['ts']!r}, got {got_thread_ts!r}")
    return None


# Individual pytest-discoverable tests -------------------------------------

def test_thread_parent() -> None:
    assert _run_case(CASES[0]) is None


def test_thread_reply_1() -> None:
    assert _run_case(CASES[1]) is None


def test_thread_reply_2() -> None:
    assert _run_case(CASES[2]) is None


def test_non_threaded_message() -> None:
    assert _run_case(CASES[3]) is None


def test_missing_ts_raises() -> None:
    """Asking for a ts not in the returned thread raises, never substitutes."""
    client = _make_client(THREAD)
    raised = False
    try:
        client.get_message(CHANNEL, "1699999999.999999")
    except RuntimeError:
        raised = True
    assert raised, "get_message must raise for an absent ts, not return a neighbor"


def main() -> int:
    failures = []
    for case in CASES:
        print(f"\n--- {case['label']}")
        print(f"    channel={CHANNEL} ts={case['ts']}")
        err = _run_case(case)
        if err:
            failures.append(err)
            print(f"    FAIL: {err}")
        else:
            print("    PASS")

    print("\n--- absent ts raises")
    try:
        test_missing_ts_raises()
        print("    PASS")
    except AssertionError as e:
        failures.append(f"absent-ts: {e}")
        print(f"    FAIL: {e}")

    print("\n=== summary")
    if failures:
        print(f"FAILED: {len(failures)}")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"PASSED: {len(CASES) + 1} cases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
