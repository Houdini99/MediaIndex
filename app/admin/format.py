"""Display formatting shared by the admin route modules.

Byte sizes are rendered in three places that now live in two modules — the
panel's storage totals, the integrity report's orphan sizes, and the
duplicate finder's per-item / wasted / freed figures — so the formatter
cannot sit in either one without the other importing across.
"""


def human_size(n):
    """Byte count → a short human-readable string ('4.2 MB').

    Bytes are shown whole; every larger unit gets one decimal. TB is the
    ceiling, so a petabyte-scale figure keeps counting in TB rather than
    running off the end of the unit list.
    """
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return f"{n:.1f} {unit}" if unit != 'B' else f"{int(n)} {unit}"
        n /= 1024
