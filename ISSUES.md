# Slack Harvester — Known Issues

Defect tracker. Triaged 2026-06-03 after a silent-failure incident where a captured `:cap:` reaction logged "Capture written" but produced no file in the vault. See [Triggering incident](#triggering-incident) below for the diagnostic trail.

Status legend: 🔴 open / 🟡 in progress / 🟢 fixed / ⚪ won't fix.

**Priority-1 PR landed 2026-06-03:** Pivoted from "opencode does the file write" to "opencode generates body text on stdout, Python writes the file." Structurally eliminates #1 and #5. Also ships: #2 (un-mark seen on failure), #9a (startup self-test, simplified to a stdout PONG probe), #10 (recovery sweep on startup).

**Capture-folder layout + asset capture PR landed 2026-06-10:** Captures moved from flat `{slug}.md` to folder `{slug}/capture.md`. Attached files (images, PDFs, video, etc.) now download into the capture folder as `01.ext`, `02.ext`, …. Partial-failure parking via `_pending-images.json`; recovery via `harvester.py --retry-pending`. Frontmatter gains optional `assets:` and `pending_assets:` fields. Body gains a mechanical `## Files` appendix. Vault-wide reference rewriter (`migrate_refs.py`) and historical capture migrator (`migrate.py`) shipped alongside. Closes #8.

## Triggering incident

- 2026-06-03 13:14:53 local: `:cap:` on `#team-ux-admin` ts `1780513854.323999`. Harvester logged `Capture written for #team-ux-admin/1780513854.323999` at 13:15:05. `seen.json` updated. **No file in `~/vault/51-slack-captures/2026-06-03/`.** `opencode run` returned rc=0 but never wrote.
- Same morning: machine had been rebooted; harvester re-started via launchd at 06:23. First `:cap:` after reboot is the one that silently failed.
- Earlier orphans in `~/vault/51-slack-captures/_pending/`: 1 from 2026-05-27 (`opencode timed out`), 20 from 2026-06-02 (`No such file or directory: 'opencode'`). Both are from a previous architecture (WebSocket extension) and predate the current search-poll sink. Not in scope for fixes; cleanup tracked as #8.

---

## Priority 1 — silent failures (data loss)

### #1 — 🟢 Success inferred from `rc=0`, not from "file exists" (FIXED via redesign)

**Where:** `harvester.py:_invoke_opencode`
**Symptom:** Log says "Capture written" but no markdown file lands in the vault. `seen.json` records the capture as done, so it never retries.
**Cause:** `opencode run` exits 0 in many non-write paths (model produced a plan instead of a tool call; write denied silently; permission prompt with no TTY; prompt parsed as `--help`; etc.). Confirmed observable cause: when invoked from a parent OpenCode session via `subprocess.run`, the child inherits `OPENCODE_PID` / `OPENCODE_RUN_ID` / `OPENCODE_CONFIG` env vars and runs as a nested session that silently no-ops Write tool calls. Launchd-spawned invocations don't inherit these env vars, but the same class of silent-failure exists for other rc=0-no-write paths.
**Fix shipped:** Redesigned `_invoke_opencode` so opencode never writes files. Opencode is called stdout-only ("return the body text, do not invoke tools"). Python computes the slug, builds frontmatter, writes the file, verifies it landed. Structurally impossible for opencode to silently succeed-without-writing now — if stdout is empty the harvester raises; if stdout has content, the file write is Python's job and Python knows when `write_text` returned.

### #2 — 🟢 `mark_seen` happens before opencode runs, no rollback on failure (FIXED, option a)

**Where:** `harvester.py:CaptureWorker._run`
**Symptom:** Any opencode failure permanently removes the capture from the retry queue. User has to manually delete the `seen.json` entry to replay.
**Cause:** "Mark seen before processing" was a deliberate idempotency choice to prevent retry storms on success. The failure path was never finished — `_park_pending` writes a JSON dump and that's it.
**Fix shipped:** Worker's exception handler now calls `state.unmark_seen(dedup_key)` after `_park_pending` so the next poll retries automatically. Accept the risk of tight retry loops on deterministically-broken messages; will mitigate via #3 (alert on pending growth) when implemented.
**Alternative (option b, deferred):** Keep seen-before-process, build a `harvester replay` command that re-enqueues from `_pending/` without touching `seen.json`. Cleaner long-term shape if option a's retry loops become a problem in practice.

### #10 — 🟢 No historical-loss recovery (FIXED)

**Symptom:** Once a capture is silently lost (rc=0, no file, `seen.json` marked), there is no automated recovery. The user must manually delete the `seen.json` entry. The 2026-05-27 timeout-orphan sat lost for 7 days; the 2026-06-03 13:14:53 message is currently lost.
**Fix:** On harvester startup, sweep `seen.json` for entries with no corresponding `.md` file in the expected dated dir (`{vault}/{capture_dir}/{date_from_iso}/`). For each orphan, un-mark from `seen.json` so the next poll re-processes. Log a summary at INFO: `Recovery sweep: un-marked N historical orphans`.
**Caveat:** This will replay every historical silent loss on first run after deploy. That's the intent. Subsequent runs find nothing to un-mark.
**Notes for implementer:** Skip the sweep for entries older than ~30 days — Slack's `search.messages` may not return very old reactions, and we don't want a runaway-replay loop. Also skip when `_pending/{ts}.json` exists for the same dedup key (that's a known-failed message with a paper trail, not a silent loss).

### #9 — 🟡 Reboot-fragile, no init-time self-test (PARTIAL: #9a fixed; #9b/c/d/e open)

**Symptom:** Today's 13:14:53 silent failure was the first capture attempt after a morning reboot. Same pattern as 2026-05-27 (single failed capture, no further failures that day).
**Candidates for the actual reboot-induced failure:**
- **9a** Chrome profile lock from unclean Chrome shutdown → `chrome_creds.read_credentials` fails silently or returns stale data.
- **9b** Sandbox path rot: `config.json` → `chrome_profile` points at a path inside a sandbox-managed directory whose name includes a rotating hash. If the sandbox rotates (reboot, reinstall, version bump), the path stops existing.
- **9c** Cookie decryption races Keychain unlock: launchd may start harvester before user login completes, before Keychain is unlocked, so cookie decrypt fails on the first poll.
- **9d** opencode 1.15.5 persistent daemon not running post-boot; first `opencode run` triggers cold-start (auth init, model registry, possible WAL recovery on `opencode.db`); race conditions plausible.
- **9e** `healthcheck.sh` cooldown file persists across reboot, suppressing alerts for up to 30 min post-boot.

**#9a fix shipped:** `startup_self_test()` in `harvester.py` runs at startup with two probes — Slack `auth.test` and an opencode stdout round-trip ("reply with PONG"). On any failure, DMs Matt immediately via `_dm_self()`. Doesn't block startup; harvester continues so partial outages still park to `_pending/`.
**Still open:**
- KeepAlive plist change (manual edit to `~/Library/LaunchAgents/com.baylor.slack-harvester.plist`).
- #9b chrome-profile path is sandbox-coupled (tracked below).
- #9c healthcheck doesn't exercise the pipeline (tracked below).
- #9d opencode db integrity check (tracked below).
- #9e healthcheck cooldown survives reboot (low priority).

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

### #5 — 🟢 Prompt relies on instruction-following for safety (FIXED via redesign)

**Where:** `harvester.py:515-558`
**Symptom:** Prompt says "Create exactly ONE file at: `{cap_dir}/{date_str}/{{slug}}.md`" and "Do NOT edit any other files in the vault." If a model misinterprets `{slug}` as a literal or decides the better path is to "update" an existing daily log, no guardrail catches it.
**Fix shipped:** Pivoted to stdout-only opencode invocation (see #1). Opencode no longer has any path to write to the filesystem — the prompt explicitly says "Do not invoke any tools. Do not write to any files." Slug and frontmatter are computed in Python (`_build_slug`, `_build_frontmatter`). Blast radius collapses to "opencode returns wrong body text," which is recoverable and visible.

### #6 — 🔴 No fallback when search-API credentials silently 401

**Symptom:** If `xoxc` token expires mid-day, Slack returns 401 on `search.messages`. Harvester's auth-failure path re-reads credentials from Chrome — but if Chrome's stored credentials are also stale, harvester silently stops capturing.
**Status:** Partially mitigated by `has_credentials: false` in `/health` and healthcheck DM. Needs verification that the false-state is actually reached on 401 (vs only on file-missing).

### #9b — 🔴 Chrome profile path is sandbox-coupled

**Where:** `config.json` → `chrome_profile`
**Symptom:** Path contains a sandbox hash that rotates with the sandbox runtime. One reboot or sandbox-runtime reinstall away from a hard breakage.
**Fix:** Move `.slack-harvest-profile` to a stable location:
- `~/Library/Application Support/SlackHarvest/profile/`, or
- `~/.local/state/slack-harvester/chrome-profile/`

Update `config.json`. One-time migration: move the existing profile dir, re-point config.

---

## Priority 4 — quality and consistency

### #11 — 🟢 Recovery sweep races cloud-sync after layout migration (FIXED 2026-06-10)

**Where:** `harvester.py:recovery_sweep` (called at startup).
**Symptom:** After a one-shot layout migration (e.g. flat `{slug}.md` →
folder `{slug}/capture.md`), restarting the harvester before the cloud sync
(GoogleDrive, Dropbox, network mount) has fully propagated the new
filesystem shape causes `find_orphaned_seen` to glob an incomplete view of
disk. It declares N real captures as orphans, un-marks them, and the next
poll re-processes them — producing duplicate capture folders with
`-{last6-ts}` collision suffixes.
**Observed:** 2026-06-10. Migrated 61 captures in the sandbox; harvester
started ~27 min later under launchd; GoogleDrive hadn't finished syncing
the new folder layout to the launchd-process view. Sweep un-marked 7
"orphans"; 5 produced duplicate folders (the other 2 lost the race some
other way). Cleaned up manually.
**Fix shipped (final):** `recovery_sweep` is now REPORT-ONLY at startup.
It logs candidate orphans at WARNING but does not un-mark anything.
First attempt (a 3-second re-verify delay) was insufficient — the
launchd-spawned process's filesystem view doesn't refresh on a simple
sleep+rescan when GoogleDrive sync is in flight. The clean shape is
human-in-the-loop: run `python harvester.py --recover` (or `--recover
--dry-run` to preview) explicitly, after confirming the filesystem is
in steady state (e.g. GoogleDrive menu-bar icon shows synced).

The trade-off: silent losses are now visible (logged loudly at startup)
but won't auto-recover. Acceptable because (a) silent losses are rare
in the post-stdout-only-redesign era, and (b) automatic recovery of
"missing" files that aren't actually missing is more dangerous than
the absence of recovery for genuine losses.

### #12 — 🟢 Image downloads silently saved Slack sign-in HTML as `NN.png` (FIXED 2026-06-15)

**Where:** `harvester.py:SlackClient.download_file` (asset download path).
**Symptom:** Captures with image attachments produce `NN.png` files that
are HTML, not PNG. The capture pipeline reports success: log says
`asset 01.png ← Screenshot ... (69145 bytes, image/png)`, frontmatter
records the asset, body inline-embeds `![alt](01.png)`, no
`_pending-images.json` is written. Opening the "image" in Obsidian
shows a broken-image icon; `file 01.png` reports `HTML document text`;
`hexdump` of the first bytes shows `<!DOCTYPE html>` — the Slack sign-in
page.
**Observed:** 2026-06-15. Capture `2026-06-15-john-capture/01.png`,
69145 bytes, magic bytes `<!DOCTYPE html`, body content the Slack
web sign-in page.
**Cause:** `download_file` deliberately omitted the `d` cookie based
on a (wrong) belief that `files.slack.com` accepts the `xoxc` Bearer
token alone. Empirically, Slack's file CDN requires BOTH `xoxc` AND
the `d` cookie. Without the cookie it returns 200 OK with the
sign-in HTML page as the body, and the response Content-Type can
mirror the requested file type — so the existing guards all passed:
the pre-flight Content-Length check saw a real number, the streamed
size cap wasn't exceeded, and the empty-body heuristic missed
because the file isn't empty, it's 67 KB of HTML.
**Fix shipped:** Three layers of defense in `download_file`:
1. Require `state.cookie` alongside `state.token` and send it as
   `Cookie: d={cookie}`, matching the Web API auth path.
2. Sniff the response `Content-Type` header; any `text/html` →
   raise immediately, no bytes written.
3. After streaming, sniff first 32 bytes: if they look like HTML
   (`<!doctype html`, `<html`, `<head`) → unlink, raise. If the
   caller passed an `expected_mimetype` starting with `image/`,
   validate the bytes against known image magics (PNG, JPEG, GIF,
   BMP, WebP, HEIC, SVG); on mismatch → unlink, raise.

Failure messages are routed through the existing
`_pending-images.json` parking flow, so failed images now leave a
recoverable paper trail instead of silent HTML corruption.
**Not in scope:** Recovery of already-corrupted captures. Per user
direction, fix forward only — bad files stay where they are.

### #13 — 🟢 Unresolved `<@Uxxxx>` mentions let opencode hallucinate names (FIXED 2026-07-13)

**Where:** `harvester.py:CaptureWorker.process` (Step 3 name resolution)
and `_invoke_opencode` (body prompt).
**Symptom:** When a captured message `@`-mentions people, the wrong name
can appear in the capture body. Observed: capture
`2026-07-13/2026-07-13-riverginther-so-don-forget/capture.md` rendered
River's opening ping as `[[John]] [[Josh Wilson]] [[Marco Rangel]]` when
the message actually pinged **John, jwilson, and matt**. The word "Marco"
was pulled from a *later* sentence in the same message ("Marco is busy
with closure compiler…") — a plausible-but-wrong substitution.
**Cause:** Name resolution (`resolve_user`) was only applied to message
**authors** (to build `participants`). Slack mention markup embedded
*inside* message text (`<@Uxxxx>`, `<#Cxxxx|name>`, `<!subteam^Sxxxx>`)
was never resolved. The raw text — still carrying `<@U0ATR90VBMJ>` —
was passed to opencode in the bundle (`reacted_message_text`) and stored
verbatim in the `reacted_message` frontmatter. opencode has **no access
to the user cache**, so it cannot resolve a raw id; the body-generation
prompt's "use `[[Display Name]]` wiki-links" instruction then forced it to
invent a name, which it did by pattern-matching nearby text. The only
prior handling of `<@…>` was in `_build_slug`, which merely *strips* the
markup for topic-word extraction (`re.sub(r"<[^>]+>", " ", text)`).
**Root property:** opencode is a stateless markdown generator by design
(see README "Why opencode is stdout-only"). Any id it receives that it
can't resolve is an invitation to hallucinate. The fix keeps id→name
resolution entirely in Python, where the cache lives.
**Fix shipped:** New `SlackClient.expand_mentions(text)` — a deterministic,
cache-backed pass that rewrites `<@Uxxxx>`/`<@Uxxxx|label>` → `@name`,
`<#Cxxxx|name>`/`<#Cxxxx>` → `#name`, `<!subteam^Sxxxx|@grp>` → `@grp`,
and `<!here|channel|everyone>` → `@here` etc. Applied in Step 3 to every
context message's `text` in-place, and applied explicitly to the reacted
message text at Step 7 (the reacted `msg` is a separate object fetched via
`get_message`, so the Step-3 loop does not touch it). Both the opencode
prompt and the Python-built frontmatter now see real names only —
structurally removing opencode's opportunity to guess. Bare link markup
(`<https://…>`, `<https://…|label>`) is left untouched (regex only matches
`<@`, `<#`, `<!`). Verified against the ARTI-281 raw ids using the live
`users-cache.json`: the three-name ping expands to `@John @jwilson @matt`.
**Backfilled:** The ARTI-281 capture body, `reacted_message` frontmatter,
and action items were manually corrected (Marco Rangel → Matt for the
opening ping; the separate "Marco is on closure-compiler work" reference
was already correct and left as-is). `participants` was already correct
(authors, not mentions) and untouched.
**Not in scope:** Sweeping other historical captures for the same class
of error. Fix-forward; any pre-2026-07-13 capture with `<@…>` in its
`reacted_message` frontmatter is a candidate for the same latent bug if
ever re-examined.

### #14 — 🟢 No sanctioned seam for agents to read Slack without touching Chrome (FIXED 2026-07-16)

**Where:** `harvester.py:HarvestHandler.do_GET` (the loopback health server —
previously `GET /health` only).
**Symptom:** Local agent tooling (and Matt) had no sanctioned path to fetch a
real read-only Slack API result — e.g. a `conversations.history` window to build
a test fixture. The only credential path, `chrome_creds.py`, reads Chrome's
LevelDB (`xoxc` token) and Cookies SQLite DB + macOS Keychain (`d` cookie), all
under browser-profile paths that are EDR-watchlisted (MITRE T1539) and forbidden
for the agent — touching them tripped a cybersec alert on 2026-06-29.
**Cause:** The harvester already reads and holds fresh creds
(`HarvesterState.token`/`.cookie`) and already runs a loopback HTTP server
(`HarvestHandler`, bound `127.0.0.1:7777`), but that server exposed **only**
`GET /health`. There was no seam for anything else to reuse the harvester's
already-loaded creds, so the creds were trapped in the process — reachable only
by re-reading Chrome, the forbidden path. Taking a dependency on
`mcp-cookie-bridge` doesn't help (it can't get the `xoxc` token — that's not a
cookie); instead we adopt the bridge's serve/freshness/loopback *pattern* in
owned code.
**Fix shipped:** Added one loopback-only, bearer-token-guarded route,
`GET /slack?method=<m>&<passthrough params>`, to `HarvestHandler.do_GET`.
1. **Auth.** Reads a `0600` bearer-token file at `<state_dir>/api-token`
   (generated with `secrets.token_urlsafe(32)` at startup if absent, never
   overwritten) and compares the `Authorization: Bearer <t>` header with
   `hmac.compare_digest` (constant-time). Missing/wrong token → `401`, and no
   Slack call is made.
2. **Allow-list (read-only only).** Exactly `{conversations.history,
   conversations.replies, auth.test}`. Anything else → `403`, no Slack call.
3. **Proxy.** On an allowed + authed request, builds params from the query
   string (everything except `method` passes through) and calls the existing
   `SlackClient._call`, returning the raw Slack JSON with `200`. The creds
   (`token`/`cookie`) are never serialized into any response.
4. **Freshness envelope.** On a Slack-side or bad-request error, returns a
   structured JSON error including a `has_credentials` hint (from
   `state.has_credentials()`) — not a `500` stack trace — so a caller can tell
   "creds expired" from "bad request".
The handler logic (token check, allow-list, query parse, envelope build, proxy
dispatch) is factored into plain module-level functions taking an injected
`call_fn`, unit-tested hermetically in `tests/test_slack_proxy_seam.py` (no
socket bind, no live Slack, no Chrome). Bind stays `127.0.0.1`; `/health` keys
are unchanged. The `chrome_creds` import was made lazy so the module is
importable for the hermetic tests without `cryptography` installed; runtime
behavior is unchanged (the import still happens on the first credential read).
**Advances but does not close:** the spirit of **#9c** (a `/health?probe=1`
pipeline exercise can now reuse the `auth.test` allow-list entry) and **#6**
(the `has_credentials` freshness hint in the `/slack` envelope makes an expired
-creds failure distinguishable from a bad request). Both stay open — this adds
the seam, not the healthcheck wiring or the verified 401-flip.
**Not in scope:** a `/creds` endpoint returning raw token/cookie (explicitly
rejected — the proxy hands data, not secrets); write/mutating Slack methods
(read-only allow-list only); turning the seam into an MCP tool (curl/HTTP is
enough for the fixture use case; noted for a future session).

### #4 — 🔴 Two opencode installations diverge silently

**Symptom:** Harvester invokes Homebrew opencode 1.15.5 (May 22 install). Interactive use is a separate sandboxed pnpm-dlx opencode install (different state dir, different auth). Model behind `claude-opus-4.7` alias in Copilot can drift; harvester silently gets a different model than tested with.
**Fix:** Pass `--model github-copilot/claude-opus-4.7` (or equivalent) explicitly to `opencode run`. Optionally pin opencode binary version via `opencode_command` in config.

---

## Priority 5 — cleanup

### #8 — 🟢 Orphaned old-architecture state in the vault (FIXED 2026-06-10)

**Symptom:** `~/vault/51-slack-captures/_pending/` (21 files) and `~/vault/51-slack-captures/_state/` (3 files) were from the WebSocket-extension era. Live state moved to `~/.local/state/slack-harvester/`. The orphans confused future debugging.
**Fix shipped:** Both archived to `~/vault/90-archive/2026-06-slack-harvester-pre-pivot/` as part of the 2026-06-10 capture-folder-layout migration (see `migrate.py`). The harvester continues to write capture-level failures to a live `_pending/` dir, but the dir starts empty on first run and is no longer a debugging tarpit.

---

## Execution order

| Step | Issues | Status |
|---|---|---|
| 1 | #1, #2, #5, #9a, #10 | 🟢 Shipped 2026-06-03. Stdout-only redesign + un-mark-on-failure + recovery sweep + startup self-test. |
| 2 | #8 + asset capture | 🟢 Shipped 2026-06-10. Folder-per-capture layout + asset download + migration tools. Closed orphan dirs. |
| 2b | #11 | 🟢 Shipped 2026-06-10. Recovery-sweep cloud-sync race guard (refuse if >20% orphan fraction). |
| 2c | #12 | 🟢 Shipped 2026-06-15. Image download auth fix (`d` cookie) + HTML/magic-bytes guards. |
| 2d | #13 | 🟢 Shipped 2026-07-13. Resolve `<@Uxxxx>` mentions in Python before opencode (stop name hallucination). |
| 2e | #14 | 🟢 Shipped 2026-07-16. Loopback token-guarded read-only Slack proxy seam (`GET /slack`). Advances #9c/#6, both still open. |
| 3 | #9b | 🔴 Next. Move chrome profile out of sandbox-coupled path. |
| 4 | #3, #7 | 🔴 Observability: `pending_count` + `queue_depth` in healthcheck. |
| 5 | #9c, #9d, #9 KeepAlive plist | 🔴 Deeper healthcheck probes + auto-restart on crash. |
| 6 | #4 | 🔴 Pin model. |
| 7 | #6 | 🔴 Verify 401-path flips `has_credentials`. |

---

## Diagnostic trail (for future debugging)

When silent-failure mode strikes:

1. `curl -s http://127.0.0.1:7777/health | jq` — `has_credentials`, `queue_depth`, `seen_count`.
2. `tail -F /private/tmp/harvester.log` — look for `Processing capture: …` followed by `Capture written for …` with no errors. That's the silent-failure signature.
3. `ls -la ~/vault/51-slack-captures/$(date +%Y-%m-%d)/*/capture.md` — confirm presence/absence of file.
4. `ls -lat ~/.local/share/opencode/log/` — find the log timestamp matching the harvester's processing window. Real-home opencode logs, *not* any sandbox's `~/.local/share/opencode/log/` (different `$HOME`).
5. `ls -lat ~/.local/share/opencode/snapshot/` and `tool-output/` — these get written when opencode actually runs tools. If they're not fresh, opencode never called a tool.
6. Replay manually: `env -i PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" HOME="$HOME" /opt/homebrew/bin/opencode run --dir ~/vault "<copy of the prompt from harvester.py:515 with {bundle} interpolated>"` — observe stdout.

Two opencode installations gotcha:

| Install | Path | State dir | Used by |
|---|---|---|---|
| Homebrew 1.15.5 | `/opt/homebrew/bin/opencode` | `~/.local/share/opencode/` | harvester (launchd) |
| pnpm-dlx (sandboxed) | `~/Library/Caches/pnpm/dlx/.../opencode` | `<sandbox-home>/.local/share/opencode/` | interactive agent sessions |

When debugging, confirm which one you're looking at logs for.
