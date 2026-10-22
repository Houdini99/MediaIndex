"""Small parsers for request input that more than one blueprint needs."""


def form_flag(value):
    """True for a checkbox or switch value that means "on": 1, true, on or
    yes, in any case. A JSON true counts too."""
    return str(value).strip().lower() in ('1', 'true', 'on', 'yes')


def json_ids(data, key, limit=500):
    """The list of integer ids under `key` in a parsed JSON body, cut to
    `limit`, or None when it is missing, empty or not all integers."""
    ids = data.get(key)
    if not isinstance(ids, list) or not ids:
        return None
    try:
        return [int(i) for i in ids[:limit]]
    except (TypeError, ValueError):
        return None
