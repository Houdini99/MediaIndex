"""The CSP forbids inline script — this keeps the codebase honest about it.

`script-src 'self'` (no `'unsafe-inline'`) is only worth having while there
is genuinely no inline script left. The failure mode is nasty and silent:
add one `onclick="…"` and the button simply stops working in production,
with nothing but a console message. Nothing in a Python test suite would
otherwise notice, because none of this is exercised server-side.

So the check is mechanical and repo-wide. If it fails, the fix is to move
the behaviour into a .js file — see `static/js/common.js` for the
`data-action` / `data-confirm` / `data-autosubmit` dispatchers that exist
for exactly this.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / 'app' / 'templates'
SCRIPTS = ROOT / 'app' / 'static' / 'js'

#: Every intrinsic-event attribute, not just the handful in use today —
#: `onmouseover=` would be just as broken as `onclick=`.
INLINE_HANDLER = re.compile(
    r'''<[^>]*\son[a-z]+\s*=\s*["']''', re.IGNORECASE)

#: A <script> with no src is an inline block. `<script src=…>` is fine.
INLINE_SCRIPT = re.compile(r'<script(?![^>]*\bsrc\s*=)[^>]*>', re.IGNORECASE)


def html_files():
    return sorted(TEMPLATES.rglob('*.html'))


def js_files():
    return sorted(SCRIPTS.glob('*.js'))


def strip_jinja_comments(text):
    """Drop {# … #} blocks — prose about inline script is not inline script."""
    return re.sub(r'\{#.*?#\}', '', text, flags=re.DOTALL)


def strip_js_comments(text):
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
    return re.sub(r'^\s*//.*$', '', text, flags=re.MULTILINE)


@pytest.mark.parametrize('path', html_files(), ids=lambda p: p.name)
def test_template_has_no_inline_event_handler(path):
    body = strip_jinja_comments(path.read_text())
    found = INLINE_HANDLER.findall(body)
    assert not found, (
        f"{path.relative_to(ROOT)} has an inline event handler {found!r}. "
        "The CSP blocks it — use data-action/data-confirm/data-autosubmit.")


@pytest.mark.parametrize('path', html_files(), ids=lambda p: p.name)
def test_template_has_no_inline_script_block(path):
    body = strip_jinja_comments(path.read_text())
    found = INLINE_SCRIPT.findall(body)
    assert not found, (
        f"{path.relative_to(ROOT)} has an inline <script> block. The CSP "
        "blocks it — move the code to static/js/ and pass server values "
        "through the `body_data` block as data-* attributes.")


@pytest.mark.parametrize('path', js_files(), ids=lambda p: p.name)
def test_generated_markup_has_no_inline_event_handler(path):
    """Handlers baked into innerHTML templates are inline script too, and
    are the easiest kind to reintroduce by accident."""
    body = strip_js_comments(path.read_text())
    found = re.findall(r'\son[a-z]+\s*=\s*\\?["\']', body, re.IGNORECASE)
    assert not found, (
        f"{path.relative_to(ROOT)} builds markup with an inline handler "
        f"{found!r}. Emit data-action and register it with MI.on().")


@pytest.mark.parametrize('path', js_files(), ids=lambda p: p.name)
def test_no_eval_or_function_constructor(path):
    body = strip_js_comments(path.read_text())
    found = re.findall(r'\beval\s*\(|\bnew\s+Function\s*\(', body)
    assert not found, f"{path.relative_to(ROOT)} uses {found!r}; CSP blocks it."


def test_escape_helper_is_defined_exactly_once():
    """Four copies existed, one of them a partial re-implementation nested
    inside a map callback. Escaping is the only thing between a free-text
    tag and script execution — it gets one definition."""
    definitions = []
    for path in js_files():
        body = strip_js_comments(path.read_text())
        for match in re.findall(r'function\s+(esc\w*)\s*\(|(?:const|let|var)'
                                r'\s+(esc\w*)\s*=\s*(?:function|\(|s\s*=>)',
                                body):
            name = match[0] or match[1]
            # Aliases of the shared helper are the point, not a duplicate.
            if f'{name} = MI.esc' in body:
                continue
            definitions.append(f'{path.name}:{name}')
    assert definitions == ['common.js:esc'] or definitions == [], (
        f"escape helper defined in more than one place: {definitions}")


#: Primitives that must have exactly one implementation, keyed by the literal
#: that gives a re-implementation away, valued by the helper to use instead.
#: Each of these had between two and five copies before common.js absorbed
#: them.
SHARED_PRIMITIVES = {
    'csrf-token': 'MI.csrf()',
    'X-CSRFToken': 'MI.post() / MI.postJSON()',
    'tag-pill': 'MI.tagPills()',
}


@pytest.mark.parametrize('needle', sorted(SHARED_PRIMITIVES))
def test_shared_primitive_is_implemented_only_in_common_js(needle):
    """Re-implementing one of these is how they forked the first time.

    Unlike the escape helper this cannot key on a function name — the copies
    were spelled `csrfToken`, `post`, `postJSON`, `makePills` and
    `makeTagPills` — so it keys on the literal each one has to mention.
    A file that legitimately needs to *match* `.tag-pill` rather than build
    it should be added to an allow-list here, deliberately.
    """
    offenders = sorted(
        path.name for path in js_files()
        if path.name != 'common.js' and needle in strip_js_comments(path.read_text())
    )
    assert not offenders, (
        f"{offenders} mention {needle!r} directly; use "
        f"{SHARED_PRIMITIVES[needle]} from common.js instead.")


def test_common_js_is_loaded_before_everything_else():
    """MI.esc and the dispatchers must exist before any page script runs.
    base.html loads common.js ahead of the extra_js block."""
    base = (TEMPLATES / 'base.html').read_text()
    assert 'js/common.js' in base
    assert base.index('js/common.js') < base.index('{% block extra_js %}')
    # No other template may load it again or earlier.
    for path in html_files():
        if path.name == 'base.html':
            continue
        assert 'js/common.js' not in path.read_text(), path.name
