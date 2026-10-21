"""Bulk-remove a tag from all media rows that have it.

Goes through app.media.db. The host cannot reach the database (it lives
on the stack's internal network), so run the script inside the web
container:

    docker exec -it mediaindex-web python scripts/remove_tag.py tag

Usage:
    python scripts/remove_tag.py <tag> [--dry-run]

Example:
    python scripts/remove_tag.py blurry
"""
import argparse

import _bootstrap  # noqa: F401  (sys.path + SECRET_KEY; must precede app.*)

from app.media.db import remove_tag


def main():
    p = argparse.ArgumentParser(description="Bulk remove a tag from all media rows.")
    p.add_argument("tag", help="Tag to remove")
    p.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    args = p.parse_args()

    n, changes = remove_tag(args.tag, args.dry_run)
    for mid, old_tags, new_tags in changes:
        print(f"#{mid}: {old_tags}  ->  {new_tags or '(empty)'}")
    verb = "would update" if args.dry_run else "updated"
    print(f"\n{verb} {n} row(s).")


if __name__ == "__main__":
    main()
