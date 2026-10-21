"""Code both ingest bots run unchanged: the yt-dlp link resolver, the site
logins it downloads with (site_logins.py), and the message-parsing helpers
that decide what counts as tags and what as a link.

Both bot images are built from the repository root (see build.sh) so each
can copy this package in beside its own. Before that they built from their
own directories, could not share a package, and carried byte-for-byte
copies kept in step by a test that diffed them.
"""
