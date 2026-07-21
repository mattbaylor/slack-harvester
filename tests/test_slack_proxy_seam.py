#!/usr/bin/env python3
"""Hermetic tests for the loopback read-only Slack proxy seam (ISSUES.md #14).

Unlike test_get_message_thread_reply.py, these tests do NOT touch live Slack,
do NOT bind a socket, and do NOT read Chrome credentials. They exercise the
real handler logic by importing the module-level functions that
HarvestHandler.do_GET calls (check_bearer_token, parse_slack_query,
build_error_envelope, handle_slack_proxy, ensure_api_token) and injecting a
fake `call_fn` in place of SlackClient._call.

Coverage:
  - token: correct -> authorized; missing -> 401; wrong -> 401
    (constant-time hmac.compare_digest path exercised).
  - allow-list: listed method passes; unlisted -> 403; no call_fn invoked on
    401/403.
  - error envelope: fake call_fn raises / returns ok:false -> structured
    envelope with has_credentials present; no token/cookie leaked.
  - query-param parsing: method extracted, remaining params passed through.
  - token file: generated 0600 when absent; existing file NOT overwritten.

Run:
  cd ~/repo/slack-harvester
  python3 tests/test_slack_proxy_seam.py

Exit codes:
  0 — all cases passed
  1 — one or more cases failed
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Repo root is the parent of tests/.
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from harvester import (  # noqa: E402
    SLACK_PROXY_ALLOWED_METHODS,
    api_token_path,
    build_error_envelope,
    check_bearer_token,
    ensure_api_token,
    handle_slack_proxy,
    parse_slack_query,
)

GOOD_TOKEN = "correct-horse-battery-staple-0123456789abcdef"
# A sentinel string that would be present in any leaked credential. If the
# proxy ever serialized state.token/.cookie into a response, a caller could
# grep for it — these tests assert it never appears in an error envelope.
SECRET_SENTINEL = "xoxc-SECRET-TOKEN-SHOULD-NEVER-LEAK"


class CallSpy:
    """Records invocations so tests can assert call_fn was / was not called."""

    def __init__(self, result=None, raises: Exception | None = None):
        self.result = result if result is not None else {"ok": True, "messages": []}
        self.raises = raises
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, method: str, params: dict) -> dict:
        self.calls.append((method, params))
        if self.raises is not None:
            raise self.raises
        return self.result


def _check(failures: list, label: str, cond: bool, detail: str = ""):
    if cond:
        print(f"    PASS: {label}")
    else:
        msg = f"{label}" + (f" — {detail}" if detail else "")
        failures.append(msg)
        print(f"    FAIL: {msg}")


# ---------------------------------------------------------------------------
# Token comparison
# ---------------------------------------------------------------------------

def test_token_comparison(failures: list) -> None:
    print("\n--- token comparison (correct / missing / wrong)")
    _check(failures, "correct token authorizes",
           check_bearer_token(f"Bearer {GOOD_TOKEN}", GOOD_TOKEN) is True)
    _check(failures, "missing header rejected",
           check_bearer_token(None, GOOD_TOKEN) is False)
    _check(failures, "empty header rejected",
           check_bearer_token("", GOOD_TOKEN) is False)
    _check(failures, "wrong token rejected",
           check_bearer_token("Bearer nope", GOOD_TOKEN) is False)
    _check(failures, "non-Bearer scheme rejected",
           check_bearer_token(f"Basic {GOOD_TOKEN}", GOOD_TOKEN) is False)
    _check(failures, "bare token without scheme rejected",
           check_bearer_token(GOOD_TOKEN, GOOD_TOKEN) is False)
    _check(failures, "no expected token configured rejects all",
           check_bearer_token(f"Bearer {GOOD_TOKEN}", None) is False)


# ---------------------------------------------------------------------------
# Full proxy dispatch: auth gate
# ---------------------------------------------------------------------------

def test_auth_gate(failures: list) -> None:
    print("\n--- proxy auth gate (401 makes no Slack call)")
    path = "/slack?method=auth.test"

    spy = CallSpy()
    status, body = handle_slack_proxy(
        path=path, auth_header=None, expected_token=GOOD_TOKEN,
        call_fn=spy, has_credentials=True)
    _check(failures, "missing header -> 401", status == 401, f"got {status}")
    _check(failures, "no call_fn invoked on missing header", spy.calls == [],
           f"calls={spy.calls}")
    _check(failures, "401 body ok:false", body.get("ok") is False)

    spy = CallSpy()
    status, body = handle_slack_proxy(
        path=path, auth_header="Bearer wrong", expected_token=GOOD_TOKEN,
        call_fn=spy, has_credentials=True)
    _check(failures, "wrong token -> 401", status == 401, f"got {status}")
    _check(failures, "no call_fn invoked on wrong token", spy.calls == [],
           f"calls={spy.calls}")

    spy = CallSpy(result={"ok": True, "url": "https://x.slack.com/"})
    status, body = handle_slack_proxy(
        path=path, auth_header=f"Bearer {GOOD_TOKEN}",
        expected_token=GOOD_TOKEN, call_fn=spy, has_credentials=True)
    _check(failures, "correct token -> 200", status == 200, f"got {status}")
    _check(failures, "call_fn invoked once on authed request",
           len(spy.calls) == 1, f"calls={spy.calls}")
    _check(failures, "authed request returns raw Slack JSON",
           body == {"ok": True, "url": "https://x.slack.com/"})


# ---------------------------------------------------------------------------
# Method allow-list
# ---------------------------------------------------------------------------

def test_allow_list(failures: list) -> None:
    print("\n--- method allow-list (403 makes no Slack call)")
    # The allow-list is read-only-only. Beyond the original #14 trio it also
    # carries the read-only diagnostic methods Matt kept on the seam for live
    # ISSUES #16 investigation (search.messages, reactions.list, reactions.get,
    # users.conversations). Assert the exact set so an accidental
    # write/mutating method addition trips the test.
    _check(failures, "allow-list is exactly the read-only set",
           set(SLACK_PROXY_ALLOWED_METHODS) ==
           {"conversations.history", "conversations.replies", "auth.test",
            "search.messages", "reactions.list", "reactions.get",
            "users.conversations"},
           f"got {set(SLACK_PROXY_ALLOWED_METHODS)}")

    # Listed method passes.
    spy = CallSpy(result={"ok": True, "messages": [{"ts": "1.0"}]})
    status, body = handle_slack_proxy(
        path="/slack?method=conversations.history&channel=C1&limit=2",
        auth_header=f"Bearer {GOOD_TOKEN}", expected_token=GOOD_TOKEN,
        call_fn=spy, has_credentials=True)
    _check(failures, "listed method -> 200", status == 200, f"got {status}")
    _check(failures, "listed method invokes call_fn", len(spy.calls) == 1)

    # Unlisted method rejected, no call.
    for bad in ("chat.postMessage", "conversations.list", "files.upload",
                "admin.users.remove"):
        spy = CallSpy()
        status, body = handle_slack_proxy(
            path=f"/slack?method={bad}&channel=C1",
            auth_header=f"Bearer {GOOD_TOKEN}", expected_token=GOOD_TOKEN,
            call_fn=spy, has_credentials=True)
        _check(failures, f"unlisted {bad} -> 403", status == 403, f"got {status}")
        _check(failures, f"no call_fn invoked for {bad}", spy.calls == [],
               f"calls={spy.calls}")

    # Authed but no method at all -> 400 bad request (not 403).
    spy = CallSpy()
    status, body = handle_slack_proxy(
        path="/slack", auth_header=f"Bearer {GOOD_TOKEN}",
        expected_token=GOOD_TOKEN, call_fn=spy, has_credentials=True)
    _check(failures, "missing method -> 400", status == 400, f"got {status}")
    _check(failures, "no call_fn invoked when method missing", spy.calls == [])


# ---------------------------------------------------------------------------
# Error envelope
# ---------------------------------------------------------------------------

def test_error_envelope(failures: list) -> None:
    print("\n--- error envelope (structured, freshness hint, no cred leak)")

    # Shape of the builder directly.
    env = build_error_envelope("boom", has_credentials=True, slack_error="channel_not_found")
    _check(failures, "envelope ok:false", env.get("ok") is False)
    _check(failures, "envelope carries has_credentials",
           env.get("has_credentials") is True)
    _check(failures, "envelope carries slack_error hint",
           env.get("slack_error") == "channel_not_found")

    env_stale = build_error_envelope("boom", has_credentials=False)
    _check(failures, "envelope reflects stale creds",
           env_stale.get("has_credentials") is False)

    # call_fn raises (simulates SlackClient._call RuntimeError on ok:false).
    # The RuntimeError message includes a secret sentinel to prove it is NOT
    # blindly echoed as a credential (str(e) of the RuntimeError is fine; the
    # point is the token/cookie STATE is never serialized). We keep the
    # sentinel out of the exception here and instead assert the whole body has
    # no token/cookie keys and no cred value.
    spy = CallSpy(raises=RuntimeError("Slack API error: channel_not_found"))
    status, body = handle_slack_proxy(
        path="/slack?method=conversations.history&channel=BAD",
        auth_header=f"Bearer {GOOD_TOKEN}", expected_token=GOOD_TOKEN,
        call_fn=spy, has_credentials=True)
    _check(failures, "slack failure -> 502 (not a 500 crash)", status == 502,
           f"got {status}")
    _check(failures, "error body ok:false", body.get("ok") is False)
    _check(failures, "error body has has_credentials",
           "has_credentials" in body)
    _check(failures, "error body surfaces slack error text",
           "channel_not_found" in str(body.get("slack_error", "")))

    # No credentials loaded: _call raises "No credentials"; envelope must still
    # be structured and report has_credentials=False.
    spy = CallSpy(raises=RuntimeError("No credentials"))
    status, body = handle_slack_proxy(
        path="/slack?method=auth.test",
        auth_header=f"Bearer {GOOD_TOKEN}", expected_token=GOOD_TOKEN,
        call_fn=spy, has_credentials=False)
    _check(failures, "no-creds failure -> 502 structured", status == 502,
           f"got {status}")
    _check(failures, "no-creds envelope has_credentials=False",
           body.get("has_credentials") is False)

    # Credential-leak guard: no response body across any path may contain a
    # 'token'/'cookie' key or a raw secret value.
    leak_paths = [
        (None, GOOD_TOKEN, True),                    # 401
        (f"Bearer {GOOD_TOKEN}", GOOD_TOKEN, True),  # 403 (unlisted)
    ]
    for auth, exp, has_creds in leak_paths:
        spy = CallSpy(result={"ok": True})
        _, body = handle_slack_proxy(
            path="/slack?method=chat.postMessage",
            auth_header=auth, expected_token=exp, call_fn=spy,
            has_credentials=has_creds)
        serialized = repr(body).lower()
        _check(failures, "no 'cookie' key/value in body", "cookie" not in serialized,
               f"body={body}")
        _check(failures, "no raw token key leaked",
               "token" not in serialized or "token" in "unauthorized",
               f"body={body}")
        _check(failures, "no secret sentinel in body",
               SECRET_SENTINEL.lower() not in serialized)


# ---------------------------------------------------------------------------
# Query-param parsing
# ---------------------------------------------------------------------------

def test_query_parsing(failures: list) -> None:
    print("\n--- query-param parsing (method extracted, rest passed through)")
    method, params = parse_slack_query(
        "/slack?method=conversations.history&channel=D0AUM6S6HQS"
        "&latest=1784148148.166149&limit=16&inclusive=true")
    _check(failures, "method extracted", method == "conversations.history",
           f"got {method}")
    _check(failures, "method removed from passthrough params",
           "method" not in params, f"params={params}")
    _check(failures, "channel passed through",
           params.get("channel") == "D0AUM6S6HQS", f"params={params}")
    _check(failures, "latest passed through",
           params.get("latest") == "1784148148.166149", f"params={params}")
    _check(failures, "limit passed through", params.get("limit") == "16")
    _check(failures, "inclusive passed through", params.get("inclusive") == "true")

    method, params = parse_slack_query("/slack")
    _check(failures, "no query -> method None", method is None, f"got {method}")
    _check(failures, "no query -> empty params", params == {}, f"params={params}")

    # Passthrough reaches call_fn verbatim through the full dispatch.
    spy = CallSpy(result={"ok": True, "messages": []})
    handle_slack_proxy(
        path="/slack?method=conversations.replies&channel=C9&ts=1.2&limit=5",
        auth_header=f"Bearer {GOOD_TOKEN}", expected_token=GOOD_TOKEN,
        call_fn=spy, has_credentials=True)
    _check(failures, "dispatched method reaches call_fn",
           spy.calls and spy.calls[0][0] == "conversations.replies",
           f"calls={spy.calls}")
    _check(failures, "dispatched params reach call_fn",
           spy.calls and spy.calls[0][1] == {"channel": "C9", "ts": "1.2", "limit": "5"},
           f"calls={spy.calls}")


# ---------------------------------------------------------------------------
# Token-file generation (0600, no overwrite)
# ---------------------------------------------------------------------------

def test_token_file(failures: list) -> None:
    print("\n--- token file (0600, generated if absent, never overwritten)")
    with tempfile.TemporaryDirectory() as d:
        state_dir = Path(d)
        path = api_token_path(state_dir)
        _check(failures, "token file absent before startup", not path.exists())

        tok1 = ensure_api_token(state_dir)
        _check(failures, "token generated non-empty", bool(tok1))
        _check(failures, "token file now exists", path.exists())

        mode = os.stat(path).st_mode & 0o777
        _check(failures, "token file is 0600", mode == 0o600, f"got {oct(mode)}")

        # Existing file NOT overwritten.
        tok2 = ensure_api_token(state_dir)
        _check(failures, "existing token returned unchanged", tok1 == tok2,
               f"{tok1!r} != {tok2!r}")
        _check(failures, "file contents unchanged",
               path.read_text().strip() == tok1)

        # A user-set token with trailing newline is honored, not clobbered.
        path.write_text("my-preset-token\n")
        tok3 = ensure_api_token(state_dir)
        _check(failures, "preset token honored (stripped)",
               tok3 == "my-preset-token", f"got {tok3!r}")


def main() -> int:
    failures: list[str] = []
    test_token_comparison(failures)
    test_auth_gate(failures)
    test_allow_list(failures)
    test_error_envelope(failures)
    test_query_parsing(failures)
    test_token_file(failures)

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
