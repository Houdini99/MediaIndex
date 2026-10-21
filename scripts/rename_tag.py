"""Bulk-rename a tag across all media rows.

Goes through app.media.db. The host cannot reach the database (it lives
on the stack's internal network), so run the script inside the web
container:

    docker exec -it mediaindex-web python scripts/rename_tag.py old new

Usage:
    python scripts/rename_tag.py <old_tag> <new_tag> [--dry-run]

Example:
    python scripts/rename_tag.py blackandwhite black_and_white
"""
import argparse
import sys

import _bootstrap  # noqa: F401  (sys.path + SECRET_KEY; must precede app.*)

from app.media.db import rename_tag


def main():
    p = argparse.ArgumentParser(description="Bulk rename a tag across all media rows.")
    p.add_argument("old", help="Tag to rename")
    p.add_argument("new", help="New tag name")
    p.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    args = p.parse_args()

    if args.old == args.new:
        print("old and new tag are identical; nothing to do.", file=sys.stderr)
        sys.exit(1)

    n, changes = rename_tag(args.old, args.new, args.dry_run)
    for mid, old_tags, new_tags in changes:
        print(f"#{mid}: {old_tags}  ->  {new_tags}")
    verb = "would update" if args.dry_run else "updated"
    print(f"\n{verb} {n} row(s).")


if __name__ == "__main__":
    main()
