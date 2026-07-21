#!/usr/bin/env python3
"""Hermetic tests for the POST /creds push-ingest handler (ISSUES.md #17).

Like test_slack_proxy_seam.py, these tests do NOT bind a socket, do NOT touch
live Slack, and do NOT read Chrome. They exercise the real pure handler
`handle_creds_ingest` — the same function HarvestHandler.do_POST calls — with
an injected expected_token and raw byte bodies.

Coverage:
  - auth: valid bearer + valid body -> 200 and (token, cookie) returned;
    missing bearer -> 401, no creds; wrong bearer -> 401, no creds.
  - body: malformed JSON -> 400, no creds; empty body -> 400, no creds;
    missing token or cookie -> 400, no creds; blank token/cookie -> 400;
    non-string token/cookie -> 400; non-object JSON -> 400.
  - leak guard: the token/cookie values NEVER appear in the returned body dict
    (only the opaque {ok:true} on success / {ok:false,error:...} on failure).

Run:
  cd ~/repo/slack-harvester
  python3 tests/test_creds_ingest.py

Exit codes:
  0 — all cases passed
  1 — one or more cases failed
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Repo root is the parent of tests/.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from harvester import handle_creds_ingest  # noqa: E402

GOOD_TOKEN = "correct-horse-battery-staple-0123456789abcdef"
# Realistic-looking secret values. The tests assert these never appear in any
# returned body dict — the ingest hands data to state, never echoes it.
SLACK_TOKEN = "xoxc-SECRET-TOKEN-SHOULD-NEVER-LEAK-abcdef0123456789"
SLACK_COOKIE = "d-SECRET-COOKIE-SHOULD-NEVER-LEAK-9876543210fedcba"


def _check(failures: list, label: str, cond: bool, detail: str = ""):
    if cond:
        print(f"    PASS: {label}")
    else:
        msg = f"{label}" + (f" — {detail}" if detail else "")
        failures.append(msg)
        print(f"    FAIL: {msg}")


def _body(token=SLACK_TOKEN, cookie=SLACK_COOKIE) -> bytes:
    payload = {}
    if token is not None:
        payload["token"] = token
    if cookie is not None:
        payload["cookie"] = cookie
    return json.dumps(payload).encode("utf-8")


def _assert_no_leak(failures: list, label: str, body: dict):
    serialized = repr(body).lower()
    _check(failures, f"{label}: no slack token value in body",
           SLACK_TOKEN.lower() not in serialized, f"body={body}")
    _check(failures, f"{label}: no slack cookie value in body",
           SLACK_COOKIE.lower() not in serialized, f"body={body}")


# ---------------------------------------------------------------------------
# Auth gate
# ---------------------------------------------------------------------------

def test_auth(failures: list) -> None:
    print("\n--- creds ingest auth gate (401 sets nothing)")

    # Valid bearer + valid body -> 200 + creds returned.
    status, body, creds = handle_creds_ingest(
        auth_header=f"Bearer {GOOD_TOKEN}", expected_token=GOOD_TOKEN,
        raw_body=_body())
    _check(failures, "valid auth+body -> 200", status == 200, f"got {status}")
    _check(failures, "valid -> ok:true", body.get("ok") is True, f"body={body}")
    _check(failures, "valid returns (token, cookie)",
           creds == (SLACK_TOKEN, SLACK_COOKIE), f"creds={creds!r}")
    _assert_no_leak(failures, "valid", body)

    # Missing header -> 401, no creds.
    status, body, creds = handle_creds_ingest(
        auth_header=None, expected_token=GOOD_TOKEN, raw_body=_body())
    _check(failures, "missing bearer -> 401", status == 401, f"got {status}")
    _check(failures, "missing bearer sets no creds", creds is None,
           f"creds={creds!r}")
    _check(failures, "401 body ok:false", body.get("ok") is False)

    # Wrong header -> 401, no creds.
    status, body, creds = handle_creds_ingest(
        auth_header="Bearer nope", expected_token=GOOD_TOKEN, raw_body=_body())
    _check(failures, "wrong bearer -> 401", status == 401, f"got {status}")
    _check(failures, "wrong bearer sets no creds", creds is None,
           f"creds={creds!r}")

    # No expected token configured -> reject everything.
    status, body, creds = handle_creds_ingest(
        auth_header=f"Bearer {GOOD_TOKEN}", expected_token=None,
        raw_body=_body())
    _check(failures, "no expected token -> 401", status == 401, f"got {status}")
    _check(failures, "no expected token sets no creds", creds is None)

    # Auth failure must NOT even peek at a (valid) body — still no leak.
    _assert_no_leak(failures, "401", body)


# ---------------------------------------------------------------------------
# Body validation
# ---------------------------------------------------------------------------

def test_body(failures: list) -> None:
    print("\n--- creds ingest body validation (400 sets nothing)")
    auth = f"Bearer {GOOD_TOKEN}"

    # Malformed JSON.
    status, body, creds = handle_creds_ingest(
        auth_header=auth, expected_token=GOOD_TOKEN, raw_body=b"{not json")
    _check(failures, "malformed JSON -> 400", status == 400, f"got {status}")
    _check(failures, "malformed sets no creds", creds is None)

    # Empty body.
    status, body, creds = handle_creds_ingest(
        auth_header=auth, expected_token=GOOD_TOKEN, raw_body=b"")
    _check(failures, "empty body -> 400", status == 400, f"got {status}")
    _check(failures, "empty body sets no creds", creds is None)

    # Non-object JSON (array / scalar).
    for bad in (b"[]", b'"just a string"', b"42", b"null"):
        status, body, creds = handle_creds_ingest(
            auth_header=auth, expected_token=GOOD_TOKEN, raw_body=bad)
        _check(failures, f"non-object {bad!r} -> 400", status == 400,
               f"got {status}")
        _check(failures, f"non-object {bad!r} sets no creds", creds is None)

    # Missing token.
    status, body, creds = handle_creds_ingest(
        auth_header=auth, expected_token=GOOD_TOKEN,
        raw_body=_body(token=None))
    _check(failures, "missing token -> 400", status == 400, f"got {status}")
    _check(failures, "missing token sets no creds", creds is None)

    # Missing cookie.
    status, body, creds = handle_creds_ingest(
        auth_header=auth, expected_token=GOOD_TOKEN,
        raw_body=_body(cookie=None))
    _check(failures, "missing cookie -> 400", status == 400, f"got {status}")
    _check(failures, "missing cookie sets no creds", creds is None)

    # Blank token/cookie.
    status, body, creds = handle_creds_ingest(
        auth_header=auth, expected_token=GOOD_TOKEN,
        raw_body=_body(token="", cookie=SLACK_COOKIE))
    _check(failures, "blank token -> 400", status == 400, f"got {status}")
    _check(failures, "blank token sets no creds", creds is None)

    status, body, creds = handle_creds_ingest(
        auth_header=auth, expected_token=GOOD_TOKEN,
        raw_body=_body(token=SLACK_TOKEN, cookie=""))
    _check(failures, "blank cookie -> 400", status == 400, f"got {status}")
    _check(failures, "blank cookie sets no creds", creds is None)

    # Non-string token (number).
    status, body, creds = handle_creds_ingest(
        auth_header=auth, expected_token=GOOD_TOKEN,
        raw_body=json.dumps({"token": 123, "cookie": SLACK_COOKIE}).encode())
    _check(failures, "non-string token -> 400", status == 400, f"got {status}")
    _check(failures, "non-string token sets no creds", creds is None)

    # Every failure body must stay opaque (no leak — though these bodies didn't
    # carry the real values anyway, assert the invariant holds).
    _assert_no_leak(failures, "400", body)


# ---------------------------------------------------------------------------
# Extra fields tolerated (forward-compat: extension may add metadata)
# ---------------------------------------------------------------------------

def test_extra_fields(failures: list) -> None:
    print("\n--- creds ingest tolerates extra fields")
    auth = f"Bearer {GOOD_TOKEN}"
    raw = json.dumps({
        "token": SLACK_TOKEN, "cookie": SLACK_COOKIE,
        "pushedAt": "2026-07-21T13:00:00Z", "source": "extension",
    }).encode()
    status, body, creds = handle_creds_ingest(
        auth_header=auth, expected_token=GOOD_TOKEN, raw_body=raw)
    _check(failures, "extra fields still -> 200", status == 200, f"got {status}")
    _check(failures, "extra fields still returns creds",
           creds == (SLACK_TOKEN, SLACK_COOKIE), f"creds={creds!r}")


def main() -> int:
    failures: list[str] = []
    test_auth(failures)
    test_body(failures)
    test_extra_fields(failures)

    print("\n=== summary")
    if failures:
        print(f"FAILED: {len(failures)}")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("PASSED: all checks green (hermetic — no socket, no network, no Chrome)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
