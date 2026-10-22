"""Repair: re-render image thumbnails that came out sideways.

Photos a phone stored rotated-with-a-flag need a thumbnail that honours
the EXIF Orientation tag. New uploads get one; this re-renders the
thumbnails on disk that don't (see app/media/thumb_repair.py). Each
repaired thumbnail gets a new file name, so browsers that cached the old
one for a year fetch the new one immediately. Private images are handled
too — decrypted to the scratch dir with the owner's key and re-encrypted.

The database lives on the stack's internal network and is not reachable
from the host, so run this inside the web container:

    docker exec -it mediaindex-web python scripts/regen_image_thumbs.py --dry-run
    docker exec -it mediaindex-web python scripts/regen_image_thumbs.py

Usage:
    python scripts/regen_image_thumbs.py [--dry-run] [--all]
"""
import argparse

import _bootstrap  # sys.path + SECRET_KEY; must precede app.*

from app.media.thumb_repair import repair_rotated_thumbs


def main():
    p = argparse.ArgumentParser(
        description="Re-render image thumbnails whose original is EXIF-rotated.")
    p.add_argument("--dry-run", action="store_true",
                   help="List what would be re-rendered without writing")
    p.add_argument("--all", action="store_true",
                   help="Re-render every image thumbnail, rotated or not")
    args = p.parse_args()

    _bootstrap.require_db()
    repaired, skipped = repair_rotated_thumbs(dry_run=args.dry_run,
                                              everything=args.all)
    for mid, old, new, orientation in repaired:
        print(f"#{mid}: orientation {orientation}  {old}  ->  {new}")
    for mid, reason in skipped:
        print(f"#{mid}: skipped ({reason})")
    verb = "would re-render" if args.dry_run else "re-rendered"
    print(f"\n{verb} {len(repaired)} thumbnail(s); skipped {len(skipped)}.")
    if repaired and not args.dry_run:
        print("Their duplicate-finder fingerprints were dropped; the next "
              "scan on the Duplicates tab recomputes them.")


if __name__ == "__main__":
    main()
