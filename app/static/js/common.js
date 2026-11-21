/* Shared front-end helpers, loaded on every page before any other script.
 *
 * Exists for two reasons:
 *
 *  1. `esc()` lives here, once. Escaping is the only thing standing
 *     between a free-text tag and script execution, so it is the last
 *     place that should have divergent copies.
 *
 *  2. The CSP doesn't allow `'unsafe-inline'` for scripts, which means
 *     no `onclick="…"` attributes and no inline `<script>` blocks anywhere.
 *     The delegated dispatchers below replace them: markup declares intent
 *     with `data-action` / `data-confirm` / `data-autosubmit`, and the
 *     behaviour lives in a real .js file the CSP can allow by origin.
 *
 * Delegation also means dynamically-built cards need no wiring of their
 * own — a `data-action` on freshly-inserted markup works immediately,
 * which is what the infinite-scroll gallery and the feed rely on.
 */
window.MI = window.MI || {};

/* HTML-escape for interpolation into innerHTML. The single copy. */
MI.esc = function (s) {
    return String(s === null || s === undefined ? '' : s)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
};

/* ── CSRF ───────────────────────────────────────────────────────────────
 * The token was read five different ways across five files — twice as an
 * arrow function, twice as a const captured at DOMContentLoaded, once
 * inline inside the poster. Read on each call rather than cached, which is
 * what the two arrow-function versions already did.
 */
MI.csrf = function () {
    return document.querySelector('meta[name="csrf-token"]').content;
};

/* ── POST helpers ───────────────────────────────────────────────────────
 * Two entry points because there are genuinely two request shapes, and
 * collapsing them would break both: a FormData body must NOT carry an
 * explicit Content-Type (the browser has to add the multipart boundary
 * itself), while a JSON body must.
 *
 * Both resolve to the raw Response, so call sites keep whatever handling
 * they already had — `.then(r => r.ok ? … : …)`, `.then(r => r.json())`,
 * or nothing at all.
 *
 * Both send `X-Requested-With`, which is what lets a route tell a scripted
 * call from a browser navigation and answer in JSON instead of a redirect.
 * That distinction matters: `fetch` follows redirects, so a route that ends
 * in one hands back a 200 HTML page and `response.ok` is true no matter
 * what actually happened.
 */
MI.post = function (url, body) {
    return fetch(url, {
        method: 'POST',
        headers: { 'X-CSRFToken': MI.csrf(), 'X-Requested-With': 'XMLHttpRequest' },
        body: body,
    });
};

MI.postJSON = function (url, data) {
    return fetch(url, {
        method: 'POST',
        headers: {
            'X-CSRFToken': MI.csrf(),
            'X-Requested-With': 'XMLHttpRequest',
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(data),
    });
};

/* POST with upload progress, for the upload page. fetch() reports none,
 * so this is XHR — with MI.post's headers. onProgress(loaded, total) runs
 * as bytes go out (total is 0 when the browser cannot tell) and once more
 * as onProgress(null) when the last byte is sent and the server is
 * working. Resolves with the XHR once it loads, whatever its status;
 * rejects when the network fails or the request is aborted.
 */
MI.postWithProgress = function (url, body, onProgress) {
    return new Promise(function (resolve, reject) {
        const xhr = new XMLHttpRequest();
        xhr.open('POST', url);
        xhr.setRequestHeader('X-CSRFToken', MI.csrf());
        xhr.setRequestHeader('X-Requested-With', 'XMLHttpRequest');
        xhr.responseType = 'json';
        if (onProgress) {
            xhr.upload.addEventListener('progress', function (ev) {
                onProgress(ev.loaded, ev.lengthComputable ? ev.total : 0);
            });
            xhr.upload.addEventListener('load', function () { onProgress(null); });
        }
        xhr.addEventListener('load', function () { resolve(xhr); });
        xhr.addEventListener('error', function () { reject(new Error('network')); });
        xhr.addEventListener('abort', function () { reject(new Error('aborted')); });
        xhr.send(body);
    });
};

/* ── Tag pills ──────────────────────────────────────────────────────────
 * Built in three places (the gallery card, the gallery lightbox footer,
 * and the feed slide) from three copies of the same loop. They differed in
 * exactly one respect — what an empty tag string renders as — so that is
 * the parameter: the gallery passes its "No tags" placeholder, the feed
 * passes nothing and gets an empty string.
 *
 * Tag text is user-controlled and goes through MI.esc; the class name is
 * derived here and never comes from the tag itself.
 */
MI.tagPills = function (tagsStr, emptyHtml) {
    if (!tagsStr) return emptyHtml || '';
    return tagsStr.split(',').filter(function (t) { return t.trim(); }).map(function (t) {
        const tag = t.trim();
        let cls = 'tag-pill';
        if (tag.startsWith('user:')) cls += ' user-tag';
        else if (['image', 'gif', 'video'].includes(tag)) cls += ' type-tag';
        return '<span class="' + cls + '">' + MI.esc(tag) + '</span>';
    }).join('');
};

/* ── Click-to-zoom ──────────────────────────────────────────────────────
 * The gallery lightbox and the feed share the gesture: click an image to
 * magnify it about the cursor, wheel to step the level, click again (or
 * Escape) to leave. Their geometry differs, so each passes three hooks:
 *   origin(ev, img)  set img.style.transformOrigin for the pointer
 *   enter(img)       runs before the first scale (clip box, classes, pausing)
 *   leave(img, was)  runs after the transform is cleared; img is null and
 *                    `was` 0 when nothing was zoomed
 * The returned object's `scale` is 0 when not zoomed; out() is safe to
 * call at any time.
 */
MI.zoom = function (hooks) {
    const DEFAULT = 2.5, MIN = 1.5, MAX = 5, STEP = .5;
    let scale = 0, el = null;
    function apply(ev) {
        hooks.origin(ev, el);
        el.style.transform = 'scale(' + scale + ')';
    }
    return {
        get scale() { return scale; },
        get el() { return el; },
        in: function (ev, img) {
            el = img;
            hooks.enter(img);
            scale = DEFAULT;
            apply(ev);
        },
        out: function () {
            const img = el, was = scale;
            el = null;
            scale = 0;
            if (img) {
                img.style.transform = '';
                img.style.transformOrigin = '';
            }
            hooks.leave(img, was);
        },
        /* One wheel notch while zoomed: up magnifies, down shrinks. */
        wheel: function (ev) {
            const next = Math.max(MIN, Math.min(MAX, scale + (ev.deltaY < 0 ? STEP : -STEP)));
            if (next === scale) return;
            scale = next;
            apply(ev);
        },
        /* Pointer movement pans: the origin follows the cursor. */
        move: function (ev) {
            if (scale && el) hooks.origin(ev, el);
        },
    };
};

/* ── data-action: delegated click dispatch ──────────────────────────────
 * Markup:  <button data-action="delete" data-id="12" data-stop>×</button>
 * Script:  MI.on('delete', (el, ev) => deleteMedia(+el.dataset.id));
 *
 * `data-stop` calls stopPropagation() before the handler — the gallery
 * cards need it, since a card-level click opens the modal and the buttons
 * sitting on top of it must not also trigger that.
 */
MI.actions = {};

MI.on = function (name, fn) {
    MI.actions[name] = fn;
};

document.addEventListener('click', function (ev) {
    const el = ev.target.closest('[data-action]');
    if (!el) return;
    const fn = MI.actions[el.dataset.action];
    if (!fn) return;
    if (el.dataset.stop !== undefined) ev.stopPropagation();
    fn(el, ev);
});

/* ── data-confirm: destructive actions ask first ────────────────────────
 * Replaces `onsubmit="return confirm('…')"`. Works on a <form> (checked at
 * submit) and on a button (checked at click).
 *
 *   data-confirm="Delete {ip} now?"   — {field} interpolates a form input
 *   data-confirm-unless="dry_run"     — skip the prompt when that checkbox
 *                                        is ticked (a dry run changes
 *                                        nothing, so it needs no warning)
 */
function confirmMessage(el, form) {
    return (el.dataset.confirm || '').replace(/\{(\w+)\}/g, function (m, field) {
        const input = form && form.elements ? form.elements[field] : null;
        return input && input.value !== undefined ? input.value : m;
    });
}

function confirmSkipped(el, form) {
    const unless = el.dataset.confirmUnless;
    if (!unless || !form || !form.elements) return false;
    const box = form.elements[unless];
    return !!(box && box.checked);
}

document.addEventListener('submit', function (ev) {
    const form = ev.target;
    if (!form.dataset || form.dataset.confirm === undefined) return;
    if (confirmSkipped(form, form)) return;
    if (!window.confirm(confirmMessage(form, form))) ev.preventDefault();
});

document.addEventListener('click', function (ev) {
    const el = ev.target.closest('[data-confirm]');
    if (!el || el.tagName === 'FORM') return;
    const form = el.form || el.closest('form');
    // A submit button inside a form is handled by the submit listener
    // above when the form itself carries data-confirm; only prompt here
    // for the button's own data-confirm.
    if (confirmSkipped(el, form)) return;
    if (!window.confirm(confirmMessage(el, form))) {
        ev.preventDefault();
        ev.stopPropagation();
    }
});

/* ── data-autosubmit: a control that submits its form on change ─────────
 * Replaces `onchange="this.form.submit()"` on the gallery's sort select
 * and filter toggles.
 */
document.addEventListener('change', function (ev) {
    const el = ev.target;
    if (!el.dataset || el.dataset.autosubmit === undefined) return;
    const form = el.form || el.closest('form');
    if (form) form.submit();
});

/* ── data-scroll-bottom: start scrolled to the newest content ───────────
 * Replaces the inline script in the admin log viewer.
 */
document.addEventListener('DOMContentLoaded', function () {
    document.querySelectorAll('[data-scroll-bottom]').forEach(function (el) {
        el.scrollTop = el.scrollHeight;
    });
});

/* ── Account menu ───────────────────────────────────────────────────────
 * Below 1024px the Settings / Admin / Sign Out links fold into a menu
 * behind the user chip (common.css). Above that they are inline and the
 * chip opens nothing, so the toggle stands down there rather than flip an
 * aria-expanded that describes no menu. A tap anywhere else, or Escape,
 * closes it.
 */
(function () {
    const narrow = window.matchMedia('(max-width: 1023px)');

    function setOpen(right, open) {
        right.classList.toggle('is-open', open);
        const chip = right.querySelector('.user-chip');
        if (chip) chip.setAttribute('aria-expanded', open ? 'true' : 'false');
    }
    function closeAll() {
        document.querySelectorAll('.nav-right.is-open').forEach(r => setOpen(r, false));
    }

    MI.on('account-menu', function (el) {
        if (!narrow.matches) return;
        const right = el.closest('.nav-right');
        if (right) setOpen(right, !right.classList.contains('is-open'));
    });
    document.addEventListener('click', function (ev) {
        const open = document.querySelector('.nav-right.is-open');
        if (open && !open.contains(ev.target)) setOpen(open, false);
    });
    document.addEventListener('keydown', function (ev) {
        if (ev.key === 'Escape') closeAll();
    });
    // Widening the window past the breakpoint turns the menu back into
    // inline links; a stale open state would reappear on the way back.
    narrow.addEventListener('change', closeAll);
})();

/* ── Admin tabs ─────────────────────────────────────────────────────────
 * On a phone the tabs are one row that scrolls sideways (admin.css); start
 * it scrolled so the current page's tab is in view, not cut off the edge.
 */
document.addEventListener('DOMContentLoaded', function () {
    const tabs = document.querySelector('.admin-tabs');
    const active = tabs && tabs.querySelector('a.active');
    if (!active || tabs.scrollWidth <= tabs.clientWidth) return;
    const offset = active.getBoundingClientRect().left - tabs.getBoundingClientRect().left;
    tabs.scrollLeft += offset - (tabs.clientWidth - active.offsetWidth) / 2;
});
