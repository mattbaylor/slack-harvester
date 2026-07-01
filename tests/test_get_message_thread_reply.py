#!/usr/bin/env python3
"""Regression test for the get_message thread-reply substitution bug.

Verifies that SlackClient.get_message returns the correct message for both
top-level messages and thread replies.

Before the fix, get_message used conversations.history which silently
returned an unrelated top-level message when asked for a thread reply's ts
— see the 2026-07-01 bug nugget in the vault:
  ~/vault/00-inbox/2026-07-01-slack-harvester-get-message-wrong-for-thread-replies.md

The fix routed get_message through conversations.replies, which returns the
correct message whether ts is a parent or a reply. A defensive check in
CaptureWorker._process now raises if get_message ever returns the wrong ts,
so this class of failure can't be silent again.

## Usage

Uses live Slack credentials read from the harvester's Chrome profile (same
mechanism as the running harvester). Read-only — no writes to Slack, no
writes to vault, no state mutation beyond the users-cache side effects of
resolve_user (not exercised here).

Config is read from ../config.json relative to this file.

Run:
  cd ~/repo/slack-harvester
  PYTHONPATH=~/.local/lib/slack-harvester-deps python3 tests/test_get_message_thread_reply.py

Exit codes:
  0 — all cases passed
  1 — one or more cases failed
  2 — no Slack credentials available (probably need to sign into Slack in
      the dedicated Chrome profile again)

## Test-case durability

The three ts values below live in a real Slack thread in #proj-graph-ui
(channel C0AS0PPTFM3) on the Banno workspace. They will remain valid as
long as those messages exist in Slack. If they're ever deleted or the
channel is archived past the retention window, this test needs new fixtures
— pick any thread with at least the parent + one reply and update CASES.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Repo root is the parent of tests/.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from harvester import SlackClient, HarvesterState  # noqa: E402

CONFIG = json.loads((REPO_ROOT / "config.json").read_text())

CASES = [
    {
        "label": "thread parent (Ethell / ARTI-298 bug ticket)",
        "channel": "C0AS0PPTFM3",
        "ts": "1782852023.839919",
        "expect_text_contains": "Add this bug ticket related to dupe component registration",
        "expect_is_reply": False,
    },
    {
        "label": "thread reply #1 (John — Josh Langner rationale)",
        "channel": "C0AS0PPTFM3",
        "ts": "1782925533.567219",
        "expect_text_contains": "Josh Langner",
        "expect_is_reply": True,
        "expect_thread_ts": "1782852023.839919",
    },
    {
        "label": "thread reply #2 (David Ethell — 'no other types of import')",
        "channel": "C0AS0PPTFM3",
        "ts": "1782926787.843129",
        "expect_text_contains": "other types of impor",
        "expect_is_reply": True,
        "expect_thread_ts": "1782852023.839919",
    },
]


def main() -> int:
    state = HarvesterState(
        vault=Path(CONFIG["vault_path"]).expanduser(),
        capture_dir=CONFIG["capture_dir"],
        chrome_profile=Path(CONFIG["chrome_profile"]).expanduser(),
        state_dir=Path(CONFIG["state_dir"]).expanduser(),
    )
    if not state.has_credentials():
        print("FAIL: no Slack credentials from Chrome profile", file=sys.stderr)
        print("      sign into Slack in the dedicated Chrome profile and retry.",
              file=sys.stderr)
        return 2

    client = SlackClient(state)

    failures = []
    for case in CASES:
        print(f"\n--- {case['label']}")
        print(f"    channel={case['channel']} ts={case['ts']}")
        try:
            msg = client.get_message(case["channel"], case["ts"])
        except Exception as e:
            failures.append(f"{case['label']}: get_message raised {e!r}")
            print(f"    FAIL: {e}")
            continue

        got_ts = msg.get("ts")
        got_text = (msg.get("text") or "")[:120]
        got_thread_ts = msg.get("thread_ts")
        got_user = msg.get("user")

        print(f"    got: ts={got_ts} thread_ts={got_thread_ts} user={got_user}")
        print(f"    text[:120]={got_text!r}")

        if got_ts != case["ts"]:
            failures.append(
                f"{case['label']}: ts mismatch — asked {case['ts']}, got {got_ts}"
            )
            print(f"    FAIL: ts mismatch")
            continue

        if case["expect_text_contains"] not in (msg.get("text") or ""):
            failures.append(
                f"{case['label']}: expected text to contain "
                f"{case['expect_text_contains']!r}, got {got_text!r}"
            )
            print(f"    FAIL: text mismatch")
            continue

        if case["expect_is_reply"]:
            if got_thread_ts != case.get("expect_thread_ts"):
                failures.append(
                    f"{case['label']}: expected thread_ts="
                    f"{case.get('expect_thread_ts')!r}, got {got_thread_ts!r}"
                )
                print(f"    FAIL: thread_ts mismatch")
                continue
        else:
            # Top-level messages: Slack sets thread_ts == ts on the parent
            # once at least one reply exists. Either equal to ts, or None,
            # is OK for a parent.
            if got_thread_ts is not None and got_thread_ts != case["ts"]:
                failures.append(
                    f"{case['label']}: expected no thread_ts or thread_ts=={case['ts']!r}, "
                    f"got {got_thread_ts!r}"
                )
                print(f"    FAIL: parent has unexpected thread_ts")
                continue

        print(f"    PASS")

    print("\n=== summary")
    if failures:
        print(f"FAILED: {len(failures)}")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"PASSED: {len(CASES)} cases")
    return 0


if __name__ == "__main__":
    sys.exit(main())
