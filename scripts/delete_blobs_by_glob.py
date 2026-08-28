"""
Delete GCS blobs matching a glob.

SAFETY: dry-run by default — it only lists what WOULD be deleted. You must pass
--yes to actually delete. Nothing is deleted without --yes.

Usage (from the repo root):
    # preview only (dry-run)
    uv run python scripts/delete_blobs_by_glob.py "captures/*/*/mentor-audio/**"

    # actually delete
    uv run python scripts/delete_blobs_by_glob.py "captures/aex-cqbn-hxi/**/*.webm" --yes

    # different bucket / show more names
    uv run python scripts/delete_blobs_by_glob.py "backlogs/**" --yes --bucket other-bucket --show 200

Glob semantics follow GCS matchGlob: `*` matches within a path segment,
`**` matches across `/`, `?` a single char, `[...]`/`{...}` groups.
"""

from __future__ import annotations

import argparse
import os
import sys

# Make `src` importable regardless of the current working directory.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from google.cloud import storage  # noqa: E402

from src.config import PROJECT_ID, BUCKET_NAME  # noqa: E402


WILDCARD_CHARS = set("*?[{")


def derive_prefix(match_glob: str) -> str:
    """Everything up to the last '/' before the first wildcard — used as an
    efficient list prefix so we don't scan the whole bucket."""
    wildcard_index = next(
        (i for i, ch in enumerate(match_glob) if ch in WILDCARD_CHARS),
        len(match_glob),
    )
    head = match_glob[:wildcard_index]
    slash_index = head.rfind("/")
    return head[: slash_index + 1] if slash_index >= 0 else ""


def human_size(num_bytes: float) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PB"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="delete_blobs_by_glob",
        description="Delete GCS blobs matching a glob (dry-run by default).",
    )
    parser.add_argument(
        "match_glob",
        help="GCS matchGlob pattern, e.g. 'captures/<a>/<b>/mentor-audio/**'.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Actually delete. Without this flag the script only previews.",
    )
    parser.add_argument(
        "--bucket",
        default=BUCKET_NAME,
        help=f"Bucket name (default: {BUCKET_NAME}).",
    )
    parser.add_argument(
        "--show",
        type=int,
        default=50,
        help="Max number of matching blob names to print (default: 50).",
    )
    args = parser.parse_args(argv[1:])

    client = storage.Client(project=PROJECT_ID)
    bucket = client.bucket(args.bucket)
    prefix = derive_prefix(args.match_glob)

    print(f"Bucket : gs://{args.bucket}")
    print(f"Glob   : {args.match_glob}")
    print(f"Prefix : {prefix or '(whole bucket)'}")
    print(f"Mode   : {'DELETE' if args.yes else 'DRY-RUN (no deletion)'}")
    print()

    blobs = list(
        bucket.list_blobs(
            prefix=prefix,
            match_glob=args.match_glob,
        )
    )

    total = len(blobs)
    total_bytes = sum(blob.size or 0 for blob in blobs)

    if total == 0:
        print("No blobs matched. Nothing to do.")
        return 0

    for index, blob in enumerate(blobs):
        if index < args.show:
            print(f"  {blob.name} ({human_size(blob.size or 0)})")
        elif index == args.show:
            print(f"  ... and {total - args.show} more")
            break

    print()
    print(f"Matched {total} blob(s), {human_size(total_bytes)} total.")

    if not args.yes:
        print()
        print("DRY-RUN: nothing deleted. Re-run with --yes to delete.")
        return 0

    print()
    print("Deleting...")
    deleted = 0
    failed = 0
    for index, blob in enumerate(blobs, start=1):
        try:
            blob.delete()
            deleted += 1
            if deleted % 50 == 0 or index == total:
                print(f"  deleted {deleted}/{total}")
        except Exception as error:
            failed += 1
            print(f"  FAILED {blob.name}: {error}")

    print()
    print("==============================")
    print(f"Deleted : {deleted}")
    print(f"Failed  : {failed}")
    print(f"Freed   : ~{human_size(total_bytes)}")
    print("==============================")

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
