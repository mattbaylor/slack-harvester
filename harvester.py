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

# Asset capture (folder-per-capture layout). Files attached to a Slack message
# are downloaded into the capture folder as 01.ext, 02.ext, etc.
ASSET_MAX_BYTES = 100 * 1024 * 1024   # 100 MB; larger files skip and park.
ASSET_DOWNLOAD_TIMEOUT = 60            # Per-file, seconds.

# MIME-prefixes that should be embedded inline in the body. Everything else
# (PDF, video, zip, etc.) gets a plain link in the Files appendix only.
ASSET_INLINE_MIME_PREFIXES = ("image/",)

# Magic-byte prefixes for the image formats Slack will serve. Used to detect
# the case where Slack returns 200 OK with the sign-in HTML page in place of
# the image (auth failure manifesting as content corruption, not HTTP error).
# See ISSUES.md #12.
IMAGE_MAGIC_BYTES = (
    b"\x89PNG\r\n\x1a\n",   # PNG
    b"\xff\xd8\xff",         # JPEG
    b"GIF87a",
    b"GIF89a",
    b"BM",                   # BMP
    b"<svg",                 # SVG (also handled via _looks_like_html for <?xml)
    b"<?xml",                # SVG with XML prolog
)
# WebP and HEIC have a magic at offset 8 (RIFF/ftyp container), checked separately.

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

                # Look for any capture file referencing this slack_ts in
                # frontmatter. Capture file names use a slug, not the ts,
                # so we have to grep. Two layouts to support:
                # - Folder-per-capture (current): {date_dir}/{slug}/capture.md
                # - Flat (historical / pre-2026-06-10): {date_dir}/{slug}.md
                # Migration moves all historical captures to the folder
                # layout, but during transition or for any forgotten
                # straggler we still scan both shapes.
                found = False
                candidates = list(date_dir.glob("*/capture.md")) + list(date_dir.glob("*.md"))
                for md in candidates:
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

    def recovery_sweep(self, max_age_days: int = 30,
                       auto_unmark: bool = False) -> list[str]:
        """Find orphaned seen entries (capture lost or never written).

        See ISSUES.md #10 (original feature) and #11 (2026-06-10 cloud-sync
        race; this method was changed from auto-act to report-only by default).

        Behavior:
        - Returns the list of orphan dedup keys without modifying seen.json.
        - When auto_unmark=True, un-marks each found orphan (legacy
          behavior; used by the explicit --recover CLI flag).

        Why not auto-act at startup: the filesystem the harvester scans can
        be cloud-synced (GoogleDrive, Dropbox), network-mounted, or
        otherwise eventually-consistent. A startup scan after a recent
        write may see an inconsistent view and flag real captures as
        orphans. Auto-un-marking then causes the next poll to re-process
        and produce duplicate folders (the 2026-06-10 incident).

        The cure for genuine silent-loss recovery is to run
        `harvester.py --recover` after manually confirming the
        filesystem is in steady state.
        """
        orphans = self.find_orphaned_seen(max_age_days=max_age_days)

        if orphans:
            log.warning(
                "Recovery sweep: found %d candidate orphan(s) in seen.json "
                "without a matching capture file on disk. NOT auto-un-marking "
                "(see ISSUES.md #11). To recover, after confirming the "
                "filesystem is in steady state run: "
                "`python harvester.py --recover`",
                len(orphans),
            )
            for key in orphans:
                log.warning("  candidate orphan: %s", key)

        if auto_unmark:
            for key in orphans:
                self.unmark_seen(key)

        return orphans

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

    def download_file(self, url: str, dest: Path, timeout: int = 60,
                      max_bytes: Optional[int] = None,
                      expected_mimetype: Optional[str] = None) -> int:
        """Download a Slack-hosted file (url_private or similar) to dest.

        Auth model: Slack file CDN requires BOTH the xoxc Bearer token AND the
        d cookie. Without the cookie, files.slack.com returns 200 OK with the
        sign-in HTML page as the body — the Content-Type may even mirror the
        requested file type. This produces silent corruption (e.g. a 67KB
        "image/png" that is actually HTML). See ISSUES.md #12.

        Guards (defense in depth):
          1. Both token + cookie required (mirrors Web API auth).
          2. Response Content-Type sniffed: text/html or similar → auth failure.
          3. First bytes magic-sniffed for HTML.
          4. If expected_mimetype starts with "image/", body must begin with a
             known image magic; else treated as content corruption.

        Args:
            url: Slack file URL (typically url_private from files[]).
            dest: Destination path. Parent directory must exist.
            timeout: Per-request timeout in seconds.
            max_bytes: Optional size cap. If the response Content-Length
                       exceeds this, the download is aborted before writing.
                       If Content-Length is missing, streams up to max_bytes
                       and aborts if exceeded.
            expected_mimetype: If provided and starts with "image/", validates
                       the downloaded bytes match a known image magic.

        Returns:
            Bytes written.

        Raises:
            RuntimeError on auth failure (HTTP or HTML-body sniff), transport
            failure, size cap, or content-type mismatch.
        """
        if not self.state.token or not self.state.cookie:
            raise RuntimeError("No credentials for file download (need token+cookie)")

        req = Request(url, method="GET")
        req.add_header("Authorization", f"Bearer {self.state.token}")
        req.add_header("Cookie", f"d={self.state.cookie}")

        try:
            resp = urlopen(req, timeout=timeout)
        except Exception as e:
            raise RuntimeError(f"Download failed (transport): {e}")

        # Response Content-Type guard. Slack returns HTML on auth failure even
        # when the URL implies an image; catch that before writing anything.
        resp_ct = (resp.headers.get("Content-Type") or "").lower()
        if "html" in resp_ct or "text/html" in resp_ct:
            raise RuntimeError(
                f"Download produced HTML response (auth failure?); "
                f"Content-Type={resp_ct!r}"
            )

        # Pre-flight size check from Content-Length when available.
        cl = resp.headers.get("Content-Length")
        if cl is not None and max_bytes is not None:
            try:
                if int(cl) > max_bytes:
                    raise RuntimeError(
                        f"File too large: Content-Length {cl} > max_bytes {max_bytes}"
                    )
            except ValueError:
                pass  # Bad header, fall through to streamed cap

        # Stream to disk with a streaming cap as a backstop. Capture the first
        # chunk's leading bytes for magic-byte validation after the stream.
        bytes_written = 0
        first_bytes = b""
        chunk = 64 * 1024
        try:
            with open(dest, "wb") as f:
                while True:
                    buf = resp.read(chunk)
                    if not buf:
                        break
                    if not first_bytes:
                        first_bytes = buf[:32]
                    bytes_written += len(buf)
                    if max_bytes is not None and bytes_written > max_bytes:
                        # Abort and unlink the partial file.
                        f.close()
                        try:
                            dest.unlink()
                        except OSError:
                            pass
                        raise RuntimeError(
                            f"File too large: exceeded max_bytes {max_bytes} mid-stream"
                        )
                    f.write(buf)
        except RuntimeError:
            raise
        except Exception as e:
            # Clean up partial file on any other error.
            try:
                dest.unlink()
            except OSError:
                pass
            raise RuntimeError(f"Download failed (write): {e}")

        if bytes_written == 0:
            try:
                dest.unlink()
            except OSError:
                pass
            raise RuntimeError("Download produced empty file (auth or URL issue?)")

        # HTML sniff on body bytes (independent of response Content-Type header,
        # in case Slack returns text/html as application/octet-stream).
        if self._looks_like_html(first_bytes):
            try:
                dest.unlink()
            except OSError:
                pass
            raise RuntimeError(
                "Download produced HTML body (auth failure?); "
                f"first bytes={first_bytes[:16]!r}"
            )

        # Image magic-byte validation when the caller declared an image.
        if expected_mimetype and expected_mimetype.lower().startswith("image/"):
            if not self._looks_like_image(first_bytes):
                try:
                    dest.unlink()
                except OSError:
                    pass
                raise RuntimeError(
                    f"Download mimetype mismatch: expected {expected_mimetype}, "
                    f"got bytes that don't match any known image magic "
                    f"(first 16 bytes={first_bytes[:16]!r})"
                )

        return bytes_written

    @staticmethod
    def _looks_like_html(buf: bytes) -> bool:
        """Heuristic: does this byte string look like the start of an HTML doc?

        Handles the Slack auth-failure case where the sign-in page comes back
        with arbitrary Content-Type. Case-insensitive on the well-known tags.
        """
        if not buf:
            return False
        head = buf[:64].lstrip().lower()
        return (
            head.startswith(b"<!doctype html")
            or head.startswith(b"<html")
            or head.startswith(b"<head")
        )

    @staticmethod
    def _looks_like_image(buf: bytes) -> bool:
        """Magic-byte check for image formats Slack will serve.

        Covers PNG, JPEG, GIF, BMP, WebP, HEIC, SVG. Returns False for HTML,
        text, or unknown binary content.
        """
        if not buf:
            return False
        # Common magics (prefix match).
        for magic in IMAGE_MAGIC_BYTES:
            if buf.startswith(magic):
                return True
        # WebP: "RIFF....WEBP" at offset 0/8.
        if len(buf) >= 12 and buf[0:4] == b"RIFF" and buf[8:12] == b"WEBP":
            return True
        # HEIC / HEIF: "....ftypheic" / "ftypheix" / "ftypmif1" at offset 4.
        if len(buf) >= 12 and buf[4:8] == b"ftyp" and buf[8:12] in (
            b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis",
            b"mif1", b"msf1",
        ):
            return True
        return False


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
            log.debug("Poll: no new trigger reactions (%d total matches across %d reaction(s))",
                      len(matches), len(self.reactions))


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

        # Step 8: Enumerate asset candidates from the message stream.
        # files[] is the primary source. blocks[].image blocks also count
        # (pasted images in rich-text). attachments[].image_url is skipped
        # deliberately — those are link-unfurl thumbnails (OG image, favicon),
        # noise that doesn't belong in the capture.
        asset_candidates = self._enumerate_assets(messages)

        # Step 9: Build the bundle for opencode body generation.
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

        # Step 10: Write capture folder + assets + capture.md.
        self._invoke_opencode(bundle, asset_candidates)

    @staticmethod
    def _enumerate_assets(messages: list[dict]) -> list[dict]:
        """Walk the message stream, return asset candidates in order.

        Each candidate is a dict with: url, original_name, mimetype, size_hint.
        Order is message order (oldest first), then file order within each
        message. The serialization index (01, 02, …) is assigned later in
        _fetch_assets to keep this side-effect free.

        Sources collected:
        - files[] entries with a url_private (any mimetype — image, PDF,
          video, zip, audio, etc.).
        - blocks[].image blocks (pasted-into-rich-text images).

        Sources deliberately skipped:
        - attachments[].image_url — link-unfurl thumbnails (OG image,
          favicon noise).
        - files[] entries without url_private (rare; tombstoned uploads).
        """
        candidates: list[dict] = []
        seen_urls: set[str] = set()  # Per-capture dedup; same file referenced twice → fetch once.

        for m in messages:
            # files[]
            for f in (m.get("files") or []):
                url = f.get("url_private")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                candidates.append({
                    "url": url,
                    "original_name": f.get("name") or "",
                    "mimetype": f.get("mimetype") or "",
                    "size_hint": f.get("size"),  # Slack reports size in files.info, often present in files[] too.
                    "permalink": f.get("permalink") or "",
                })

            # blocks[].image
            for block in (m.get("blocks") or []):
                if block.get("type") != "image":
                    continue
                url = block.get("image_url") or block.get("slack_file", {}).get("url")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                candidates.append({
                    "url": url,
                    "original_name": block.get("alt_text") or "",
                    "mimetype": "image/unknown",  # blocks don't carry mimetype reliably
                    "size_hint": None,
                    "permalink": "",
                })

        return candidates

    def _fetch_assets(self, candidates: list[dict], dest_dir: Path) -> tuple[list[dict], list[dict]]:
        """Download asset candidates into dest_dir as 01.ext, 02.ext, ….

        Returns (succeeded, failed):
        - succeeded: list of dicts with index, filename, mimetype, original_name, size_bytes, url
        - failed:    list of dicts with index, original_name, url, reason

        Per ASSET_MAX_BYTES, large files are skipped with a recorded reason
        (not raised). Per the plan's failure mode: partial success — the
        capture lands with whatever fetched; failures are surfaced via
        _pending-images.json by the caller.
        """
        succeeded: list[dict] = []
        failed: list[dict] = []

        if not candidates:
            return succeeded, failed

        dest_dir.mkdir(parents=True, exist_ok=True)

        for i, cand in enumerate(candidates, start=1):
            ext = self._infer_extension(cand)
            filename = f"{i:02d}{ext}"
            dest = dest_dir / filename

            try:
                size = self.client.download_file(
                    cand["url"], dest,
                    timeout=ASSET_DOWNLOAD_TIMEOUT,
                    max_bytes=ASSET_MAX_BYTES,
                    expected_mimetype=cand.get("mimetype") or None,
                )
                log.info("  asset %s ← %s (%d bytes, %s)",
                         filename, cand.get("original_name") or "<unnamed>", size,
                         cand.get("mimetype") or "?")
                succeeded.append({
                    "index": i,
                    "filename": filename,
                    "mimetype": cand.get("mimetype") or "",
                    "original_name": cand.get("original_name") or "",
                    "size_bytes": size,
                    "url": cand["url"],
                    "permalink": cand.get("permalink") or "",
                })
            except RuntimeError as e:
                reason = str(e)
                log.warning("  asset %s FAILED: %s — %s",
                            filename, cand.get("original_name") or "<unnamed>", reason)
                failed.append({
                    "index": i,
                    "filename": filename,
                    "original_name": cand.get("original_name") or "",
                    "mimetype": cand.get("mimetype") or "",
                    "url": cand["url"],
                    "permalink": cand.get("permalink") or "",
                    "reason": reason,
                })

        return succeeded, failed

    @staticmethod
    def _infer_extension(cand: dict) -> str:
        """Infer a sensible file extension from a candidate's metadata.

        Priority: original filename extension > mimetype mapping > .bin fallback.
        Always returns a leading dot, e.g. ".png" or ".bin".
        """
        import os as _os

        name = cand.get("original_name") or ""
        if name:
            _, dot_ext = _os.path.splitext(name)
            if dot_ext and len(dot_ext) <= 8 and dot_ext.startswith("."):
                # Normalize to lowercase ASCII.
                ext_clean = "".join(c for c in dot_ext.lower() if c.isalnum() or c == ".")
                if ext_clean and ext_clean.startswith("."):
                    return ext_clean

        mime = (cand.get("mimetype") or "").lower()
        mime_map = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/jpg": ".jpg",
            "image/gif": ".gif",
            "image/webp": ".webp",
            "image/svg+xml": ".svg",
            "image/heic": ".heic",
            "image/heif": ".heif",
            "application/pdf": ".pdf",
            "application/zip": ".zip",
            "video/mp4": ".mp4",
            "video/quicktime": ".mov",
            "video/webm": ".webm",
            "audio/mpeg": ".mp3",
            "audio/mp4": ".m4a",
            "audio/x-m4a": ".m4a",
            "audio/wav": ".wav",
            "text/plain": ".txt",
            "text/csv": ".csv",
            "application/json": ".json",
        }
        if mime in mime_map:
            return mime_map[mime]
        # Last-resort: type/subtype → .subtype if it looks safe.
        if "/" in mime:
            subtype = mime.split("/", 1)[1]
            subtype = "".join(c for c in subtype if c.isalnum())
            if 1 <= len(subtype) <= 8:
                return f".{subtype}"
        return ".bin"

    def _invoke_opencode(self, bundle: dict, asset_candidates: list[dict]):
        """Materialize a capture: folder + assets + capture.md.

        Layout (folder-per-capture, 2026-06-10):

            {vault}/{capture_dir}/YYYY-MM-DD/{slug}/
                capture.md
                01.ext              ← asset 1
                02.ext              ← asset 2
                _pending-images.json  ← only on partial download failure

        Responsibilities:
        - Python: slug, folder creation, asset download, frontmatter,
                  appendix, file write, verification.
        - opencode: body text only (stdout-only; no filesystem access).

        The stdout-only opencode contract from ISSUES.md #1/#5 is preserved.
        Adding assets does NOT give opencode any path to write to the
        filesystem — Python remains the sole writer.
        """
        cap_dir = self.state.capture_dir
        msg_ts = float(bundle["slack_ts"])
        date_str = datetime.fromtimestamp(msg_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        date_dir = self.state.vault / cap_dir / date_str
        slug = self._build_slug(bundle, date_str)

        # Folder-collision guard. Same author + same 3-word topic on the
        # same day → suffix folder name with last 6 of slack ts.
        #
        # A folder existing without `capture.md` is NOT a real collision —
        # it's a half-built attempt from a previous failure (e.g. opencode
        # error after assets downloaded). In that case we re-use the folder
        # and let the asset re-download (or the cleanup-then-rebuild flow)
        # complete it. Only when an actual successful prior capture lives
        # in `{slug}/capture.md` do we suffix to avoid clobbering it.
        capture_dir_path = date_dir / slug
        if capture_dir_path.exists() and (capture_dir_path / "capture.md").exists():
            suffix = bundle["slack_ts"].replace(".", "")[-6:]
            capture_dir_path = date_dir / f"{slug}-{suffix}"
        # Track whether we created this folder so the failure path can
        # clean it up without nuking unrelated content.
        folder_was_new = not capture_dir_path.exists()
        capture_dir_path.mkdir(parents=True, exist_ok=True)

        try:
            # Asset download FIRST so the body generation can be informed
            # about what assets exist (filename, mimetype) and embed them.
            # On partial failure: succeeded list lands in the folder; failed
            # list is parked as _pending-images.json (capture still ships).
            succeeded, failed = self._fetch_assets(asset_candidates, capture_dir_path)

            if failed:
                pending_path = capture_dir_path / "_pending-images.json"
                pending_payload = {
                    "capture_slug": capture_dir_path.name,
                    "failed_at": datetime.now(timezone.utc).isoformat(),
                    "items": failed,
                }
                pending_path.write_text(json.dumps(pending_payload, indent=2) + "\n")
                log.warning("Parked %d asset(s) as %s",
                            len(failed),
                            pending_path.relative_to(self.state.vault))

            # Generate the body. Pass succeeded assets so the prompt can
            # instruct embeds at the right anchor (per-message? per-thread?).
            body = self._generate_body_via_opencode(bundle, succeeded)

            # Mechanically append a Files appendix from the succeeded list
            # (plus failed entries, marked). Defensive — even if the model
            # forgets the inline embeds, the appendix guarantees discoverability.
            appendix = self._build_appendix(succeeded, failed)

            frontmatter = self._build_frontmatter(bundle, succeeded, failed)

            out_path = capture_dir_path / "capture.md"

            composed = frontmatter + "\n" + body.strip()
            if appendix:
                composed += "\n\n" + appendix
            composed += "\n"

            out_path.write_text(composed)
            if not out_path.exists() or out_path.stat().st_size == 0:
                raise RuntimeError(f"Wrote {out_path} but file is missing or empty")

            log.info("Capture written: %s (%d bytes, %d assets%s)",
                     out_path.relative_to(self.state.vault),
                     out_path.stat().st_size,
                     len(succeeded),
                     f", {len(failed)} failed → pending" if failed else "")

        except Exception:
            # Failure cleanup: if `capture.md` didn't land, we don't want
            # to leave a half-built folder behind. It pollutes the date
            # dir and causes the next retry to either re-use a stale state
            # or pick a suffixed name (sprawl). Two cases:
            #   - folder_was_new: we created it; safe to remove the whole
            #     subtree (it's all ours).
            #   - else: folder pre-existed (rare, from a prior aborted
            #     attempt that wasn't cleaned). Remove only the artifacts
            #     we just added (downloaded assets + _pending-images.json),
            #     leave anything we didn't touch untouched.
            try:
                if (capture_dir_path / "capture.md").exists():
                    pass  # capture.md landed; failure is post-write; keep folder.
                elif folder_was_new:
                    import shutil as _shutil
                    _shutil.rmtree(capture_dir_path, ignore_errors=True)
                    log.info("Cleaned up half-built folder %s",
                             capture_dir_path.name)
                else:
                    # Conservative cleanup: remove our known artifacts.
                    for child in capture_dir_path.iterdir():
                        if child.name == "_pending-images.json":
                            child.unlink(missing_ok=True)
                        elif child.is_file() and child.suffix and child.stem.isdigit():
                            # NN.ext shape — our naming convention.
                            child.unlink(missing_ok=True)
                    log.info("Cleaned up our artifacts in pre-existing folder %s",
                             capture_dir_path.name)
            except Exception as cleanup_err:
                log.warning("Cleanup after failure also failed: %s", cleanup_err)
            raise

    def _generate_body_via_opencode(self, bundle: dict,
                                    assets: list[dict]) -> str:
        """Run opencode to turn the bundle into curated markdown body text.

        Returns the body string. Raises RuntimeError on any failure mode
        (non-zero rc, timeout, empty stdout).

        Asset-awareness: when assets are present, the prompt is augmented
        with a list of their relative paths + metadata, and opencode is
        instructed to embed images inline (`![alt](NN.ext)`) and link
        non-images (`[name](NN.ext)`). The Files appendix at the bottom
        is mechanically appended by Python afterwards regardless, so
        flaky embed compliance doesn't lose information.
        """
        # Augment the bundle for the prompt with the asset list so the
        # model knows what's available, what filenames to use, and what
        # mimetypes they are. We do NOT modify the input `bundle` dict
        # so frontmatter generation sees the canonical shape.
        prompt_bundle = dict(bundle)
        prompt_bundle["_local_assets"] = [
            {
                "filename": a["filename"],
                "mimetype": a["mimetype"],
                "original_name": a["original_name"],
                "is_image": (a.get("mimetype") or "").lower().startswith(ASSET_INLINE_MIME_PREFIXES),
            }
            for a in assets
        ]
        bundle_json = json.dumps(prompt_bundle, indent=2)

        if assets:
            asset_instructions = """

## Assets

Local assets have been downloaded into the same folder as the capture file.
For each entry in `_local_assets`:
- If `is_image` is true → embed inline at the point in the body where the
  message that contained it would naturally appear, using `![alt](FILENAME)`
  with a short descriptive alt-text (use `original_name` if helpful, else
  describe the message context). Use the literal `filename` field as the
  path — do NOT add a `./` prefix; do NOT add a folder.
- If `is_image` is false → link inline with `[original_name or "Attachment"](FILENAME)`.

Do not mention "see Files appendix" or any reference to a list at the
bottom — a Files appendix is appended mechanically after your body, so
inline embeds should focus on contextual presentation only.

If you cannot determine where an asset belongs in the body, embed it
once at the most logical position (e.g. right after the reacted message).
Do not omit any asset from the body — every entry in `_local_assets`
must appear at least once, inline.
"""
        else:
            asset_instructions = ""

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
{asset_instructions}
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
    def _build_appendix(succeeded: list[dict], failed: list[dict]) -> str:
        """Build a mechanical 'Files' appendix listing all assets.

        Succeeded entries appear as plain markdown links; failed entries
        appear under a sub-heading with their reason, so the human reading
        the capture immediately sees what couldn't be fetched and can run
        `harvester.py --retry-pending` to attempt re-download.
        """
        if not succeeded and not failed:
            return ""

        lines = ["## Files"]

        if succeeded:
            for a in succeeded:
                name = a.get("original_name") or a["filename"]
                # Escape pipe in name to avoid breaking nested tables (rare).
                name = name.replace("\n", " ").strip() or a["filename"]
                size_kb = max(1, a.get("size_bytes", 0) // 1024)
                mime = a.get("mimetype") or ""
                lines.append(
                    f"- [{name}]({a['filename']}) — {mime or 'unknown'}, ~{size_kb} KB"
                )

        if failed:
            lines.append("")
            lines.append("### Failed downloads (parked to `_pending-images.json`)")
            for f in failed:
                name = f.get("original_name") or f["filename"]
                name = name.replace("\n", " ").strip() or f["filename"]
                reason = (f.get("reason") or "unknown").replace("\n", " ")[:160]
                permalink = f.get("permalink") or f.get("url") or ""
                if permalink:
                    lines.append(f"- **{name}** ({f['filename']}) — {reason} · [Slack]({permalink})")
                else:
                    lines.append(f"- **{name}** ({f['filename']}) — {reason}")
            lines.append("")
            lines.append("Retry with: `python ~/repo/slack-harvester/harvester.py --retry-pending`")

        return "\n".join(lines)

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
    def _build_frontmatter(bundle: dict,
                           succeeded_assets: Optional[list[dict]] = None,
                           failed_assets: Optional[list[dict]] = None) -> str:
        """Build the YAML frontmatter block for a capture file.

        Deterministic. Mirrors the contract previously baked into the
        opencode prompt.

        Folder-layout addition (2026-06-10): emits an `assets:` field
        listing every successfully downloaded asset (filename + mimetype)
        and an optional `pending_assets:` field if any failed. Both are
        omitted entirely when there are no assets (no empty `assets: []`
        on historical-shape captures from before this change).
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
        ]

        if succeeded_assets:
            lines.append("assets:")
            for a in succeeded_assets:
                # JSON-encode the compact dict for safe YAML embedding.
                lines.append(
                    f"  - {json.dumps({'filename': a['filename'], 'mimetype': a.get('mimetype', ''), 'original_name': a.get('original_name', ''), 'size_bytes': a.get('size_bytes', 0)}, separators=(', ', ': '))}"
                )

        if failed_assets:
            lines.append("pending_assets:")
            for f in failed_assets:
                lines.append(
                    f"  - {json.dumps({'filename': f['filename'], 'mimetype': f.get('mimetype', ''), 'original_name': f.get('original_name', ''), 'reason': f.get('reason', 'unknown')[:160]}, separators=(', ', ': '))}"
                )

        lines.append("---")
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
# Pending-image retry (orphan cleanup CLI)
# ---------------------------------------------------------------------------


def run_retry_pending(state: HarvesterState, client: SlackClient,
                      dry_run: bool = False) -> dict:
    """Walk every capture folder, find `_pending-images.json`, attempt re-download.

    On success per item:
      - Move the bytes into the capture folder at the originally-recorded
        filename (NN.ext) so the body's inline embed (if any) resolves.
      - Remove the item from `_pending-images.json`.
      - When `_pending-images.json` is empty (no items left), delete it
        and update the capture's frontmatter to drop `pending_assets:` and
        add the now-succeeded assets to `assets:` if not already present.

    On failure per item:
      - Update the item's `reason` to the latest error and leave it in
        `_pending-images.json`. Caller can re-run later.

    Args:
        state: HarvesterState (provides vault path, credentials).
        client: SlackClient (provides download_file).
        dry_run: When true, log what would happen but don't modify files.

    Returns:
        Summary dict: {"folders_scanned": N, "items_found": N, "items_succeeded": N,
                       "items_failed": N, "folders_cleared": N}
    """
    summary = {
        "folders_scanned": 0,
        "items_found": 0,
        "items_succeeded": 0,
        "items_failed": 0,
        "folders_cleared": 0,
    }

    if not client.state.has_credentials():
        log.error("--retry-pending: no Slack credentials available")
        return summary

    capture_root = state.vault / state.capture_dir
    if not capture_root.exists():
        log.error("--retry-pending: capture root %s does not exist", capture_root)
        return summary

    # Find every _pending-images.json across all date dirs.
    pending_files = list(capture_root.glob("*/*/_pending-images.json"))
    if not pending_files:
        log.info("--retry-pending: no pending-image files found")
        return summary

    log.info("--retry-pending: scanning %d pending-image file(s)%s",
             len(pending_files), " (dry-run)" if dry_run else "")

    for pending_path in sorted(pending_files):
        summary["folders_scanned"] += 1
        capture_folder = pending_path.parent
        rel = capture_folder.relative_to(state.vault)

        try:
            payload = json.loads(pending_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            log.error("  %s: unreadable (%s); skipping", rel, e)
            continue

        items = payload.get("items") or []
        if not items:
            log.info("  %s: empty pending file; removing", rel)
            if not dry_run:
                try:
                    pending_path.unlink()
                    summary["folders_cleared"] += 1
                except OSError as e:
                    log.warning("    unlink failed: %s", e)
            continue

        log.info("  %s: %d pending item(s)", rel, len(items))
        still_failed: list[dict] = []
        newly_succeeded: list[dict] = []

        for item in items:
            summary["items_found"] += 1
            filename = item.get("filename") or "??.bin"
            original = item.get("original_name") or "<unnamed>"
            url = item.get("url")
            if not url:
                log.warning("    %s: no url; cannot retry", filename)
                still_failed.append(item)
                summary["items_failed"] += 1
                continue

            dest = capture_folder / filename
            if dry_run:
                log.info("    [DRY-RUN] would retry %s ← %s", filename, original)
                still_failed.append(item)  # Keep counts honest in dry-run.
                continue

            try:
                size = client.download_file(
                    url, dest,
                    timeout=ASSET_DOWNLOAD_TIMEOUT,
                    max_bytes=ASSET_MAX_BYTES,
                    expected_mimetype=item.get("mimetype") or None,
                )
                log.info("    %s: succeeded (%d bytes)", filename, size)
                newly_succeeded.append({
                    "index": item.get("index"),
                    "filename": filename,
                    "mimetype": item.get("mimetype", ""),
                    "original_name": item.get("original_name", ""),
                    "size_bytes": size,
                    "url": url,
                    "permalink": item.get("permalink", ""),
                })
                summary["items_succeeded"] += 1
            except RuntimeError as e:
                reason = str(e)
                log.warning("    %s: still failing (%s)", filename, reason)
                item = dict(item)
                item["reason"] = reason
                still_failed.append(item)
                summary["items_failed"] += 1

        if dry_run:
            continue

        if not still_failed:
            # All recovered. Drop the pending file and update frontmatter.
            try:
                pending_path.unlink()
                summary["folders_cleared"] += 1
                log.info("  %s: all pending items recovered; removed _pending-images.json", rel)
            except OSError as e:
                log.warning("  %s: unlink failed (%s)", rel, e)
            _patch_capture_frontmatter_post_retry(
                capture_folder / "capture.md",
                newly_succeeded=newly_succeeded,
                still_failed=[],
            )
        else:
            # Rewrite the pending file with the remaining failures only.
            payload["items"] = still_failed
            payload["last_retry_at"] = datetime.now(timezone.utc).isoformat()
            pending_path.write_text(json.dumps(payload, indent=2) + "\n")
            if newly_succeeded:
                log.info("  %s: %d recovered, %d still failing", rel,
                         len(newly_succeeded), len(still_failed))
                _patch_capture_frontmatter_post_retry(
                    capture_folder / "capture.md",
                    newly_succeeded=newly_succeeded,
                    still_failed=still_failed,
                )

    log.info("--retry-pending summary: scanned=%d found=%d succeeded=%d failed=%d cleared=%d",
             summary["folders_scanned"],
             summary["items_found"],
             summary["items_succeeded"],
             summary["items_failed"],
             summary["folders_cleared"])
    return summary


def _patch_capture_frontmatter_post_retry(capture_md: Path,
                                          newly_succeeded: list[dict],
                                          still_failed: list[dict]) -> None:
    """Best-effort frontmatter patch after a retry-pending cycle.

    Adds newly-recovered entries to the `assets:` list and rewrites the
    `pending_assets:` list to reflect the remaining failures. If the file
    is unreadable or malformed, logs a warning and returns without raising
    — the body file is the canonical artifact; frontmatter drift is
    recoverable manually.

    This is intentionally simple: it does a textual splice rather than a
    full YAML round-trip, to avoid pulling in a YAML dependency.
    """
    if not capture_md.exists():
        log.warning("    frontmatter patch skipped: %s missing", capture_md)
        return

    try:
        content = capture_md.read_text()
    except OSError as e:
        log.warning("    frontmatter patch skipped: read failed (%s)", e)
        return

    if not content.startswith("---\n"):
        log.warning("    frontmatter patch skipped: no frontmatter detected")
        return

    end = content.find("\n---\n", 4)
    if end == -1:
        log.warning("    frontmatter patch skipped: unterminated frontmatter")
        return

    fm = content[4:end]      # between the two --- markers, no leading newline
    body = content[end + 5:]  # everything after the closing ---\n

    # Split frontmatter into lines, isolate the assets/pending_assets blocks.
    lines = fm.split("\n")
    kept: list[str] = []
    existing_assets: list[str] = []
    in_assets = False
    in_pending = False
    for line in lines:
        if line.startswith("assets:"):
            in_assets = True
            in_pending = False
            existing_assets = []
            continue
        if line.startswith("pending_assets:"):
            in_assets = False
            in_pending = True
            continue
        if (in_assets or in_pending) and line.startswith("  - "):
            if in_assets:
                existing_assets.append(line)
            continue
        if (in_assets or in_pending) and not line.startswith("  "):
            in_assets = False
            in_pending = False
        kept.append(line)

    # Append newly-succeeded to existing_assets (avoid filename duplicates).
    existing_filenames = set()
    for ln in existing_assets:
        if '"filename":' in ln:
            try:
                obj = json.loads(ln.lstrip("  -").strip())
                existing_filenames.add(obj.get("filename"))
            except json.JSONDecodeError:
                pass
    for a in newly_succeeded:
        if a.get("filename") in existing_filenames:
            continue
        existing_assets.append(
            "  - " + json.dumps(
                {"filename": a["filename"],
                 "mimetype": a.get("mimetype", ""),
                 "original_name": a.get("original_name", ""),
                 "size_bytes": a.get("size_bytes", 0)},
                separators=(', ', ': ')
            )
        )

    # Rebuild frontmatter.
    new_fm_lines = list(kept)
    if existing_assets:
        new_fm_lines.append("assets:")
        new_fm_lines.extend(existing_assets)
    if still_failed:
        new_fm_lines.append("pending_assets:")
        for f in still_failed:
            new_fm_lines.append(
                "  - " + json.dumps(
                    {"filename": f.get("filename", ""),
                     "mimetype": f.get("mimetype", ""),
                     "original_name": f.get("original_name", ""),
                     "reason": (f.get("reason") or "unknown")[:160]},
                    separators=(', ', ': ')
                )
            )

    new_content = "---\n" + "\n".join(new_fm_lines).rstrip("\n") + "\n---\n" + body
    try:
        capture_md.write_text(new_content)
        log.info("    frontmatter patched")
    except OSError as e:
        log.warning("    frontmatter patch failed: %s", e)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Slack Harvester")
    parser.add_argument("--config", type=Path,
                        default=SCRIPT_DIR / "config.json",
                        help="Path to config.json (default: alongside this script)")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--retry-pending", action="store_true",
                        help="Walk capture folders, retry any _pending-images.json "
                             "items, then exit. Does not start the daemon.")
    parser.add_argument("--recover", action="store_true",
                        help="Run the recovery sweep (find seen.json entries "
                             "with no matching capture file on disk and un-mark "
                             "them so the next poll re-processes), then exit. "
                             "Use after confirming the filesystem is in steady "
                             "state — running this with a cloud-syncing "
                             "filesystem mid-sync can produce duplicate captures.")
    parser.add_argument("--dry-run", action="store_true",
                        help="With --retry-pending: report what would happen "
                             "but do not download or modify files. "
                             "With --recover: report orphans without un-marking.")
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

    # --retry-pending: one-shot orphan cleanup mode. Doesn't start the
    # daemon, doesn't construct the worker/poller, doesn't bind the
    # health port. Exits with rc=0 unless retry produced no failures
    # for items that should have succeeded; rc=2 if no credentials.
    if args.retry_pending:
        if not state.has_credentials():
            log.error("--retry-pending requires Slack credentials")
            sys.exit(2)
        run_retry_pending(state, client, dry_run=args.dry_run)
        sys.exit(0)

    # --recover: explicit recovery-sweep mode. Un-marks orphans found in
    # seen.json so the next poll re-processes them. Use only after
    # confirming the filesystem is in steady state (no in-flight cloud
    # sync). See ISSUES.md #11.
    if args.recover:
        candidates = state.recovery_sweep(max_age_days=30,
                                          auto_unmark=not args.dry_run)
        if args.dry_run:
            log.info("--recover --dry-run: would un-mark %d orphan(s)",
                     len(candidates))
        else:
            log.info("--recover: un-marked %d orphan(s); next poll will re-process",
                     len(candidates))
        sys.exit(0)

    worker = CaptureWorker(state, client, opencode_cmd)
    poller = ReactionPoller(state, client, worker, interval, reactions)

    HarvestHandler.state = state
    HarvestHandler.worker = worker

    # Recovery sweep (ISSUES.md #10): un-mark seen entries with no corresponding
    # capture file in the expected date dir. As of 2026-06-10 (ISSUES.md #11)
    # this is REPORT-ONLY at startup — auto-un-marking caused duplicate
    # captures during a GoogleDrive sync race. Use `python harvester.py
    # --recover` after confirming the filesystem is in steady state to
    # actually un-mark orphans.
    if state.has_credentials():
        try:
            candidates = state.recovery_sweep(max_age_days=30, auto_unmark=False)
            if not candidates:
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
