#!/usr/bin/env python3
"""migrate.py — One-shot migration: flat capture layout → folder-per-capture.

Before:
    ~/vault/51-slack-captures/YYYY-MM-DD/{slug}.md
    ~/vault/51-slack-captures/YYYY-MM-DD/{slug}.png            (manual sibling)
    ~/vault/51-slack-captures/YYYY-MM-DD/{slug}-extra.png      (manual sibling)
    ~/vault/51-slack-captures/YYYY-MM-DD/Screenshot ....png    (body-referenced)
    ~/vault/51-slack-captures/_state/...                       (orphan)
    ~/vault/51-slack-captures/_pending/...                     (orphan)

After:
    ~/vault/51-slack-captures/YYYY-MM-DD/{slug}/capture.md
    ~/vault/51-slack-captures/YYYY-MM-DD/{slug}/01.png         (renamed from absorbed sibling)
    ~/vault/51-slack-captures/YYYY-MM-DD/{slug}/02.png         (renamed from second sibling)
    ~/vault/90-archive/2026-06-slack-harvester-pre-pivot/_state/...
    ~/vault/90-archive/2026-06-slack-harvester-pre-pivot/_pending/...

Asset absorption strategy (per plan):
1. Glob `{slug}*.{png,jpg,jpeg,gif,pdf,mp4,webp,svg,heic,heif,mov,webm,mp3,m4a,wav}`
   in the same date dir. Renumber to 01.ext, 02.ext, … in glob-sort order.
2. Scan capture body for backtick-quoted filenames matching local files
   in the date dir (handles the `📎 Attachment: \`Screenshot ....png\`` case
   where the asset name doesn't share the slug prefix). Absorb those too.

Bare-filename body references (e.g. `![](01.png)` or `Screenshot ....png`)
are NOT rewritten inside the body — under folder layout both files end up
in the same folder, so the bare reference still resolves to the local file.

Run order:
    python migrate.py            # dry-run (default): report what would happen
    python migrate.py --apply    # actually do it

Dry-run reports every change and exits with rc=0 regardless. --apply
exits rc=0 on success, rc=1 if any operation failed (report shows which).
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


# Asset extensions absorbed via the slug-prefix glob heuristic. Lowercase
# match; the actual file system check is case-insensitive (suffix.lower()).
ASSET_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".heic", ".heif",
    ".pdf", ".zip",
    ".mp4", ".mov", ".webm",
    ".mp3", ".m4a", ".wav",
    ".txt", ".csv", ".json",
}

# Backtick-quoted filename pattern for body-reference scan.
# Matches: `Screenshot 2026-06-04 at 3.32.47 PM.png`, `foo.pdf`, etc.
# Excludes paths (no `/`) and excludes filenames that are themselves
# captures (.md), to avoid pulling in wikilink-like references.
_BACKTICK_FILE_RE = re.compile(r"`([^`/\n]+\.(?:" + "|".join(
    sorted(e.lstrip(".") for e in ASSET_EXTS)
) + r"))`", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class CaptureMigration:
    """Plan for migrating a single capture from flat to folder layout."""
    src_md: Path              # e.g. .../2026-06-04/2026-06-04-john-was-intended-your.md
    date_dir: Path            # e.g. .../2026-06-04/
    slug: str                 # e.g. 2026-06-04-john-was-intended-your (no extension)
    target_folder: Path       # e.g. .../2026-06-04/2026-06-04-john-was-intended-your/
    target_md: Path           # e.g. .../{folder}/capture.md
    absorbed_assets: list[tuple[Path, Path]] = field(default_factory=list)
    # Each tuple: (src_asset_path, dest_renamed_path)
    notes: list[str] = field(default_factory=list)
    # Anomalies worth surfacing to the human reviewer.


@dataclass
class OrphanMove:
    """Plan for archiving a non-capture top-level dir (e.g. _state, _pending)."""
    src: Path
    dest: Path
    is_empty: bool


@dataclass
class MigrationPlan:
    captures: list[CaptureMigration] = field(default_factory=list)
    orphans: list[OrphanMove] = field(default_factory=list)
    skipped_files: list[tuple[Path, str]] = field(default_factory=list)
    # (path, reason) — anything weird we noticed but didn't act on.


# ---------------------------------------------------------------------------
# Plan building
# ---------------------------------------------------------------------------


def build_plan(capture_root: Path, archive_root: Path) -> MigrationPlan:
    plan = MigrationPlan()

    # Scan top-level entries in capture_root.
    for entry in sorted(capture_root.iterdir()):
        name = entry.name

        # Date dir: matches YYYY-MM-DD
        if entry.is_dir() and re.fullmatch(r"\d{4}-\d{2}-\d{2}", name):
            _scan_date_dir(entry, plan)
            continue

        # Orphan dirs to archive.
        if entry.is_dir() and name in ("_state", "_pending"):
            plan.orphans.append(OrphanMove(
                src=entry,
                dest=archive_root / name,
                is_empty=not any(entry.iterdir()),
            ))
            continue

        # Ignore .DS_Store, .git, etc.
        if name.startswith(".") or name == "_templates":
            plan.skipped_files.append((entry, "ignored top-level entry"))
            continue

        plan.skipped_files.append((entry, "unexpected top-level entry"))

    return plan


def _scan_date_dir(date_dir: Path, plan: MigrationPlan) -> None:
    """Inventory one YYYY-MM-DD dir, produce CaptureMigration entries."""
    # Pass 1: find every flat-shape capture (depth-1 .md file).
    flat_captures: list[Path] = sorted(
        p for p in date_dir.iterdir()
        if p.is_file() and p.suffix == ".md"
    )

    # Pass 2: inventory non-md files in date_dir (potential assets).
    asset_files: list[Path] = sorted(
        p for p in date_dir.iterdir()
        if p.is_file() and p.suffix.lower() in ASSET_EXTS
    )
    # Track which assets get absorbed (so we can report leftovers).
    absorbed: set[Path] = set()

    # Pass 3: folder-shape captures (already-migrated). Skip but note.
    for sub in sorted(date_dir.iterdir()):
        if sub.is_dir():
            if (sub / "capture.md").exists():
                plan.skipped_files.append(
                    (sub, "already in folder layout (capture.md present)")
                )
            else:
                # A subdir without capture.md — could be an aborted half-built
                # capture, or something unrelated. Flag for human eyes.
                plan.skipped_files.append(
                    (sub, "subdir without capture.md — leaving in place")
                )

    # Pass 4: build per-capture plans.
    for md in flat_captures:
        slug = md.stem  # filename without .md

        target_folder = date_dir / slug
        target_md = target_folder / "capture.md"

        cap = CaptureMigration(
            src_md=md,
            date_dir=date_dir,
            slug=slug,
            target_folder=target_folder,
            target_md=target_md,
        )

        # Folder name collision: a directory named {slug} already exists
        # at the date dir. Possibilities:
        #   a) It's a half-built folder from a prior aborted attempt.
        #   b) It's some unrelated subdir.
        # Either way, don't auto-merge — surface to human.
        if target_folder.exists():
            cap.notes.append(
                f"target folder already exists: {target_folder.name} "
                "(skipping; resolve manually)"
            )
            plan.captures.append(cap)
            continue

        # Asset heuristic A: slug-prefix glob.
        prefix_matches = _glob_slug_prefix_assets(date_dir, slug, asset_files)

        # Asset heuristic B: backtick-quoted filenames in body.
        body_matches = _scan_body_for_file_refs(md, asset_files)

        # Union, preserve order: prefix matches first (file-system order),
        # then any body-refs not already in prefix matches.
        absorb_order: list[Path] = list(prefix_matches)
        body_only: list[Path] = []
        for p in body_matches:
            if p not in absorb_order:
                absorb_order.append(p)
                body_only.append(p)

        # Assign 01.ext, 02.ext, …
        for i, src in enumerate(absorb_order, start=1):
            ext = src.suffix.lower()
            dest = target_folder / f"{i:02d}{ext}"
            cap.absorbed_assets.append((src, dest))
            absorbed.add(src)

        # Flag body-only matches: the body refers to a filename that
        # will be renamed to NN.ext after migration. The reference will
        # become a stale name (still readable, but won't resolve as a
        # link). Per plan, we don't rewrite historical bodies — surface
        # to the human instead.
        if body_only:
            names = ", ".join(p.name for p in body_only)
            cap.notes.append(
                f"body references will become stale (renamed to NN.ext): {names}"
            )

        plan.captures.append(cap)

    # Pass 5: report any asset files that didn't get absorbed by any capture.
    for asset in asset_files:
        if asset not in absorbed:
            plan.skipped_files.append(
                (asset, "asset file not matched to any capture (manual review)")
            )


def _glob_slug_prefix_assets(date_dir: Path, slug: str,
                              asset_files: list[Path]) -> list[Path]:
    """Find {slug}*.ext assets via filename prefix match.

    Returns assets sorted by filename for deterministic numbering.
    """
    matches = [p for p in asset_files if p.stem.startswith(slug)]
    return sorted(matches, key=lambda p: p.name)


def _normalize_filename(name: str) -> str:
    """Normalize a filename for cross-source matching.

    macOS screenshots use U+202F (narrow no-break space) between time and AM/PM,
    e.g. `Screenshot 2026-06-04 at 3.32.47\u202fPM.png`. When the same filename
    appears in markdown body text it's typically typed/pasted with a regular
    space. Normalize whitespace so matches succeed.

    Other common Unicode space variants are also collapsed:
    - U+00A0 NO-BREAK SPACE
    - U+2007 FIGURE SPACE
    - U+202F NARROW NO-BREAK SPACE
    """
    for ch in ("\u00a0", "\u2007", "\u202f"):
        name = name.replace(ch, " ")
    return name


def _scan_body_for_file_refs(md_path: Path,
                              asset_files: list[Path]) -> list[Path]:
    """Scan capture body for backtick-quoted filenames matching local assets.

    Returns assets in order of first appearance in the body. Filters to
    files that actually exist in the same date dir (asset_files). Uses
    normalized filename matching (see _normalize_filename) so macOS-style
    narrow-no-break-space filenames match body references that use a
    regular space.
    """
    try:
        body = md_path.read_text()
    except OSError:
        return []

    # Build normalized-name → Path lookup.
    name_to_path = {_normalize_filename(p.name): p for p in asset_files}
    seen: set[str] = set()
    ordered: list[Path] = []

    for m in _BACKTICK_FILE_RE.finditer(body):
        name = _normalize_filename(m.group(1))
        if name in seen:
            continue
        seen.add(name)
        if name in name_to_path:
            ordered.append(name_to_path[name])

    return ordered


# ---------------------------------------------------------------------------
# Plan reporting
# ---------------------------------------------------------------------------


def report_plan(plan: MigrationPlan, apply: bool) -> None:
    print("=" * 78)
    print(f"Slack-harvester capture migration — {'APPLY' if apply else 'DRY-RUN'}")
    print(f"Generated at: {datetime.now(timezone.utc).isoformat()}")
    print("=" * 78)
    print()

    # Captures summary.
    total = len(plan.captures)
    will_migrate = sum(1 for c in plan.captures if not _captures_skipped(c))
    will_absorb = sum(len(c.absorbed_assets) for c in plan.captures)
    print(f"Captures: {total} discovered, {will_migrate} ready to migrate, "
          f"{total - will_migrate} need manual review")
    print(f"Assets to absorb: {will_absorb}")
    print()

    # Per-date-dir grouping for readability.
    by_date: dict[str, list[CaptureMigration]] = {}
    for cap in plan.captures:
        by_date.setdefault(cap.date_dir.name, []).append(cap)

    for date in sorted(by_date.keys()):
        print(f"--- {date} ---")
        for cap in by_date[date]:
            if _captures_skipped(cap):
                print(f"  ⚠ SKIP {cap.src_md.name}")
                for note in cap.notes:
                    print(f"      • {note}")
                continue

            print(f"  {cap.src_md.name}")
            print(f"    → {cap.target_md.relative_to(cap.date_dir.parent)}")
            for src, dest in cap.absorbed_assets:
                print(f"    + {src.name} → {dest.name}")
            for note in cap.notes:
                print(f"      • {note}")
        print()

    # Orphans.
    if plan.orphans:
        print("--- Orphan dirs ---")
        for o in plan.orphans:
            empty_tag = " (empty)" if o.is_empty else ""
            print(f"  {o.src.name}{empty_tag}")
            print(f"    → {o.dest}")
        print()

    # Skipped files: split into "needs review" (anomalies) vs "ignored"
    # (DS_Store etc.) so the human only scans what's actionable.
    needs_review = [(p, r) for p, r in plan.skipped_files
                    if not r.startswith("ignored")]
    ignored = [(p, r) for p, r in plan.skipped_files
               if r.startswith("ignored")]

    if needs_review:
        print("--- Needs review ---")
        for path, reason in needs_review:
            print(f"  {path}: {reason}")
        print()

    if ignored:
        print(f"--- Ignored ({len(ignored)} entry(ies)) ---")
        for path, reason in ignored:
            print(f"  {path.name}: {reason}")
        print()

    print("=" * 78)
    print(f"End of {'APPLY' if apply else 'DRY-RUN'} report")
    print("=" * 78)


def _captures_skipped(cap: CaptureMigration) -> bool:
    """True if this capture won't be migrated (e.g. folder collision)."""
    return any(n.startswith("target folder already exists") for n in cap.notes)


# ---------------------------------------------------------------------------
# Plan execution
# ---------------------------------------------------------------------------


def apply_plan(plan: MigrationPlan) -> int:
    """Execute the plan. Returns count of failures (0 = success)."""
    failures = 0

    # 1) Captures first.
    for cap in plan.captures:
        if _captures_skipped(cap):
            continue
        try:
            _apply_capture(cap)
            print(f"✓ migrated {cap.src_md.name}")
        except Exception as e:
            failures += 1
            print(f"✗ FAILED {cap.src_md.name}: {e}")

    # 2) Orphan archives.
    for o in plan.orphans:
        try:
            _apply_orphan(o)
            print(f"✓ archived {o.src.name} → {o.dest}")
        except Exception as e:
            failures += 1
            print(f"✗ FAILED to archive {o.src.name}: {e}")

    return failures


def _apply_capture(cap: CaptureMigration) -> None:
    """Atomically migrate one capture from flat to folder layout.

    Order matters:
    1. mkdir target_folder
    2. Move .md to target_folder/capture.md
    3. Move + rename each absorbed asset

    On any failure mid-way, do NOT roll back (manual review preferred over
    silent partial state). The fail-fast print above gives the human a
    clear marker to investigate.
    """
    if cap.target_folder.exists():
        raise RuntimeError(
            f"target folder exists: {cap.target_folder} (should have been "
            "filtered by _captures_skipped)"
        )

    cap.target_folder.mkdir(parents=True)

    # Move the .md file.
    shutil.move(str(cap.src_md), str(cap.target_md))

    # Move each absorbed asset.
    for src, dest in cap.absorbed_assets:
        if not src.exists():
            raise RuntimeError(f"asset disappeared mid-migration: {src}")
        shutil.move(str(src), str(dest))


def _apply_orphan(o: OrphanMove) -> None:
    """Archive an orphan dir to the pre-pivot archive."""
    o.dest.parent.mkdir(parents=True, exist_ok=True)
    if o.dest.exists():
        raise RuntimeError(f"archive target already exists: {o.dest}")
    shutil.move(str(o.src), str(o.dest))


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrate Slack captures from flat to folder-per-capture layout.",
    )
    parser.add_argument(
        "--capture-root", type=Path,
        default=Path.home() / "vault" / "51-slack-captures",
        help="Capture root dir (default: ~/vault/51-slack-captures).",
    )
    parser.add_argument(
        "--archive-root", type=Path,
        default=Path.home() / "vault" / "90-archive" / "2026-06-slack-harvester-pre-pivot",
        help="Archive destination for orphan dirs (default: "
             "~/vault/90-archive/2026-06-slack-harvester-pre-pivot).",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually perform the migration. Without this flag, runs in "
             "dry-run mode and prints a report only.",
    )
    parser.add_argument(
        "--report-json", type=Path,
        help="Optionally write the plan to a JSON file for auditing.",
    )
    args = parser.parse_args()

    capture_root: Path = args.capture_root.expanduser().resolve()
    archive_root: Path = args.archive_root.expanduser().resolve()

    if not capture_root.exists():
        print(f"ERROR: capture root does not exist: {capture_root}", file=sys.stderr)
        return 2

    plan = build_plan(capture_root, archive_root)
    report_plan(plan, apply=args.apply)

    if args.report_json:
        _write_report_json(plan, args.report_json)
        print(f"Plan JSON written to {args.report_json}")

    if not args.apply:
        print()
        print("DRY-RUN complete. Re-run with --apply to commit the migration.")
        return 0

    print()
    print("Applying migration…")
    failures = apply_plan(plan)
    print()
    if failures:
        print(f"FAILED: {failures} operation(s) did not complete. Review above.")
        return 1
    print(f"SUCCESS: migrated {sum(1 for c in plan.captures if not _captures_skipped(c))} "
          f"capture(s) and archived {len(plan.orphans)} orphan dir(s).")
    return 0


def _write_report_json(plan: MigrationPlan, dest: Path) -> None:
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "captures": [
            {
                "src_md": str(c.src_md),
                "target_md": str(c.target_md),
                "slug": c.slug,
                "absorbed_assets": [
                    {"src": str(s), "dest": str(d)}
                    for s, d in c.absorbed_assets
                ],
                "notes": c.notes,
                "skipped": _captures_skipped(c),
            }
            for c in plan.captures
        ],
        "orphans": [
            {"src": str(o.src), "dest": str(o.dest), "is_empty": o.is_empty}
            for o in plan.orphans
        ],
        "skipped_files": [
            {"path": str(p), "reason": r} for p, r in plan.skipped_files
        ],
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(payload, indent=2) + "\n")


if __name__ == "__main__":
    sys.exit(main())
