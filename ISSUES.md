# Slack Harvester — Known Issues

Defect tracker. Triaged 2026-06-03 after a silent-failure incident where a captured `:cap:` reaction logged "Capture written" but produced no file in the vault. See [Triggering incident](#triggering-incident) below for the diagnostic trail.

Status legend: 🔴 open / 🟡 in progress / 🟢 fixed / ⚪ won't fix.

## Triggering incident

- 2026-06-03 13:14:53 local: `:cap:` on `#team-ux-admin` ts `1780513854.323999`. Harvester logged `Capture written for #team-ux-admin/1780513854.323999` at 13:15:05. `seen.json` updated. **No file in `~/vault/51-slack-captures/2026-06-03/`.** `opencode run` returned rc=0 but never wrote.
- Same morning: machine had been rebooted; harvester re-started via launchd at 06:23. First `:cap:` after reboot is the one that silently failed.
- Earlier orphans in `~/vault/51-slack-captures/_pending/`: 1 from 2026-05-27 (`opencode timed out`), 20 from 2026-06-02 (`No such file or directory: 'opencode'`). Both are from a previous architecture (WebSocket extension) and predate the current search-poll sink. Not in scope for fixes; cleanup tracked as #8.

---

## Priority 1 — silent failures (data loss)

### #1 — 🔴 Success inferred from `rc=0`, not from "file exists"

**Where:** `harvester.py:507-577` (`_invoke_opencode`)
**Symptom:** Log says "Capture written" but no markdown file lands in the vault. `seen.json` records the capture as done, so it never retries.
**Cause:** `opencode run` exits 0 in many non-write paths (model produced a plan instead of a tool call; write denied silently; permission prompt with no TTY; prompt parsed as `--help`).
**Fix:**
- Snapshot `out_dir` file list before `subprocess.run`, diff after.
- If no new `.md` appeared, raise — `_park_pending` writes JSON + (per #2) un-marks seen.
- Log `result.stdout[:500]` and `result.stderr[:500]` at INFO on every invocation (gate verbose dump behind `-v`).

### #2 — 🔴 `mark_seen` happens before opencode runs, no rollback on failure

**Where:** `harvester.py:372-373`
**Symptom:** Any opencode failure permanently removes the capture from the retry queue. User has to manually delete the `seen.json` entry to replay.
**Cause:** "Mark seen before processing" was a deliberate idempotency choice (per [design doc](~/vault/00-inbox/2026-05-26-slack-harvester-design.md)) to prevent retry storms on success. But the failure path was never finished — `_park_pending` writes a JSON dump and that's it.
**Fix (chosen — option a):** On failure path, `_park_pending` also calls `state.unmark_seen(dedup_key)` so the next poll retries automatically. Accept the risk of tight retry loops on deterministically-broken messages; mitigate via #3 (alert on pending growth).
**Alternative (option b, deferred):** Keep seen-before-process, build a `harvester replay` command that re-enqueues from `_pending/` without touching `seen.json`. Cleaner long-term shape if option a's retry loops become a problem in practice.

### #10 — 🔴 No historical-loss recovery (fold into priority-1 PR)

**Symptom:** Once a capture is silently lost (rc=0, no file, `seen.json` marked), there is no automated recovery. The user must manually delete the `seen.json` entry. The 2026-05-27 timeout-orphan sat lost for 7 days; the 2026-06-03 13:14:53 message is currently lost.
**Fix:** On harvester startup, sweep `seen.json` for entries with no corresponding `.md` file in the expected dated dir (`{vault}/{capture_dir}/{date_from_iso}/`). For each orphan, un-mark from `seen.json` so the next poll re-processes. Log a summary at INFO: `Recovery sweep: un-marked N historical orphans`.
**Caveat:** This will replay every historical silent loss on first run after deploy. That's the intent. Subsequent runs find nothing to un-mark.
**Notes for implementer:** Skip the sweep for entries older than ~30 days — Slack's `search.messages` may not return very old reactions, and we don't want a runaway-replay loop. Also skip when `_pending/{ts}.json` exists for the same dedup key (that's a known-failed message with a paper trail, not a silent loss).

### #9 — 🔴 Reboot-fragile, no init-time self-test

**Symptom:** Today's 13:14:53 silent failure was the first capture attempt after a morning reboot. Same pattern as 2026-05-27 (single failed capture, no further failures that day).
**Candidates for the actual reboot-induced failure:**
- **9a** Chrome profile lock from unclean Chrome shutdown → `chrome_creds.read_credentials` fails silently or returns stale data.
- **9b** Sandbox path rot: `config.json` → `chrome_profile: ~/.local/state/jh-code/sandboxes/ef3ef8c6f8de8799/...`. The hash is from a previous jh-code sandbox; current sandbox is `f4ecbd62229d0e22`. If jh-code rotates sandboxes the path stops existing.
- **9c** Cookie decryption races Keychain unlock: launchd may start harvester before user login completes, before Keychain is unlocked, so cookie decrypt fails on the first poll.
- **9d** opencode 1.15.5 persistent daemon not running post-boot; first `opencode run` triggers cold-start (auth init, model registry, possible WAL recovery on `opencode.db`); race conditions plausible.
- **9e** `healthcheck.sh` cooldown file persists across reboot, suppressing alerts for up to 30 min post-boot.

**Fix:** Init-time self-test in `harvester.py` startup that:
1. Calls Slack `auth.test` and logs the result.
2. Runs `opencode run "echo OK"` against a temp dir and verifies a probe file lands.
3. DMs Matt immediately on any failure (don't wait for the 5-min healthcheck cycle).
4. Also: `launchctl` plist gets `KeepAlive { SuccessfulExit: false }` so a crash auto-restarts.

---

## Priority 2 — observability gaps

### #3 — 🔴 `_pending/` is write-only; nothing alerts on growth

**Symptom:** 2026-05-27 timeout entry sat unread for 7 days. Nobody knew until I went looking for unrelated reasons.
**Fix:**
- `/health` endpoint returns `pending_count: <int>`.
- `healthcheck.sh` DMs when `pending_count > 0`, separate cooldown from "down" alerts.

### #7 — 🔴 No alert on queue depth

**Where:** `harvester.py:402-404` (queue is unbounded, depth logged at INFO only)
**Symptom:** If opencode hangs (120s timeout per call) and you spam `:cap:` during the hang, queue grows silently until eventually drains or capture window closes.
**Fix:** `healthcheck.sh` reads `queue_depth` from `/health`, DMs when `> 3` for 2 consecutive polls.

### #9c — 🔴 Healthcheck doesn't exercise the pipeline

**Symptom:** Current `healthcheck.sh` just GETs `/health`, which returns `has_credentials: true` based on cookie/token presence on disk — doesn't prove either still works.
**Fix:** Add `/health?probe=1` that returns success only if harvester can call Slack `auth.test` AND invoke `opencode run` on a probe input. Run probe variant once per hour from healthcheck (every 5 min would be too expensive).

### #9d — 🔴 No opencode DB integrity check on startup

**Symptom:** `~/.local/share/opencode/opencode.db` is in WAL mode. Unclean shutdown could corrupt it. Symptoms would be the same silent-failure mode as #1.
**Fix:** On harvester startup, run `sqlite3 ~/.local/share/opencode/opencode.db 'PRAGMA quick_check;'`. If not "ok", log loudly and DM.

---

## Priority 3 — blast radius

### #5 — 🔴 Prompt relies on instruction-following for safety

**Where:** `harvester.py:515-558`
**Symptom:** Prompt says "Create exactly ONE file at: `{cap_dir}/{date_str}/{{slug}}.md`" and "Do NOT edit any other files in the vault." If a model misinterprets `{slug}` as a literal or decides the better path is to "update" an existing daily log, no guardrail catches it.
**Fix (chosen — 5b):** Use `--agent` with a stripped-down agent definition that has **write access scoped to `51-slack-captures/**` only**. opencode permissions support path-scoped denies; use them.
**Bonus:** Pre-compute the slug in Python from `bundle['author']` and the message text, pass an absolute output path. Tell the model the *path* not the *naming scheme*.

### #6 — 🔴 No fallback when search-API credentials silently 401

**Symptom:** If `xoxc` token expires mid-day, Slack returns 401 on `search.messages`. Harvester's auth-failure path re-reads credentials from Chrome — but if Chrome's stored credentials are also stale, harvester silently stops capturing.
**Status:** Partially mitigated by `has_credentials: false` in `/health` and healthcheck DM. Needs verification that the false-state is actually reached on 401 (vs only on file-missing).

### #9b — 🔴 Chrome profile path is sandbox-coupled

**Where:** `config.json` → `chrome_profile`
**Symptom:** Path contains a jh-code sandbox hash that has rotated since config was written. One reboot or jh-code reinstall away from a hard breakage.
**Fix:** Move `.slack-harvest-profile` to a stable location:
- `~/Library/Application Support/SlackHarvest/profile/`, or
- `~/.local/state/slack-harvester/chrome-profile/`

Update `config.json`. One-time migration: move the existing profile dir, re-point config.

---

## Priority 4 — quality and consistency

### #4 — 🔴 Two opencode installations diverge silently

**Symptom:** Harvester invokes Homebrew opencode 1.15.5 (May 22 install). Interactive use is jh-code-sandboxed pnpm-dlx opencode (separate state dir, separate auth). Model behind `claude-opus-4.7` alias in Copilot can drift; harvester silently gets a different model than tested with.
**Fix:** Pass `--model github-copilot/claude-opus-4.7` (or equivalent) explicitly to `opencode run`. Optionally pin opencode binary version via `opencode_command` in config.

---

## Priority 5 — cleanup

### #8 — 🔴 Orphaned old-architecture state in the vault

**Symptom:** `~/vault/51-slack-captures/_pending/` (21 files) and `~/vault/51-slack-captures/_state/` (3 files) are from the WebSocket-extension era. Live state moved to `/Users/matt/.local/state/slack-harvester/`. The orphans confuse future debugging — I almost mis-diagnosed today's issue by trusting the in-vault `seen.json`.
**Fix:** Move both to `~/vault/90-archive/2026-06-slack-harvester-pre-pivot/` or delete outright. Add a note to the README that current state dir is `state_dir` in `config.json`, not in the vault.

---

## Execution order

| Step | Issues | Rationale |
|---|---|---|
| 1 | #1, #2, #9a, #10 | One PR. Kills the silent-failure class that started this. Recovers today's 13:14:53 message and the 2026-05-27 orphan via #10's sweep. |
| 2 | #9b | Independent, low-risk, eliminates reboot fragility class. |
| 3 | #5 | Reduce blast radius before piling on more features. |
| 4 | #3, #7 | Observability — should land before adding more failure modes. |
| 5 | #9c, #9d | Deeper healthcheck probes. |
| 6 | #8 | Cleanup. |
| 7 | #4 | Pin model. |
| — | #6 | Fold into #1 work (verify the 401-path actually flips `has_credentials`). |

---

## Diagnostic trail (for future debugging)

When silent-failure mode strikes:

1. `curl -s http://127.0.0.1:7777/health | jq` — `has_credentials`, `queue_depth`, `seen_count`.
2. `tail -F /private/tmp/harvester.log` — look for `Processing capture: …` followed by `Capture written for …` with no errors. That's the silent-failure signature.
3. `ls -la ~/vault/51-slack-captures/$(date +%Y-%m-%d)/` — confirm presence/absence of file.
4. `ls -lat ~/.local/share/opencode/log/` — find the log timestamp matching the harvester's processing window. Real-home opencode logs, *not* the jh-code sandbox `~/.local/share/opencode/log/` (different `$HOME`).
5. `ls -lat ~/.local/share/opencode/snapshot/` and `tool-output/` — these get written when opencode actually runs tools. If they're not fresh, opencode never called a tool.
6. Replay manually: `env -i PATH="/Users/matt/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" HOME=/Users/matt /opt/homebrew/bin/opencode run --dir ~/vault "<copy of the prompt from harvester.py:515 with {bundle} interpolated>"` — observe stdout.

Two opencode installations gotcha:

| Install | Path | State dir | Used by |
|---|---|---|---|
| Homebrew 1.15.5 | `/opt/homebrew/bin/opencode` | `~/.local/share/opencode/` | harvester (launchd) |
| jh-code sandbox | `~/Library/Caches/pnpm/dlx/.../opencode` | `~/.local/state/jh-code/sandboxes/<hash>/home/.local/share/opencode/` | interactive agent sessions |

When debugging, confirm which one you're looking at logs for.
