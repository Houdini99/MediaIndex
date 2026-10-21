"""Repair: canonicalize stored tag strings.

insert_media() and update_media_tags() strip every tag part and drop
the empties, because the comma-padded `(',' || tags || ',') ILIKE '%,tag,%'`
whole-tag matching in _build_where can only see canonical parts. A row
written some other way, for example by an import, can hold a value like
`"public, beach"`, which leaves `beach` silently unfindable — no error,
the tag simply never matches.

This rewrites such rows. It only touches rows that are not already
canonical, and it never reorders or invents tags: each row is re-emitted
as tag_str(tag_list(row)).

The database lives on the stack's internal network and is not reachable
from the host, so run this inside the web container:

    docker exec -it mediaindex-web python scripts/normalize_tags.py --dry-run
    docker exec -it mediaindex-web python scripts/normalize_tags.py

Usage:
    python scripts/normalize_tags.py [--dry-run]
"""
import argparse

import _bootstrap  # noqa: F401  (sys.path + SECRET_KEY; must precede app.*)

from app.media.db import normalize_tags


def main():
    p = argparse.ArgumentParser(
        description="Canonicalize stored tag strings across all media rows.")
    p.add_argument("--dry-run", action="store_true",
                   help="Preview changes without writing")
    args = p.parse_args()

    n, changes = normalize_tags(args.dry_run)
    for mid, old_tags, new_tags in changes:
        print(f"#{mid}: {old_tags!r}  ->  {new_tags!r}")
    verb = "would update" if args.dry_run else "updated"
    print(f"\n{verb} {n} row(s).")


if __name__ == "__main__":
    main()
