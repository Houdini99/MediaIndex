"""File helpers shared across the app."""
import os


def remove_if_exists(path):
    """Delete `path` if it is there; returns whether a file was removed.

    A file that is already gone, or that vanishes between a caller's check
    and the delete, is not an error. An empty path (an optional thumbnail,
    say) is ignored. Where the file must exist, call os.remove() instead,
    so that a missing one still raises.
    """
    if not path:
        return False
    try:
        os.remove(path)
    except FileNotFoundError:
        return False
    return True
