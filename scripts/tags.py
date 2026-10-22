"""Bulk tag repairs across all media rows: rename, remove, normalize.

All three go through app.media.db and rewrite every matching row in one
transaction, so run them with --dry-run first.

`normalize` canonicalizes stored tag strings. insert_media() and
update_media_tags() strip every tag part and drop the empties, because
the comma-padded `(',' || tags || ',') ILIKE '%,tag,%'` whole-tag matching
in _build_where can only see canonical parts. A row written some other
way, for example by an import, can hold a value like `"public, beach"`,
which leaves `beach` silently unfindable — no error, the tag simply never
matches. It only touches rows that are not already canonical, and it never
reorders or invents tags: each row is re-emitted as tag_str(tag_list(row)).

The database lives on the stack's internal network and is not reachable
from the host, so run this inside the web container:

    docker exec -it mediaindex-web python scripts/tags.py rename blackandwhite black_and_white --dry-run

Usage:
    python scripts/tags.py rename <old_tag> <new_tag> [--dry-run]
    python scripts/tags.py remove <tag> [--dry-run]
    python scripts/tags.py normalize [--dry-run]
"""
import argparse
import sys

import _bootstrap  # sys.path + SECRET_KEY; must precede app.*

from app.media.db import normalize_tags, remove_tag, rename_tag


def main():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--dry-run", action="store_true",
                        help="Preview changes without writing")
    p = argparse.ArgumentParser(description="Bulk tag repairs across all media rows.")
    sub = p.add_subparsers(dest="command", required=True)
    rename = sub.add_parser("rename", parents=[common],
                            help="Rename a tag across all media rows")
    rename.add_argument("old", help="Tag to rename")
    rename.add_argument("new", help="New tag name")
    remove = sub.add_parser("remove", parents=[common],
                            help="Remove a tag from all media rows")
    remove.add_argument("tag", help="Tag to remove")
    sub.add_parser("normalize", parents=[common],
                   help="Canonicalize stored tag strings")
    args = p.parse_args()

    _bootstrap.require_db()
    if args.command == "rename":
        if args.old == args.new:
            sys.exit("old and new tag are identical; nothing to do.")
        n, changes = rename_tag(args.old, args.new, args.dry_run)
    elif args.command == "remove":
        n, changes = remove_tag(args.tag, args.dry_run)
    else:
        n, changes = normalize_tags(args.dry_run)

    # normalize quotes both sides: the whitespace it drops is the change.
    show = repr if args.command == "normalize" else str
    for mid, old_tags, new_tags in changes:
        print(f"#{mid}: {show(old_tags)}  ->  {show(new_tags) if new_tags else '(empty)'}")
    verb = "would update" if args.dry_run else "updated"
    print(f"\n{verb} {n} row(s).")


if __name__ == "__main__":
    main()
