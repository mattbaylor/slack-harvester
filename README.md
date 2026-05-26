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
       │
       ▼
  opencode (AI writes the Markdown)
       │
       ▼
  vault/{capture_dir}/2026-05-26/2026-05-26-rossi-deploy-rollback.md
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

The harvester runs as a foreground process. To run it in the background:

```bash
nohup python3 harvester.py > /tmp/harvester.log 2>&1 &
```

Health check endpoint at `http://localhost:7777/health`:

```bash
curl -s localhost:7777/health | python3 -m json.tool
```

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

Each capture produces a Markdown file:

```
slack-captures/
  2026-05-26/
    2026-05-26-rossi-deploy-rollback.md
  _pending/           # raw JSON for failed captures
  _state/
    seen.json         # dedup ledger
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
---
```

The body is AI-generated from the message context: a summary, key decisions, action items, wiki-linked participant names, and preserved code blocks or links.

## How credentials work

The harvester reads Slack's `xoxc` session token from Chrome's `localStorage` LevelDB and the `d` cookie from Chrome's `Cookies` SQLite database. Both are stored in the dedicated Chrome profile you created during setup.

- **Token**: Unencrypted in LevelDB. Extracted via binary regex — no LevelDB library needed.
- **Cookie**: AES-CBC encrypted. Decrypted using Chrome's key from the macOS Keychain (`Chrome Safe Storage`).
- **No Chrome process needed** after initial sign-in. Credentials persist on disk.
- **Token rotation**: If the token expires, sign into Slack again in the dedicated Chrome profile. The harvester picks up the new credentials on the next poll.

## Failure handling

- Failed captures are parked as raw JSON in `_pending/`. Fix the issue (usually expired credentials), then remove the entry from `_state/seen.json` and let the next poll retry.
- Slack rate limits are handled with `Retry-After` backoff.
- Auth failures trigger an automatic credential re-read from the Chrome profile.

## Limitations

- **macOS only** — cookie decryption depends on the macOS Keychain. Linux would need a different decryption path.
- **`xoxc` token scope** — some Slack API methods don't work with session tokens. The harvester uses `search.messages` (which works) instead of `reactions.list` (which doesn't).
- **One workspace** — the config supports a single workspace. For multiple workspaces, run separate instances with separate configs.

## License

MIT
