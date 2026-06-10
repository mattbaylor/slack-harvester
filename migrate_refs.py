#!/usr/bin/env python3
"""migrate_refs.py — Rewrite vault references to migrated capture paths.

After migrate.py moves captures from flat to folder layout:
    ~/vault/51-slack-captures/YYYY-MM-DD/{slug}.md
        → ~/vault/51-slack-captures/YYYY-MM-DD/{slug}/capture.md

    ~/vault/51-slack-captures/YYYY-MM-DD/{slug}.png
        → ~/vault/51-slack-captures/YYYY-MM-DD/{slug}/01.png  (or 02.png, …)

…every reference to the old paths in vault docs becomes stale. This tool
rewrites them in place. Dry-run default; --apply to commit.

Reference path-form variations handled:
1. `~/vault/51-slack-captures/YYYY-MM-DD/{slug}.md`
2. `51-slack-captures/YYYY-MM-DD/{slug}.md`        (vault-relative)
3. `{vault}/51-slack-captures/YYYY-MM-DD/{slug}.md` (templated)

Trailing context preserved:
- "(same thread, lines 22–24)" → preserved verbatim after the rewrite
- backticks/quotes around the path → preserved
- `.png` asset refs → rewritten to `{slug}/{NN}.png` if migrate.py
  records which NN.ext the source PNG became (loaded from --plan-json,
  default reads from a sibling plan file written by migrate.py)

Files excluded from rewrites (hand-edited or intentionally historical):
- `80-slush/slack-harvester-assets/plan.md` — the plan itself
- `40-references/slack-harvester.md` — schema doc, hand-edited
- `30-repos/slack-harvester.md` — schema doc, hand-edited
- `90-archive/00-inbox/2026-05-26-slack-harvester-design.md` — historical
- This script's own README path (if any)

Date-dir-only refs (e.g. `~/vault/51-slack-captures/2026-06-04/`) and
root-dir refs (`51-slack-captures/`) are NOT rewritten — directory
semantics are preserved by the migration.

Run order:
    python migrate_refs.py            # dry-run (default)
    python migrate_refs.py --apply    # commit

Optional:
    --plan-json PATH   Asset-rename map from migrate.py (so .png refs
                       can be rewritten to {slug}/{NN}.png). If absent,
                       .png refs are flagged for manual review.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


# Files we never rewrite. Paths are vault-relative.
EXCLUDED_PATHS = {
    "80-slush/slack-harvester-assets/plan.md",
    "40-references/slack-harvester.md",
    "30-repos/slack-harvester.md",
    "90-archive/00-inbox/2026-05-26-slack-harvester-design.md",
}

# Asset extensions we know how to handle (for .ext refs other than .md).
ASSET_EXTS_NON_MD = (
    "png", "jpg", "jpeg", "gif", "webp", "svg", "heic", "heif",
    "pdf", "zip", "mp4", "mov", "webm", "mp3", "m4a", "wav",
)

# Regex: capture a path to a specific capture file.
# Path forms (left → right):
#   prefix groups (~/vault/ | {vault}/ | "" for vault-relative)
#   51-slack-captures/YYYY-MM-DD/{slug}.{ext}
#
# Group 1: full path-prefix-up-to-the-slug ("…/2026-06-04/")
# Group 2: slug (no extension)
# Group 3: extension (md or asset ext), lowercase
_REF_RE = re.compile(
    r"(?P<prefix>(?:~/vault/|\{vault\}/|(?<![\w./~]))"      # path prefix variants
    r"51-slack-captures/"
    r"(?P<date>\d{4}-\d{2}-\d{2})/)"
    r"(?P<slug>[A-Za-z0-9_\-]+?)"                            # slug, non-greedy
    r"\.(?P<ext>md|" + "|".join(ASSET_EXTS_NON_MD) + r")\b",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class RefRewrite:
    """One reference to rewrite in one file."""
    file_path: Path
    line_no: int        # 1-based
    col_start: int      # 0-based offset within line
    col_end: int        # exclusive
    old: str            # matched text
    new: str            # replacement text
    ext: str            # md | png | jpg | ...
    needs_asset_index: bool = False
    # True if this is a non-md ref and we don't know the NN.ext to map it to.


@dataclass
class FileRewrites:
    file_path: Path
    rewrites: list[RefRewrite] = field(default_factory=list)
    excluded: bool = False
    excluded_reason: str = ""


@dataclass
class Plan:
    files: list[FileRewrites] = field(default_factory=list)
    asset_map: dict[tuple[str, str], str] = field(default_factory=dict)
    # Keys: (date, original_slug_or_filename), value: NN.ext
    vault_root: Path = field(default_factory=Path)


# ---------------------------------------------------------------------------
# Plan building
# ---------------------------------------------------------------------------


def build_plan(vault_root: Path, asset_map: dict[tuple[str, str], str]) -> Plan:
    plan = Plan(asset_map=asset_map, vault_root=vault_root)

    # Walk all .md files in the vault.
    for md in sorted(vault_root.rglob("*.md")):
        # Skip the capture files themselves (don't rewrite refs inside captures).
        try:
            rel = md.relative_to(vault_root)
        except ValueError:
            continue
        rel_str = str(rel)

        if rel_str.startswith("51-slack-captures/"):
            continue  # Captures are migrated separately; don't touch their bodies.

        fr = FileRewrites(file_path=md)

        if rel_str in EXCLUDED_PATHS:
            fr.excluded = True
            fr.excluded_reason = "hand-edited / intentionally historical"
            # Still scan so the report shows what we *would* have done.
            _scan_file(md, fr, plan)
            if fr.rewrites or fr.excluded:
                plan.files.append(fr)
            continue

        _scan_file(md, fr, plan)
        if fr.rewrites:
            plan.files.append(fr)

    return plan


def _scan_file(md_path: Path, fr: FileRewrites, plan: Plan) -> None:
    try:
        text = md_path.read_text()
    except OSError:
        return

    # Identify fenced code blocks to skip (rewriting paths inside code
    # samples would corrupt documentation examples).
    fenced_ranges = _find_fenced_ranges(text)

    for line_no, line in enumerate(text.splitlines(keepends=False), start=1):
        line_offset = _line_offset(text, line_no)

        for m in _REF_RE.finditer(line):
            abs_start = line_offset + m.start()
            if any(s <= abs_start < e for s, e in fenced_ranges):
                continue  # inside fenced code block

            old = m.group(0)
            prefix = m.group("prefix")
            date = m.group("date")
            slug = m.group("slug")
            ext = m.group("ext").lower()

            if ext == "md":
                # {prefix}{slug}.md → {prefix}{slug}/capture.md
                new = f"{prefix}{slug}/capture.md"
                fr.rewrites.append(RefRewrite(
                    file_path=md_path,
                    line_no=line_no,
                    col_start=m.start(),
                    col_end=m.end(),
                    old=old,
                    new=new,
                    ext="md",
                ))
            else:
                # Asset ref: lookup the new NN.ext in the migrate.py plan.
                # Key: (date, slug + "." + ext).
                source_filename = f"{slug}.{ext}"
                new_name = plan.asset_map.get((date, source_filename))
                if new_name:
                    # The asset became {slug}/{new_name}, e.g. {slug}/01.png
                    new = f"{prefix}{slug}/{new_name}"
                    fr.rewrites.append(RefRewrite(
                        file_path=md_path,
                        line_no=line_no,
                        col_start=m.start(),
                        col_end=m.end(),
                        old=old,
                        new=new,
                        ext=ext,
                    ))
                else:
                    # Unknown asset rename. Flag for human review.
                    fr.rewrites.append(RefRewrite(
                        file_path=md_path,
                        line_no=line_no,
                        col_start=m.start(),
                        col_end=m.end(),
                        old=old,
                        new=old,   # No rewrite proposed
                        ext=ext,
                        needs_asset_index=True,
                    ))


def _find_fenced_ranges(text: str) -> list[tuple[int, int]]:
    """Return absolute (start, end) offsets of every fenced code block.

    Handles ``` and ``` lang. Ignores inline backticks. Defensive against
    unterminated fences (treats EOF as fence end).
    """
    ranges: list[tuple[int, int]] = []
    in_fence = False
    fence_start = 0
    pos = 0

    for line_no, line in enumerate(text.splitlines(keepends=True), start=1):
        stripped = line.lstrip()
        if stripped.startswith("```"):
            if not in_fence:
                in_fence = True
                fence_start = pos
            else:
                in_fence = False
                ranges.append((fence_start, pos + len(line)))
        pos += len(line)

    if in_fence:
        ranges.append((fence_start, pos))  # unterminated → EOF

    return ranges


def _line_offset(text: str, line_no: int) -> int:
    """Return the absolute offset of the start of `line_no` (1-based)."""
    offset = 0
    current = 1
    for line in text.splitlines(keepends=True):
        if current == line_no:
            return offset
        offset += len(line)
        current += 1
    return offset


# ---------------------------------------------------------------------------
# Asset map loader (from migrate.py --report-json)
# ---------------------------------------------------------------------------


def load_asset_map_from_migrate_plan(plan_json: Path) -> dict[tuple[str, str], str]:
    """Parse migrate.py's JSON report into (date, source_filename) → NN.ext.

    The JSON has, per capture:
      "src_md": ".../YYYY-MM-DD/{slug}.md",
      "absorbed_assets": [{"src": ".../YYYY-MM-DD/{source_name}", "dest": ".../YYYY-MM-DD/{slug}/NN.ext"}]
    """
    raw = json.loads(plan_json.read_text())
    asset_map: dict[tuple[str, str], str] = {}
    for cap in raw.get("captures", []):
        for asset in cap.get("absorbed_assets", []):
            src = Path(asset["src"])
            dest = Path(asset["dest"])
            date = src.parent.name  # YYYY-MM-DD
            source_filename = src.name  # {original}.png
            new_filename = dest.name  # NN.ext
            asset_map[(date, source_filename)] = new_filename
    return asset_map


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def report_plan(plan: Plan, apply: bool) -> None:
    print("=" * 78)
    print(f"Slack-capture reference rewrite — {'APPLY' if apply else 'DRY-RUN'}")
    print(f"Generated at: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 78)
    print()

    total_files = len([f for f in plan.files if not f.excluded])
    total_rewrites = sum(len(f.rewrites) for f in plan.files if not f.excluded)
    unresolved_assets = sum(
        1 for f in plan.files if not f.excluded
        for r in f.rewrites if r.needs_asset_index
    )

    print(f"Files with rewrites: {total_files}")
    print(f"Rewrites proposed:   {total_rewrites - unresolved_assets} (md → capture.md)")
    if unresolved_assets:
        print(f"Asset refs flagged:  {unresolved_assets} (no rename mapping; manual)")
    print()

    def _display(p: Path) -> str:
        try:
            return str(p.relative_to(plan.vault_root))
        except ValueError:
            return str(p)

    excluded = [f for f in plan.files if f.excluded]
    if excluded:
        print("--- Excluded files (would-be matches reported, not rewritten) ---")
        for f in excluded:
            print(f"  {_display(f.file_path)}: {f.excluded_reason}")
            if f.rewrites:
                print(f"    ({len(f.rewrites)} would-be rewrite(s) suppressed)")
        print()

    for f in plan.files:
        if f.excluded or not f.rewrites:
            continue
        print(f"--- {_display(f.file_path)} ---")
        for r in f.rewrites:
            if r.needs_asset_index:
                print(f"  L{r.line_no}: ⚠ ASSET (no mapping): {r.old}")
            else:
                print(f"  L{r.line_no}: {r.old}")
                print(f"       → {r.new}")
        print()

    print("=" * 78)
    print(f"End of {'APPLY' if apply else 'DRY-RUN'} report")
    print("=" * 78)


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


def apply_plan(plan: Plan) -> int:
    """Apply rewrites in-place. Returns count of failures."""
    failures = 0
    for f in plan.files:
        if f.excluded or not f.rewrites:
            continue
        actionable = [r for r in f.rewrites if not r.needs_asset_index]
        if not actionable:
            continue
        try:
            _apply_file(f.file_path, actionable)
            print(f"✓ rewrote {len(actionable)} ref(s) in {f.file_path}")
        except Exception as e:
            failures += 1
            print(f"✗ FAILED to rewrite {f.file_path}: {e}")
    return failures


def _apply_file(path: Path, rewrites: list[RefRewrite]) -> None:
    """Rewrite a single file atomically.

    Rewrites are line+col scoped, but to keep the in-place edit simple
    we read the entire file, apply all rewrites in reverse-order (so
    earlier offsets remain valid), and write the result.
    """
    text = path.read_text()

    # Re-index rewrites by absolute offset so we can apply in reverse.
    lines = text.splitlines(keepends=True)
    line_offsets: list[int] = []
    acc = 0
    for line in lines:
        line_offsets.append(acc)
        acc += len(line)

    abs_edits: list[tuple[int, int, str]] = []  # (start, end, replacement)
    for r in rewrites:
        if not (1 <= r.line_no <= len(line_offsets)):
            raise RuntimeError(f"line {r.line_no} out of range")
        start = line_offsets[r.line_no - 1] + r.col_start
        end = line_offsets[r.line_no - 1] + r.col_end
        # Sanity check: text at this range matches r.old.
        if text[start:end] != r.old:
            raise RuntimeError(
                f"L{r.line_no} c{r.col_start}: expected {r.old!r}, "
                f"found {text[start:end]!r} (file changed since plan?)"
            )
        abs_edits.append((start, end, r.new))

    # Apply in reverse so earlier offsets stay valid.
    abs_edits.sort(key=lambda t: t[0], reverse=True)
    new_text = text
    for start, end, replacement in abs_edits:
        new_text = new_text[:start] + replacement + new_text[end:]

    path.write_text(new_text)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Rewrite vault references to migrated capture paths.",
    )
    parser.add_argument(
        "--vault", type=Path,
        default=Path.home() / "vault",
        help="Vault root (default: ~/vault).",
    )
    parser.add_argument(
        "--plan-json", type=Path,
        help="Path to migrate.py's --report-json output, used to resolve "
             ".png → NN.png asset renames. If omitted, asset refs are "
             "flagged for manual review only.",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually perform the rewrites. Default: dry-run.",
    )
    args = parser.parse_args()

    vault_root: Path = args.vault.expanduser().resolve()
    if not vault_root.exists():
        print(f"ERROR: vault root does not exist: {vault_root}", file=sys.stderr)
        return 2

    asset_map: dict[tuple[str, str], str] = {}
    if args.plan_json:
        try:
            asset_map = load_asset_map_from_migrate_plan(args.plan_json.expanduser().resolve())
            print(f"Loaded asset map: {len(asset_map)} entries from {args.plan_json}")
            print()
        except (OSError, json.JSONDecodeError) as e:
            print(f"WARN: failed to load --plan-json ({e}); asset refs will be flagged.")
            print()

    plan = build_plan(vault_root, asset_map)
    report_plan(plan, apply=args.apply)

    if not args.apply:
        print()
        print("DRY-RUN complete. Re-run with --apply to commit rewrites.")
        return 0

    print()
    print("Applying rewrites…")
    failures = apply_plan(plan)
    print()
    if failures:
        print(f"FAILED: {failures} file(s) did not complete.")
        return 1
    actionable = sum(
        len([r for r in f.rewrites if not r.needs_asset_index])
        for f in plan.files if not f.excluded
    )
    print(f"SUCCESS: rewrote {actionable} reference(s) in "
          f"{len([f for f in plan.files if not f.excluded and f.rewrites])} file(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
