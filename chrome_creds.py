"""
Read Slack credentials directly from a Chrome profile on disk.
No Chrome process or extension needed.

Token: extracted from localStorage LevelDB files (unencrypted)
Cookie: decrypted from Chrome's Cookies SQLite DB using macOS Keychain

RETIRED FROM THE LIVE PATH (ISSUES #17, 2026-07-21). The harvester no longer
imports this module at runtime — credentials now arrive by push (POST /creds)
from a companion Chrome extension that reads the LIVE Slack session
(localStorage xoxc + chrome.cookies d), keeps it warm, and alerts on logout.
This disk-scrape approach touched EDR-watchlisted browser-profile paths
(MITRE T1539) and had a LevelDB-lock / expired-token fragility class that the
push model eliminates. This file is kept as reference and a possible manual
fallback ONLY; nothing in harvester.py imports it. Do not re-wire it into the
runtime without revisiting that decision.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import subprocess
from pathlib import Path
from typing import Optional, Tuple

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def get_chrome_key() -> bytes:
    """Get Chrome's encryption key from macOS Keychain."""
    result = subprocess.run(
        ["security", "find-generic-password", "-s", "Chrome Safe Storage", "-w"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Failed to get Chrome Keychain key: {result.stderr}")

    password = result.stdout.strip()

    # Chrome derives the actual AES key via PBKDF2
    key = hashlib.pbkdf2_hmac(
        "sha1",
        password.encode("utf-8"),
        b"saltysalt",
        1003,  # Chrome's iteration count on macOS
        dklen=16,
    )
    return key


def decrypt_cookie_value(encrypted: bytes, key: bytes) -> str:
    """Decrypt a Chrome cookie value."""
    # Chrome encrypted values start with b"v10" (3 bytes version prefix)
    if encrypted[:3] != b"v10":
        # Not encrypted, return as-is
        return encrypted.decode("utf-8", errors="replace")

    # Format: v10 (3 bytes) + ciphertext
    # Try AES-128-CBC. The IV format varies across Chrome versions,
    # so we decrypt and extract the xoxd- cookie value from the output.
    ciphertext = encrypted[3:]

    # Pad ciphertext to block boundary if needed
    if len(ciphertext) % 16 != 0:
        ciphertext += b"\x00" * (16 - len(ciphertext) % 16)

    cipher = Cipher(algorithms.AES(key), modes.CBC(b" " * 16))
    decryptor = cipher.decryptor()
    decrypted = decryptor.update(ciphertext) + decryptor.finalize()

    # The first block(s) may be garbled due to IV mismatch.
    # Extract the actual cookie value starting at xoxd-
    idx = decrypted.find(b"xoxd-")
    if idx >= 0:
        # Strip padding and trailing garbage
        tail = decrypted[idx:]
        # Remove PKCS7 padding
        pad_len = tail[-1]
        if 1 <= pad_len <= 16 and all(b == pad_len for b in tail[-pad_len:]):
            tail = tail[:-pad_len]
        return tail.decode("utf-8", errors="replace")

    # Fallback: try to decode everything after removing padding
    pad_len = decrypted[-1]
    if 1 <= pad_len <= 16:
        decrypted = decrypted[:-pad_len]
    return decrypted.decode("utf-8", errors="replace")


def read_slack_cookie(profile_dir: Path) -> str | None:
    """Read the Slack 'd' cookie from Chrome's Cookies DB."""
    cookies_db = profile_dir / "Default" / "Cookies"
    if not cookies_db.exists():
        return None

    key = get_chrome_key()

    # Copy the DB to avoid locking issues if Chrome is running
    tmp_db = profile_dir / "Default" / "Cookies.tmp"
    try:
        import shutil
        shutil.copy2(cookies_db, tmp_db)

        conn = sqlite3.connect(str(tmp_db))
        cur = conn.execute(
            "SELECT encrypted_value FROM cookies "
            "WHERE name = 'd' AND host_key LIKE '%slack.com' "
            "ORDER BY last_access_utc DESC LIMIT 1"
        )
        row = cur.fetchone()
        conn.close()

        if not row:
            return None

        return decrypt_cookie_value(row[0], key)
    finally:
        tmp_db.unlink(missing_ok=True)


def read_slack_token(profile_dir: Path) -> str | None:
    """Read the xoxc token from Chrome's localStorage LevelDB files.

    LevelDB files are binary, but the token is stored as a plain string
    inside the localConfig_v2 JSON value. We can extract it with a
    binary regex without needing a LevelDB library.
    """
    ls_dir = profile_dir / "Default" / "Local Storage" / "leveldb"
    if not ls_dir.exists():
        return None

    # Search all LDB files + the WAL log for the token
    token_pattern = re.compile(rb"xoxc-[a-zA-Z0-9._-]{50,}")
    tokens = set()

    for f in ls_dir.iterdir():
        if f.suffix in (".ldb", ".log"):
            try:
                data = f.read_bytes()
                for match in token_pattern.finditer(data):
                    tokens.add(match.group(0).decode("utf-8"))
            except OSError:
                continue

    if not tokens:
        return None

    # Return the longest token (in case of partial matches from old entries)
    return max(tokens, key=len)


def read_credentials(profile_dir: Path) -> tuple[str | None, str | None]:
    """Read both token and cookie from a Chrome profile."""
    token = read_slack_token(profile_dir)
    cookie = read_slack_cookie(profile_dir)
    return token, cookie
