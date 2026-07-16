# Slack Harvester

Personal, local-only tool that captures Slack messages into an Obsidian vault. React to any message with a trigger emoji (`:bookmark:`, `:eyes:`, `:pushpin:`, etc.) from any Slack client — desktop, mobile, browser — and the message plus its context lands in your vault as structured Markdown within 60 seconds.

## How it works

```
Slack (any client)
  you react with :bookmark:
       │
       ▼
  harvester.py (polls Slack search API every 60s)
       │
       ├─ finds new reactions via search.messages
       ├─ fetches thread or channel context
       ├─ resolves user/channel names
       ├─ enumerates attached files (files[] + blocks[].image)
       │
       ▼
  Python downloads attached files → folder/01.png, 02.pdf, …
       │
       ▼
  opencode (AI writes the Markdown body; informed of asset list)
       │
       ▼
   vault/{capture_dir}/2026-05-26/2026-05-26-rossi-deploy-rollback/
       ├─ capture.md
       ├─ 01.png         (attached image, if any)
       └─ 02.pdf         (attached PDF, if any)
```

Credentials are read directly from a Chrome profile on disk — no running browser, no extension, no Slack app install required. You sign into Slack in a dedicated Chrome profile once, and the harvester reads the session token and cookie from Chrome's local storage and cookie database.

## Requirements

- macOS (reads Chrome Keychain for cookie decryption)
- Python 3.9+
- Chrome (for one-time Slack sign-in)
- [opencode](https://opencode.ai) CLI

## Setup

```bash
git clone https://github.com/mattbaylor/slack-harvester.git
cd slack-harvester
./setup.sh
```

`setup.sh` walks you through:

1. Installing Python dependencies (`cryptography`)
2. Creating `config.json` (workspace URL, vault path, capture directory)
3. Opening Chrome for a one-time Slack sign-in
4. Verifying credentials are readable

## Run

```bash
python3 harvester.py          # uses config.json in the same directory
python3 harvester.py -v       # verbose logging
```

The harvester runs as a foreground process. To run it as a managed background
service that survives reboots and gets auto-restarted on crash, install it
under launchd (recommended for daily use — see "Run as a background service"
below).

For a quick one-off background run:

```bash
nohup python3 harvester.py > /tmp/harvester.log 2>&1 &
```

Health check endpoint at `http://localhost:7777/health`:

```bash
curl -s localhost:7777/health | python3 -m json.tool
```

## Run as a background service (macOS, recommended)

After `./setup.sh` succeeds and you can run the harvester manually, install it
as a launchd agent so it starts on login and gets a periodic healthcheck:

```bash
./install-launchd.sh
```

This:

1. Installs `cryptography` to `~/.local/lib/slack-harvester-deps/` so the
   launchd-spawned `/usr/bin/python3` finds it regardless of pyenv shims or
   per-user site changes.
2. Probes `opencode` under a launchd-equivalent env to confirm model auth works
   (catches "works in my shell, breaks under launchd" early).
3. Renders the launchd plists from
   `com.example.slack-harvester.plist.example` and
   `com.example.slack-harvester-healthcheck.plist.example`, substituting your
   paths and chosen label namespace.
4. Bootstraps both services. They start now and on every login.

Why a dedicated installer (vs. "just symlink the plist"):

- **`HOME` must be inherited from the user session**, not set inside the plist.
  opencode reads `~/.local/share/opencode/auth.json` to pick a model provider;
  overriding `HOME` will silently route to a different auth file and a
  non-existent model. This was a load-bearing bug. The template intentionally
  does not set `HOME`.
- **`opencode` must be authenticated to a working model** (e.g. GitHub Copilot
  OAuth) before launchd will produce captures. The installer probes for this
  upfront so failures happen at install time, not in production.
- **The `cryptography` dep path needs to be stable.** User-site or pyenv paths
  drift; the installer pins a path that survives Python upgrades.

After install:

```bash
# Logs
tail -f /private/tmp/harvester.log
tail -f /private/tmp/harvester-healthcheck.log

# Control (substitute your chosen namespace, default is com.<your-username>.*)
launchctl print gui/$(id -u)/com.<NS>.slack-harvester
launchctl kickstart -k gui/$(id -u)/com.<NS>.slack-harvester       # restart
launchctl bootout gui/$(id -u)/com.<NS>.slack-harvester             # stop
```

The healthcheck DMs you on Slack via the harvester's own credentials if the
service is down or unhealthy for >5 minutes. Cooldown is 30 min between alerts.

## Configuration

Copy `config.example.json` to `config.json` and edit (or let `setup.sh` do it):

```json
{
  "workspace_url": "https://your-workspace.slack.com/",
  "vault_path": "~/vault",
  "capture_dir": "slack-captures",
  "chrome_profile": "~/.slack-harvest-profile",
  "poll_interval": 60,
  "reactions": [
    "bookmark",
    "pushpin",
    "eyes",
    "memo",
    "floppy_disk",
    "point_up",
    "cap",
    "noted"
  ],
  "opencode_command": "opencode"
}
```

| Field | Description |
|---|---|
| `workspace_url` | Your Slack workspace URL. Used during setup for the Chrome sign-in. |
| `vault_path` | Root of your Obsidian vault (or any directory). |
| `capture_dir` | Directory name inside the vault for captures. Created automatically. |
| `chrome_profile` | Path to the dedicated Chrome profile. Don't use your daily profile. |
| `poll_interval` | Seconds between polls. 60 is fine; this isn't latency-sensitive. |
| `reactions` | Emoji names (without colons) that trigger a capture. Use whatever feels natural. |
| `opencode_command` | Path or name of the opencode CLI binary. |

`config.json` is gitignored — each person keeps their own.

## Output

Each capture produces a folder containing `capture.md` plus any attached files:

```
slack-captures/
  2026-05-26/
    2026-05-26-rossi-deploy-rollback/
      capture.md
      01.png                       # attached image, if any
      02.pdf                       # attached PDF, if any
      _pending-images.json         # only present if some assets failed to download
  _pending/                        # raw JSON for capture-level (not asset-level) failures
```

State is no longer kept inside the vault. The dedup ledger and name caches live at:

```
~/.local/state/slack-harvester/
  seen.json
  users-cache.json
  channels-cache.json
```

### Frontmatter

```yaml
---
source: slack
workspace: your-workspace
channel: "#engineering"
author: "Jane Rossi"
participants: ["Jane Rossi", "Alex Chen"]
permalink: https://your-workspace.slack.com/archives/C.../p...
message_date: 2026-05-26T14:03:22+00:00
reacted_message: "Rolled back the prod deploy. Root cause was..."
captured_at: 2026-05-26T14:04:18+00:00
slack_ts: "1716732202.001900"
tags: []
assets:                  # OPTIONAL — present only when ≥1 file downloaded
  - {"filename": "01.png", "mimetype": "image/png", "original_name": "screenshot.png", "size_bytes": 138292}
pending_assets:          # OPTIONAL — present only when ≥1 file failed to download
  - {"filename": "02.mp4", "mimetype": "video/mp4", "original_name": "demo.mp4", "reason": "File too large"}
---
```

The body is AI-generated from the message context: a summary, key decisions, action items, wiki-linked participant names, preserved code blocks or links, and inline asset embeds. A mechanical `## Files` appendix is appended at the bottom for any capture with assets (defensive — guarantees discoverability even if the model omits inline embeds).

## Attached files

The harvester downloads all attached files (images, PDFs, video, audio, zip — anything in Slack `files[]` or `blocks[].image`) into the capture folder. Skipped: link-unfurl thumbnails (OG image, favicon noise) and files larger than 100 MB.

Auth uses the same `xoxc` Bearer token the Web API uses; the `d` cookie is not required for file downloads.

### Partial failure — `_pending-images.json`

If any asset fails to download (network blip, auth issue, size cap), the capture still ships with whatever succeeded. Failures are recorded in two places:

1. `_pending-images.json` next to `capture.md` — machine-readable, contains the original URL, mimetype, intended filename, and failure reason for each item.
2. The capture's `pending_assets:` frontmatter — same shape, easier to skim.
3. A "Failed downloads" section in the body's `## Files` appendix with a retry hint.

### Retrying failed downloads

```bash
python3 harvester.py --retry-pending           # apply
python3 harvester.py --retry-pending --dry-run # preview
```

Walks every `_pending-images.json` under the capture root and retries each item. On success, the bytes land at the originally-intended filename (`01.png`, etc.), the item is removed from `_pending-images.json`, and the capture's `assets:` frontmatter is updated. When `_pending-images.json` becomes empty, it's deleted.

Idempotent — running it with no pending files exits cleanly. Exits rc=2 if Slack credentials are missing.

## How credentials work

The harvester reads Slack's `xoxc` session token from Chrome's `localStorage` LevelDB and the `d` cookie from Chrome's `Cookies` SQLite database. Both are stored in the dedicated Chrome profile you created during setup.

- **Token**: Unencrypted in LevelDB. Extracted via binary regex — no LevelDB library needed.
- **Cookie**: AES-CBC encrypted. Decrypted using Chrome's key from the macOS Keychain (`Chrome Safe Storage`).
- **No Chrome process needed** after initial sign-in. Credentials persist on disk.
- **Token rotation**: If the token expires, sign into Slack again in the dedicated Chrome profile. The harvester picks up the new credentials on the next poll.

## Read-only Slack proxy seam

The harvester exposes one extra loopback route, `GET /slack`, that proxies an
allow-listed set of **read-only** Slack API methods through the credentials the
harvester has already loaded — and returns the raw Slack JSON.

**Why this exists.** Sanctioned local agent tooling (and you) sometimes need a
real Slack API result — e.g. a `conversations.history` window to build a test
fixture. The only credential path is `chrome_creds.py`, which reads Chrome's
cookie/token store under browser-profile paths that are EDR-watchlisted and
off-limits to agents. This seam lets the harvester — whose Chrome-reading is its
accepted job — make the call, so the caller only ever sees JSON. **The
credentials never cross the wire.** The endpoint proxies calls; it never returns
the `token` or `cookie`.

**Auth.** The route is bound to `127.0.0.1` only and gated by a bearer token:

- The token lives in a `0600` file at `<state_dir>/api-token` (default
  `~/.local/state/slack-harvester/api-token`).
- The harvester generates it (`secrets.token_urlsafe(32)`, mode `0600`) at
  startup if the file is absent; an existing file is **never** overwritten, so
  you can pre-seed your own token.
- Callers pass `Authorization: Bearer <token>`; the comparison is constant-time.
  A missing or wrong token returns `401` and **no Slack call is made**.

**Allow-list (read-only only).** Exactly three methods are permitted; anything
else returns `403` with no Slack call:

- `conversations.history`
- `conversations.replies`
- `auth.test`

**Usage.**

```bash
TOKEN=$(cat ~/.local/state/slack-harvester/api-token)
curl -s -H "Authorization: Bearer $TOKEN" \
  'http://127.0.0.1:7777/slack?method=conversations.history&channel=D0AUM6S6HQS&latest=1784148148.166149&limit=16&inclusive=true' \
  | python3 -m json.tool
```

The `method` query param selects the Slack method; every other query param is
passed through to Slack unchanged.

**Errors.** On a Slack-side error or bad request, the response is a structured
JSON envelope (`{"ok": false, "error": ..., "has_credentials": <bool>, ...}`),
not a stack trace. The `has_credentials` field lets a caller tell "creds
expired" apart from "bad request".

A sandbox-run caller resolves a different `$HOME` than the launchd harvester, so
its `<state_dir>/api-token` differs. Always target the running harvester at
`http://127.0.0.1:7777` and read the token from the harvester's real-`HOME`
state dir — that's the file the running process compares against.

## Failure handling

- **Capture-level failures** (opencode error, network drop mid-fetch, etc.) park raw JSON to `{capture_dir}/_pending/`. The dedup ledger entry is un-marked so the next poll retries automatically. Persistent failures stay parked; clean up manually after fixing the underlying issue.
- **Asset-level failures** (one or more attached files won't download) park to `{capture_dir}/YYYY-MM-DD/{slug}/_pending-images.json`. The capture itself ships. Retry with `python3 harvester.py --retry-pending`. See "Attached files" above.
- **Silent losses** (entries in `seen.json` with no matching capture file on disk) are detected by the recovery sweep at startup but are NOT auto-recovered (avoids spurious duplicates from cloud-sync races; see ISSUES.md #11). After confirming the filesystem is in steady state (e.g. GoogleDrive synced), run `python3 harvester.py --recover` (or `--recover --dry-run` to preview) to un-mark the orphans so the next poll re-processes them.
- Slack rate limits are handled with `Retry-After` backoff.
- Auth failures trigger an automatic credential re-read from the Chrome profile.

## Limitations

- **macOS only** — cookie decryption depends on the macOS Keychain. Linux would need a different decryption path.
- **`xoxc` token scope** — some Slack API methods don't work with session tokens. The harvester uses `search.messages` (which works) instead of `reactions.list` (which doesn't).
- **One workspace** — the config supports a single workspace. For multiple workspaces, run separate instances with separate configs.

## Known issues and design notes

See [ISSUES.md](./ISSUES.md). It tracks defects, design decisions, and the
diagnostic trail from past silent-failure incidents. **Read it before modifying
`harvester.py`** — several non-obvious failure modes are documented there.

## License

MIT
