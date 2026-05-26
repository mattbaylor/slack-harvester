#!/usr/bin/env python3
"""
Slack Harvester — Polling-based capture.

Polls Slack's search API for messages you've reacted to with trigger emojis,
fetches message context, and hands the bundle to opencode for vault capture.

Reads credentials directly from a Chrome profile on disk — no extension needed.

Usage:
    python harvester.py                     # uses config.json in same directory
    python harvester.py --config other.json
    python harvester.py --verbose
"""

from __future__ import annotations

import argparse
import json
import logging
import queue
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from chrome_creds import read_credentials

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent.resolve()

DEFAULTS = {
    "vault_path": "~/vault",
    "capture_dir": "slack-captures",
    "chrome_profile": "~/.slack-harvest-profile",
    "poll_interval": 60,
    "health_port": 7777,
    "reactions": ["cap", "bookmark", "pushpin", "floppy_disk", "memo",
                  "eyes", "point_up", "noted"],
    "opencode_command": "opencode",
}


def load_config(config_path: Path) -> dict:
    """Load config.json, merging with defaults."""
    cfg = dict(DEFAULTS)
    if config_path.exists():
        with open(config_path) as f:
            user_cfg = json.load(f)
        cfg.update(user_cfg)
    else:
        log.warning("No config.json found at %s, using defaults", config_path)
    return cfg

SLACK_API = "https://slack.com/api"
CACHE_MAX_AGE_DAYS = 7

log = logging.getLogger("harvester")

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class HarvesterState:
    """Thread-safe state container."""

    def __init__(self, vault: Path, capture_dir: str, chrome_profile: Path):
        self.vault = vault
        self.capture_dir = capture_dir
        self.chrome_profile = chrome_profile
        self.state_dir = vault / capture_dir / "_state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (vault / capture_dir / "_pending").mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self.token: Optional[str] = None
        self.cookie: Optional[str] = None

        # Dedup
        self.seen_path = self.state_dir / "seen.json"
        self.seen: dict = self._load_json(self.seen_path, {})

        # Caches
        self.users_cache_path = self.state_dir / "users-cache.json"
        self.channels_cache_path = self.state_dir / "channels-cache.json"
        self.users: dict = self._load_json(self.users_cache_path, {})
        self.channels: dict = self._load_json(self.channels_cache_path, {})

        # Load credentials from Chrome profile
        self.refresh_credentials()

    @staticmethod
    def _load_json(path: Path, default):
        if path.exists():
            try:
                return json.loads(path.read_text())
            except (json.JSONDecodeError, OSError) as e:
                log.warning("Failed to load %s: %s", path, e)
        return default

    def _save_json(self, path: Path, data):
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.rename(path)

    def is_seen(self, key: str) -> bool:
        with self._lock:
            return key in self.seen

    def mark_seen(self, key: str):
        with self._lock:
            self.seen[key] = datetime.now(timezone.utc).isoformat()
            self._save_json(self.seen_path, self.seen)

    def refresh_credentials(self):
        """Read credentials from Chrome profile on disk."""
        with self._lock:
            try:
                token, cookie = read_credentials(self.chrome_profile)
                if token and cookie:
                    self.token = token
                    self.cookie = cookie
                    log.info("Credentials loaded from Chrome profile (token: %d chars, cookie: %d chars)",
                             len(token), len(cookie))
                elif token:
                    self.token = token
                    log.warning("Token loaded but cookie not found")
                else:
                    log.warning("No credentials found in Chrome profile at %s", self.chrome_profile)
            except Exception as e:
                log.error("Failed to read Chrome credentials: %s", e)

    def has_credentials(self) -> bool:
        return bool(self.token and self.cookie)

    def save_users_cache(self):
        with self._lock:
            self._save_json(self.users_cache_path, self.users)

    def save_channels_cache(self):
        with self._lock:
            self._save_json(self.channels_cache_path, self.channels)


# ---------------------------------------------------------------------------
# Slack API client
# ---------------------------------------------------------------------------


class SlackClient:
    """Minimal Slack Web API client using xoxc + d cookie."""

    def __init__(self, state: HarvesterState):
        self.state = state

    def _call(self, method: str, params: dict) -> dict:
        if not self.state.token or not self.state.cookie:
            raise RuntimeError("No credentials")

        url = f"{SLACK_API}/{method}"
        body = urlencode(params).encode()

        req = Request(url, data=body, method="POST")
        req.add_header("Authorization", f"Bearer {self.state.token}")
        req.add_header("Cookie", f"d={self.state.cookie}")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")

        resp = urlopen(req, timeout=15)
        data = json.loads(resp.read())

        if not data.get("ok"):
            error = data.get("error", "unknown")
            if error == "invalid_auth":
                log.error("Auth invalid — token/cookie may have rotated")
                self.state.token = None
                self.state.cookie = None
            elif error == "ratelimited":
                retry_after = int(resp.headers.get("Retry-After", 5))
                log.warning("Rate limited, sleeping %ds", retry_after)
                time.sleep(retry_after)
                return self._call(method, params)
            raise RuntimeError(f"Slack API error: {error}")

        return data

    def search_reactions(self, reactions: list, count: int = 10) -> list[dict]:
        """Search for messages the current user reacted to with any of the given emojis.

        Runs one search per reaction (Slack doesn't support OR with hasmy::).
        Deduplicates by ts.
        """
        seen_ts = set()
        results = []

        for reaction in reactions:
            try:
                data = self._call("search.messages", {
                    "query": f"hasmy::{reaction}:",
                    "count": str(count),
                    "sort": "timestamp",
                    "sort_dir": "desc",
                })
                for msg in data.get("messages", {}).get("matches", []):
                    ts = msg.get("ts")
                    if ts and ts not in seen_ts:
                        seen_ts.add(ts)
                        results.append(msg)
            except RuntimeError as e:
                if "invalid_auth" in str(e):
                    raise  # Propagate auth failures
                log.warning("Search for :%s: failed: %s", reaction, e)

        return results

    def get_authed_user(self) -> dict:
        """Get info about the authenticated user."""
        data = self._call("auth.test", {})
        return data

    def get_message(self, channel: str, ts: str) -> dict:
        data = self._call("conversations.history", {
            "channel": channel,
            "latest": ts,
            "limit": 1,
            "inclusive": "true",
        })
        messages = data.get("messages", [])
        if not messages:
            raise RuntimeError(f"Message not found: {channel}/{ts}")
        return messages[0]

    def get_thread(self, channel: str, thread_ts: str) -> list[dict]:
        data = self._call("conversations.replies", {
            "channel": channel,
            "ts": thread_ts,
            "limit": 200,
        })
        return data.get("messages", [])

    def get_context(self, channel: str, ts: str, count: int = 16) -> list[dict]:
        data = self._call("conversations.history", {
            "channel": channel,
            "latest": ts,
            "limit": count,
            "inclusive": "true",
        })
        messages = data.get("messages", [])
        messages.reverse()  # Chronological order
        return messages

    def get_permalink(self, channel: str, ts: str) -> str:
        data = self._call("chat.getPermalink", {
            "channel": channel,
            "message_ts": ts,
        })
        return data.get("permalink", "")

    def resolve_user(self, user_id: str) -> str:
        if user_id in self.state.users:
            return self.state.users[user_id]
        try:
            data = self._call("users.info", {"user": user_id})
            profile = data.get("user", {}).get("profile", {})
            name = (
                profile.get("display_name")
                or profile.get("real_name")
                or user_id
            )
            self.state.users[user_id] = name
            self.state.save_users_cache()
            return name
        except Exception as e:
            log.warning("Failed to resolve user %s: %s", user_id, e)
            return user_id

    def resolve_channel(self, channel_id: str) -> str:
        if channel_id in self.state.channels:
            return self.state.channels[channel_id]
        try:
            data = self._call("conversations.info", {"channel": channel_id})
            ch = data.get("channel", {})
            name = ch.get("name") or ch.get("name_normalized") or channel_id
            if not ch.get("is_im") and not ch.get("is_mpim"):
                name = f"#{name}"
            self.state.channels[channel_id] = name
            self.state.save_channels_cache()
            return name
        except Exception as e:
            log.warning("Failed to resolve channel %s: %s", channel_id, e)
            return channel_id


# ---------------------------------------------------------------------------
# Poller — checks reactions.list periodically
# ---------------------------------------------------------------------------


class ReactionPoller:
    """Polls Slack for new :cap: reactions and enqueues captures."""

    def __init__(self, state: HarvesterState, client: SlackClient,
                 worker: "CaptureWorker", interval: int, reactions: list):
        self.state = state
        self.client = client
        self.worker = worker
        self.interval = interval
        self.reactions = reactions
        self.user_id: Optional[str] = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def _run(self):
        while True:
            if not self.state.has_credentials():
                log.debug("No credentials yet, sleeping %ds", self.interval)
                time.sleep(self.interval)
                continue

            try:
                self._poll()
            except RuntimeError as e:
                if "No credentials" in str(e) or "invalid_auth" in str(e):
                    log.warning("Credentials invalid, refreshing from Chrome profile...")
                    self.state.refresh_credentials()
                else:
                    log.error("Poll error: %s", e)
            except Exception as e:
                log.error("Poll error: %s", e, exc_info=True)

            time.sleep(self.interval)

    def _poll(self):
        # Discover our user ID on first poll
        if not self.user_id:
            auth = self.client.get_authed_user()
            self.user_id = auth.get("user_id")
            log.info("Authenticated as user %s (%s)", auth.get("user"), self.user_id)

        matches = self.client.search_reactions(self.reactions, count=20)
        new_count = 0

        for msg in matches:
            # search.messages returns channel as an object or ID
            channel_info = msg.get("channel", {})
            channel = channel_info.get("id") if isinstance(channel_info, dict) else channel_info
            ts = msg.get("ts")

            if not channel or not ts:
                continue

            dedup_key = f"{channel}:{ts}"
            if self.state.is_seen(dedup_key):
                continue

            # New capture!
            log.info("New :cap: found: channel=%s ts=%s text=%s",
                     channel, ts, (msg.get("text") or "")[:60])
            self.state.mark_seen(dedup_key)
            self.worker.enqueue({
                "channel": channel,
                "ts": ts,
                "workspace_domain": "app.slack.com",
            })
            new_count += 1

        if new_count:
            log.info("Enqueued %d new capture(s)", new_count)
        else:
            log.debug("Poll: no new :cap: reactions (%d total matches)", len(matches))


# ---------------------------------------------------------------------------
# Worker — serial capture processor
# ---------------------------------------------------------------------------


class CaptureWorker:
    """Processes capture events serially in a background thread."""

    def __init__(self, state: HarvesterState, client: SlackClient, opencode_cmd: str = "opencode"):
        self.state = state
        self.client = client
        self.opencode_cmd = opencode_cmd
        self.queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def enqueue(self, event: dict):
        self.queue.put(event)
        log.info("Queued capture (depth: %d)", self.queue.qsize())

    def _run(self):
        while True:
            event = self.queue.get()
            try:
                self._process(event)
            except Exception as e:
                log.error("Capture failed: %s", e, exc_info=True)
                self._park_pending(event, str(e))
            finally:
                self.queue.task_done()

    def _process(self, event: dict):
        channel = event["channel"]
        ts = event["ts"]

        log.info("Processing capture: %s/%s", channel, ts)

        # Step 1: Fetch the reacted message to check for thread_ts
        msg = self.client.get_message(channel, ts)
        thread_ts = msg.get("thread_ts")

        # Step 2: Fetch context
        if thread_ts:
            messages = self.client.get_thread(channel, thread_ts)
            context_type = "thread"
        else:
            messages = self.client.get_context(channel, ts, count=16)
            context_type = "channel"

        # Step 3: Resolve names
        participants = set()
        for m in messages:
            uid = m.get("user")
            if uid:
                name = self.client.resolve_user(uid)
                m["_display_name"] = name
                participants.add(name)

        channel_name = self.client.resolve_channel(channel)

        # Step 4: Get permalink
        permalink = self.client.get_permalink(channel, ts)

        # Step 5: Determine author
        author = msg.get("_display_name", self.client.resolve_user(msg.get("user", "unknown")))

        # Step 6: Extract workspace from permalink
        workspace = "unknown"
        if permalink and "://" in permalink:
            host = permalink.split("://")[1].split(".")[0]
            if host and host != "app":
                workspace = host

        # Step 7: Convert Slack ts to ISO datetime
        msg_date = datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
        reacted_text = msg.get("text", "")

        # Step 8: Build the bundle for opencode
        bundle = {
            "channel": channel_name,
            "channel_id": channel,
            "workspace": workspace,
            "author": author,
            "participants": sorted(participants),
            "permalink": permalink,
            "message_date": msg_date,
            "reacted_message_text": reacted_text,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "slack_ts": ts,
            "thread_ts": thread_ts,
            "context_type": context_type,
            "reacted_message": {
                "user": author,
                "text": reacted_text,
                "ts": ts,
            },
            "messages": [
                {
                    "user": m.get("_display_name", m.get("user", "?")),
                    "text": m.get("text", ""),
                    "ts": m.get("ts", ""),
                    "attachments": [
                        a.get("fallback") or a.get("text") or a.get("title", "")
                        for a in m.get("attachments", [])
                    ],
                    "files": [
                        {
                            "name": f.get("name", ""),
                            "url": f.get("url_private", f.get("permalink", "")),
                            "mimetype": f.get("mimetype", ""),
                        }
                        for f in m.get("files", [])
                    ],
                }
                for m in messages
            ],
        }

        # Step 9: Invoke opencode
        self._invoke_opencode(bundle)

    def _invoke_opencode(self, bundle: dict):
        cap_dir = self.state.capture_dir
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        out_dir = self.state.vault / cap_dir / date_str
        out_dir.mkdir(parents=True, exist_ok=True)

        bundle_json = json.dumps(bundle, indent=2)

        prompt = f"""You are processing a captured Slack message. Write it to the vault as a single Markdown file.

## Input data (JSON)

```json
{bundle_json}
```

## Instructions

1. Create exactly ONE file at: `{cap_dir}/{date_str}/{{slug}}.md`
   - Slug format: `{date_str}-{{author-lastname}}-{{3-word-summary}}`
   - Use lowercase, hyphens, no spaces
   - The 3-word summary should capture the core topic

2. Frontmatter (YAML):
```yaml
---
source: slack
workspace: {bundle['workspace']}
channel: "{bundle['channel']}"
author: "{bundle['author']}"
participants: {json.dumps(bundle['participants'])}
permalink: {bundle['permalink']}
message_date: {bundle['message_date']}
reacted_message: "{bundle['reacted_message_text'][:200]}"
captured_at: {bundle['captured_at']}
slack_ts: "{bundle['slack_ts']}"
tags: []
---
```

3. Body:
   - Start with a 1-2 sentence summary of what was captured and why it matters
   - Include the key content — decisions, action items, links, important context
   - If this is a thread, preserve the thread structure with author attribution
   - If this is channel context, focus on the reacted message and include relevant surrounding context only
   - Filter out noise (reactions-only messages, "thanks", join/leave, etc.)
   - If you find references to people, use [[Display Name]] wiki-links
   - Preserve any code blocks, links, or structured data

4. Do NOT edit any other files in the vault.
5. Do NOT create more than one file.
"""

        try:
            result = subprocess.run(
                [self.opencode_cmd, "run", prompt],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=str(self.state.vault),
            )

            if result.returncode != 0:
                log.error("opencode failed (rc=%d): %s", result.returncode, result.stderr[:500])
                raise RuntimeError(f"opencode exited {result.returncode}: {result.stderr[:200]}")

            log.info("Capture written for %s/%s", bundle["channel"], bundle["slack_ts"])

        except subprocess.TimeoutExpired:
            log.error("opencode timed out after 120s")
            raise RuntimeError("opencode timed out")

    def _park_pending(self, event: dict, error: str):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        pending_dir = self.state.vault / self.state.capture_dir / "_pending"
        pending_dir.mkdir(parents=True, exist_ok=True)

        pending_file = pending_dir / f"{ts}.json"
        payload = {
            "event": event,
            "error": error,
            "failed_at": datetime.now(timezone.utc).isoformat(),
        }
        pending_file.write_text(json.dumps(payload, indent=2))
        log.info("Parked failed capture to %s", pending_file)


# ---------------------------------------------------------------------------
# HTTP handler (credentials + health only)
# ---------------------------------------------------------------------------


class HarvestHandler(BaseHTTPRequestHandler):
    state: HarvesterState
    worker: "CaptureWorker"

    def do_GET(self):
        if self.path == "/health":
            self._respond(200, {
                "status": "ok",
                "has_credentials": self.state.has_credentials(),
                "seen_count": len(self.state.seen),
                "queue_depth": self.worker.queue.qsize(),
            })
        else:
            self._respond(404, {"error": "not found"})

    def _respond(self, code: int, body: dict):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, fmt, *args):
        log.debug(fmt, *args)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Slack Harvester")
    parser.add_argument("--config", type=Path,
                        default=SCRIPT_DIR / "config.json",
                        help="Path to config.json (default: alongside this script)")
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = load_config(args.config)

    vault = Path(cfg["vault_path"]).expanduser().resolve()
    if not vault.exists():
        log.error("Vault path does not exist: %s", vault)
        sys.exit(1)

    chrome_profile = Path(cfg["chrome_profile"]).expanduser().resolve()
    if not chrome_profile.exists():
        log.error("Chrome profile does not exist: %s", chrome_profile)
        log.error("Run setup.sh first to sign into Slack.")
        sys.exit(1)

    capture_dir = cfg["capture_dir"]
    interval = cfg["poll_interval"]
    reactions = cfg["reactions"]
    opencode_cmd = cfg.get("opencode_command", "opencode")
    port = cfg.get("health_port", 7777)

    log.info("Vault: %s", vault)
    log.info("Capture dir: %s/%s", vault, capture_dir)
    log.info("Chrome profile: %s", chrome_profile)
    log.info("Poll interval: %ds", interval)
    log.info("Trigger reactions: %s", ", ".join(f":{r}:" for r in reactions))

    state = HarvesterState(vault, capture_dir, chrome_profile)
    client = SlackClient(state)
    worker = CaptureWorker(state, client, opencode_cmd)
    poller = ReactionPoller(state, client, worker, interval, reactions)

    HarvestHandler.state = state
    HarvestHandler.worker = worker

    poller.start()
    log.info("Poller started")

    server = HTTPServer(("127.0.0.1", port), HarvestHandler)
    log.info("Health check: http://127.0.0.1:%d/health", port)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log.info("Shutting down")
        server.shutdown()


if __name__ == "__main__":
    main()
