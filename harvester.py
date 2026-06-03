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
    "state_dir": "~/.local/state/slack-harvester",
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

    def __init__(self, vault: Path, capture_dir: str, chrome_profile: Path,
                 state_dir: Optional[Path] = None):
        self.vault = vault
        self.capture_dir = capture_dir
        self.chrome_profile = chrome_profile
        self.state_dir = state_dir or (vault / capture_dir / "_state")
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

    def unmark_seen(self, key: str):
        """Remove a key from seen.json. Used to retry failed captures.

        See ISSUES.md #2 — failure path un-marks so the next poll re-processes.
        """
        with self._lock:
            if key in self.seen:
                del self.seen[key]
                self._save_json(self.seen_path, self.seen)
                log.info("Un-marked seen: %s (will retry on next poll)", key)

    def find_orphaned_seen(self, max_age_days: int = 30) -> list[str]:
        """Find seen.json entries with no corresponding capture file on disk.

        These are silent losses — opencode returned rc=0 (so we marked seen)
        but never wrote a file. See ISSUES.md #10.

        Skips:
        - entries older than max_age_days (Slack search.messages may not return
          ancient reactions, so un-marking is futile)
        - entries with a matching _pending/*.json (these are known-failed with
          a paper trail, not silent losses)

        Returns the list of orphaned dedup keys.
        """
        capture_root = self.vault / self.capture_dir
        pending_dir = capture_root / "_pending"
        cutoff = datetime.now(timezone.utc).timestamp() - (max_age_days * 86400)

        # Build a set of pending dedup keys (channel:ts) for skip-check.
        pending_keys: set[str] = set()
        if pending_dir.exists():
            for pf in pending_dir.glob("*.json"):
                try:
                    payload = json.loads(pf.read_text())
                    ev = payload.get("event", {})
                    ch, ts = ev.get("channel"), ev.get("ts")
                    if ch and ts:
                        pending_keys.add(f"{ch}:{ts}")
                except (json.JSONDecodeError, OSError):
                    continue

        orphans: list[str] = []
        with self._lock:
            for key, marked_iso in self.seen.items():
                # Skip if there's a paper trail in _pending/.
                if key in pending_keys:
                    continue

                # Parse the dedup key's Slack ts (key is "channel:ts").
                try:
                    _, ts_str = key.split(":", 1)
                    msg_ts = float(ts_str)
                except (ValueError, IndexError):
                    continue
                if msg_ts < cutoff:
                    continue

                # Date-dir to check is derived from the message ts (UTC),
                # matching _invoke_opencode's date_str logic.
                msg_date = datetime.fromtimestamp(msg_ts, tz=timezone.utc).strftime("%Y-%m-%d")
                date_dir = capture_root / msg_date
                if not date_dir.exists():
                    orphans.append(key)
                    continue

                # Look for any .md file referencing this slack_ts in frontmatter.
                # Capture file names use a slug, not the ts, so we have to grep.
                found = False
                for md in date_dir.glob("*.md"):
                    try:
                        # Cheap check: ts string appears in the first 2KB
                        # (frontmatter `slack_ts:` line).
                        head = md.read_text()[:2048]
                        if ts_str in head:
                            found = True
                            break
                    except OSError:
                        continue
                if not found:
                    orphans.append(key)

        return orphans

    def recovery_sweep(self, max_age_days: int = 30) -> int:
        """Find and un-mark orphaned seen entries. Returns count un-marked.

        See ISSUES.md #10.
        """
        orphans = self.find_orphaned_seen(max_age_days=max_age_days)
        for key in orphans:
            self.unmark_seen(key)
        return len(orphans)

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
                # Un-mark seen so the next poll retries this capture.
                # ISSUES.md #2 — without this, any opencode failure
                # permanently loses the capture.
                dedup_key = f"{event.get('channel')}:{event.get('ts')}"
                self.state.unmark_seen(dedup_key)
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
        """Generate markdown body via opencode (stdout-only), write file in Python.

        Splitting the responsibilities:
        - opencode: turns the Slack bundle into curated markdown body text.
          Stdout-only; no filesystem access required.
        - Python: computes the slug, builds frontmatter, writes the file,
          verifies it landed on disk.

        This eliminates the silent-failure class where opencode returns rc=0
        but never invokes a Write tool (ISSUES.md #1). Also reduces blast
        radius (#5) — opencode has no path to write outside our control.
        """
        cap_dir = self.state.capture_dir
        msg_ts = float(bundle["slack_ts"])
        date_str = datetime.fromtimestamp(msg_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        out_dir = self.state.vault / cap_dir / date_str
        out_dir.mkdir(parents=True, exist_ok=True)

        body = self._generate_body_via_opencode(bundle)
        slug = self._build_slug(bundle, date_str)
        frontmatter = self._build_frontmatter(bundle)

        out_path = out_dir / f"{slug}.md"
        # Guard against slug collisions (same author + same 3-word topic on
        # the same day). Append a suffix derived from the slack ts.
        if out_path.exists():
            suffix = bundle["slack_ts"].replace(".", "")[-6:]
            out_path = out_dir / f"{slug}-{suffix}.md"

        out_path.write_text(frontmatter + "\n" + body.strip() + "\n")
        if not out_path.exists() or out_path.stat().st_size == 0:
            raise RuntimeError(f"Wrote {out_path} but file is missing or empty")

        log.info("Capture written: %s (%d bytes)",
                 out_path.relative_to(self.state.vault), out_path.stat().st_size)

    def _generate_body_via_opencode(self, bundle: dict) -> str:
        """Run opencode to turn the bundle into curated markdown body text.

        Returns the body string. Raises RuntimeError on any failure mode
        (non-zero rc, timeout, empty stdout).
        """
        bundle_json = json.dumps(bundle, indent=2)
        prompt = f"""You are processing a captured Slack message. Return ONLY the markdown body text. No frontmatter. No preamble. No "Here is the markdown" wrapper. No code fence around the whole output. Just the body content, ready to be appended to a markdown file.

## Input data (JSON)

```json
{bundle_json}
```

## Body requirements

- Start with a 1-2 sentence summary of what was captured and why it matters.
- Include the key content — decisions, action items, links, important context.
- If this is a thread, preserve the thread structure with author attribution.
- If this is channel context, focus on the reacted message and include relevant surrounding context only.
- Filter out noise (reactions-only messages, "thanks", join/leave, etc.).
- For references to people, use `[[Display Name]]` wiki-links.
- Preserve code blocks, links, and structured data inside the body.

Return only the markdown body. Do not invoke any tools. Do not write to any files. Do not output anything other than the body text.
"""
        try:
            result = subprocess.run(
                [self.opencode_cmd, "run", prompt],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=str(self.state.vault),
            )
        except subprocess.TimeoutExpired:
            log.error("opencode timed out after 120s")
            raise RuntimeError("opencode timed out")

        stdout = result.stdout or ""
        stderr = (result.stderr or "").strip()

        if stderr:
            # opencode prints ANSI escape codes and the agent/model banner
            # to stderr. Log it summarized for diagnostic value.
            log.debug("opencode stderr: %s", stderr[:300])

        if result.returncode != 0:
            log.error("opencode failed (rc=%d): %s", result.returncode, stderr[:200])
            raise RuntimeError(f"opencode exited {result.returncode}: {stderr[:200]}")

        body = stdout.strip()
        if not body:
            raise RuntimeError(
                f"opencode returned empty stdout (rc=0, stderr: {stderr[:200]!r})"
            )

        return body

    @staticmethod
    def _build_slug(bundle: dict, date_str: str) -> str:
        """Build a filename slug: `{date}-{author-last-name}-{3-word-topic}`.

        Deterministic, no model involvement. Derives the topic from the
        reacted message's first words, lowercased, alphanumeric only.
        """
        import re

        # Author last name. "First Last" -> "last". Falls back to full
        # display name slugified if there's no obvious split.
        author = bundle.get("author") or "unknown"
        author_parts = author.strip().split()
        last_name = author_parts[-1] if author_parts else "unknown"
        last_name_slug = re.sub(r"[^a-z0-9]+", "", last_name.lower()) or "unknown"

        # Topic: first 3 word-like tokens from the reacted message text.
        text = bundle.get("reacted_message_text") or ""
        # Strip Slack markup (user mentions, channel mentions, links).
        text = re.sub(r"<[^>]+>", " ", text)
        words = re.findall(r"[a-zA-Z0-9]+", text.lower())
        # Skip very short / stopword-y tokens.
        stop = {"the", "a", "an", "of", "to", "and", "or", "is", "in", "on",
                "for", "with", "at", "by", "from", "as", "it", "be", "do",
                "i", "you", "we", "they", "he", "she"}
        topic_words = [w for w in words if w not in stop and len(w) >= 2][:3]
        if not topic_words:
            topic_words = ["capture"]
        topic_slug = "-".join(topic_words)

        return f"{date_str}-{last_name_slug}-{topic_slug}"

    @staticmethod
    def _build_frontmatter(bundle: dict) -> str:
        """Build the YAML frontmatter block for a capture file.

        Deterministic. Mirrors the contract previously baked into the
        opencode prompt.
        """
        # Trim reacted message to 200 chars and escape for safe YAML quoting.
        # Strategy: replace " with ' so the outer "..." quoting stays valid,
        # collapse newlines to spaces.
        reacted = (bundle.get("reacted_message_text") or "")[:200]
        reacted = reacted.replace("\n", " ").replace('"', "'")

        lines = [
            "---",
            "source: slack",
            f"workspace: {bundle.get('workspace', 'unknown')}",
            f'channel: "{bundle.get("channel", "")}"',
            f'author: "{bundle.get("author", "")}"',
            f"participants: {json.dumps(bundle.get('participants', []))}",
            f"permalink: {bundle.get('permalink', '')}",
            f"message_date: {bundle.get('message_date', '')}",
            f'reacted_message: "{reacted}"',
            f"captured_at: {bundle.get('captured_at', '')}",
            f'slack_ts: "{bundle.get("slack_ts", "")}"',
            "tags: []",
            "---",
        ]
        return "\n".join(lines)

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
# Startup self-test (ISSUES.md #9a)
# ---------------------------------------------------------------------------


def _dm_self(client: SlackClient, text: str) -> bool:
    """DM the authenticated user. Used for startup-failure alerts.

    Returns True on success, False on any failure. Errors are swallowed —
    this is a best-effort alert path, not a critical-path operation.
    """
    try:
        auth = client.get_authed_user()
        user_id = auth.get("user_id")
        if not user_id:
            return False
        dm = client._call("conversations.open", {"users": user_id})
        channel = dm.get("channel", {}).get("id")
        if not channel:
            return False
        client._call("chat.postMessage", {"channel": channel, "text": text})
        return True
    except Exception as e:
        log.warning("Self-DM failed (alert lost): %s", e)
        return False


def startup_self_test(client: SlackClient, opencode_cmd: str) -> list[str]:
    """Verify Slack auth and opencode invocation work post-boot.

    Returns a list of failure messages (empty list = healthy).
    See ISSUES.md #9a.
    """
    failures: list[str] = []

    # Test 1: Slack auth.test
    try:
        auth = client.get_authed_user()
        log.info("Self-test: Slack auth OK (user=%s, user_id=%s)",
                 auth.get("user"), auth.get("user_id"))
    except Exception as e:
        msg = f"Slack auth.test failed: {e}"
        log.error("Self-test: %s", msg)
        failures.append(msg)

    # Test 2: opencode stdout round-trip.
    # The harvester only uses opencode to generate body text (stdout-only,
    # no filesystem access). The probe matches that contract: ask for a
    # single token on stdout, verify it appears.
    try:
        result = subprocess.run(
            [opencode_cmd, "run",
             "Reply with the single word PONG and nothing else. "
             "Do not invoke any tools. Do not write to any files."],
            capture_output=True,
            text=True,
            timeout=120,
        )
        stdout = (result.stdout or "").strip()
        if result.returncode != 0:
            msg = (f"opencode probe rc={result.returncode}: "
                   f"{(result.stderr or '').strip()[:200]}")
            log.error("Self-test: %s", msg)
            failures.append(msg)
        elif "PONG" not in stdout.upper():
            msg = (f"opencode probe rc=0 but PONG not in stdout. "
                   f"got: {stdout[:200]!r}")
            log.error("Self-test: %s", msg)
            failures.append(msg)
        else:
            log.info("Self-test: opencode stdout probe OK")
    except subprocess.TimeoutExpired:
        msg = "opencode probe timed out after 120s"
        log.error("Self-test: %s", msg)
        failures.append(msg)
    except FileNotFoundError as e:
        msg = f"opencode binary not found: {e}"
        log.error("Self-test: %s", msg)
        failures.append(msg)
    except Exception as e:
        msg = f"opencode probe raised: {e}"
        log.error("Self-test: %s", msg)
        failures.append(msg)

    return failures


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
    state_dir = Path(cfg["state_dir"]).expanduser().resolve() if cfg.get("state_dir") else None
    interval = cfg["poll_interval"]
    reactions = cfg["reactions"]
    opencode_cmd = cfg.get("opencode_command", "opencode")
    port = cfg.get("health_port", 7777)

    log.info("Vault: %s", vault)
    log.info("Capture dir: %s/%s", vault, capture_dir)
    log.info("State dir: %s", state_dir or f"{vault}/{capture_dir}/_state")
    log.info("Chrome profile: %s", chrome_profile)
    log.info("Poll interval: %ds", interval)
    log.info("Trigger reactions: %s", ", ".join(f":{r}:" for r in reactions))

    state = HarvesterState(vault, capture_dir, chrome_profile, state_dir=state_dir)
    client = SlackClient(state)
    worker = CaptureWorker(state, client, opencode_cmd)
    poller = ReactionPoller(state, client, worker, interval, reactions)

    HarvestHandler.state = state
    HarvestHandler.worker = worker

    # Recovery sweep (ISSUES.md #10): un-mark seen entries with no corresponding
    # .md file in the expected dated dir. Catches silent losses from before
    # ISSUES.md #1's verification landed. Runs every startup; cheap (only scans
    # seen.json entries from the last 30 days).
    if state.has_credentials():
        try:
            recovered = state.recovery_sweep(max_age_days=30)
            if recovered:
                log.info("Recovery sweep: un-marked %d historical orphan(s); "
                         "will retry on next poll", recovered)
            else:
                log.info("Recovery sweep: no orphans found")
        except Exception as e:
            log.error("Recovery sweep failed: %s", e, exc_info=True)
    else:
        log.warning("Recovery sweep skipped: no credentials at startup")

    # Self-test (ISSUES.md #9a): verify Slack auth + opencode round-trip work
    # post-boot. If either fails, DM Matt immediately rather than waiting for
    # the 5-min healthcheck cycle to notice. Doesn't block startup — harvester
    # continues even if self-test fails, so a partial outage (e.g. opencode
    # broken but Slack auth fine) doesn't prevent the sink from at least
    # parking captures to _pending/.
    if state.has_credentials():
        failures = startup_self_test(client, opencode_cmd)
        if failures:
            alert = (":rotating_light: *Slack Harvester startup self-test failed*\n\n"
                     + "\n".join(f"\u2022 {f}" for f in failures)
                     + "\n\nLog: `tail -100 /private/tmp/harvester.log`")
            _dm_self(client, alert)
        else:
            log.info("Startup self-test: all checks passed")
    else:
        log.warning("Self-test skipped: no credentials at startup")

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
