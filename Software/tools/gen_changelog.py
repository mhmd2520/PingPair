"""Generate user-facing release notes from git history.

Turns the commits since the last release tag into grouped Markdown — the body
for a GitHub Release (which the Feature-6 in-app updater later fetches). The
project doesn't use Conventional Commits, so grouping is heuristic: each commit
subject is bucketed by the words it starts with (Feature / Round / fix / docs /
rename …), with everything else under "Other". Pure stdlib — no deps.

Usage (from the repo, venv optional since there are no imports beyond stdlib)::

    python Software/tools/gen_changelog.py                       # since last tag -> stdout
    python Software/tools/gen_changelog.py --version v0.5.0       # stamp a version heading
    python Software/tools/gen_changelog.py --since v0.4.7-pre-rebrand
    python Software/tools/gen_changelog.py --output NOTES.md      # write to a file

The date is taken from ``--date`` or the most recent commit (never the wall
clock — keeps output reproducible).
"""

from __future__ import annotations

import argparse
import contextlib
import subprocess
import sys
from pathlib import Path

# (heading, ordered list of lowercase prefixes that route a subject here).
_BUCKETS: list[tuple[str, tuple[str, ...]]] = [
    ("✨ Features", ("feature", "feat", "add ")),
    ("🐛 Fixes", ("fix", "correct", "round", "bug", "hotfix")),
    ("📝 Docs & chores", ("doc", "readme", "refresh", "rename", "chore", "clean")),
]
_OTHER = "🔧 Other"


def _git(*args: str) -> str:
    """Run a git command from the repo root and return stripped stdout."""
    repo_root = Path(__file__).resolve().parents[2]
    out = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",  # git emits UTF-8; don't let a cp1252 locale mojibake em-dashes
        errors="replace",
        check=True,
    )
    return out.stdout.strip()


def _last_tag() -> str | None:
    try:
        return _git("describe", "--tags", "--abbrev=0") or None
    except subprocess.CalledProcessError:
        return None  # no tags yet -> whole history


def _commits(since: str | None) -> list[tuple[str, str]]:
    """Return (short-sha, subject) pairs in newest-first order."""
    rev_range = f"{since}..HEAD" if since else "HEAD"
    raw = _git("log", rev_range, "--no-merges", "--pretty=format:%h%x09%s")
    rows: list[tuple[str, str]] = []
    for line in raw.splitlines():
        if "\t" in line:
            sha, subject = line.split("\t", 1)
            rows.append((sha, subject.strip()))
    return rows


def _bucket_for(subject: str) -> str:
    low = subject.lower()
    for heading, prefixes in _BUCKETS:
        if any(low.startswith(p) for p in prefixes):
            return heading
    return _OTHER


def _remote_compare_url(since: str | None) -> str | None:
    """Best-effort GitHub compare link, derived from the origin remote."""
    try:
        url = _git("remote", "get-url", "origin")
    except subprocess.CalledProcessError:
        return None
    if not url or not since:
        return None
    # git@github.com:owner/repo.git  /  https://github.com/owner/repo.git
    slug = url.removesuffix(".git")
    if slug.startswith("git@github.com:"):
        slug = "https://github.com/" + slug.split(":", 1)[1]
    if "github.com/" not in slug:
        return None
    return f"{slug}/compare/{since}...HEAD"


def build_notes(*, version: str | None, since: str | None, date: str) -> str:
    rows = _commits(since)
    heading_order = [h for h, _ in _BUCKETS] + [_OTHER]
    grouped: dict[str, list[tuple[str, str]]] = {h: [] for h in heading_order}
    for sha, subject in rows:
        grouped[_bucket_for(subject)].append((sha, subject))

    title = version or "Unreleased"
    lines = [f"# {title} — {date}", ""]
    if since:
        lines.append(f"_Changes since **{since}** ({len(rows)} commits)._")
    else:
        lines.append(f"_Full history ({len(rows)} commits)._")
    lines.append("")

    for heading in heading_order:
        entries = grouped[heading]
        if not entries:
            continue
        lines.append(f"## {heading}")
        for sha, subject in entries:
            lines.append(f"- {subject} (`{sha}`)")
        lines.append("")

    compare = _remote_compare_url(since)
    if compare:
        lines.append(f"**Full diff:** {compare}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--version", help="Version heading, e.g. v0.5.0 (default: 'Unreleased').")
    parser.add_argument("--since", help="Start ref (default: most recent tag, else whole history).")
    parser.add_argument("--date", help="Date string for the heading (default: latest commit date).")
    parser.add_argument("--output", type=Path, help="Write to this file instead of stdout.")
    args = parser.parse_args(argv)

    # The emoji headings can't encode on a cp1252 Windows console; force UTF-8.
    with contextlib.suppress(AttributeError, ValueError, OSError):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    since = args.since if args.since is not None else _last_tag()
    date = args.date or _git("log", "-1", "--format=%ad", "--date=short")
    notes = build_notes(version=args.version, since=since, date=date)

    if args.output:
        args.output.write_text(notes, encoding="utf-8")
        print(f"Wrote {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(notes)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
