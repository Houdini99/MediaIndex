/* Comma-aware tag autocomplete.

   Native <datalist> matches the whole input value, so suggestions die after
   the first comma. This widget completes only the token behind the last
   comma instead. Attach by adding `data-tag-ac` to a text input — set-up is
   delegated through focusin, so dynamically inserted inputs work too.
   Tag vocabulary comes from /api/tags (fetched once per page). */
(function () {
    const MAX_ITEMS = 8;
    let tagsPromise = null;
    let openWidget = null;   // the one widget whose list is currently open

    function allTags() {
        if (!tagsPromise) {
            tagsPromise = fetch('/api/tags').then(r => r.json()).catch(() => []);
        }
        return tagsPromise;
    }

    const escHtml = MI.esc;   // shared helper (static/js/common.js)

    function attach(input) {
        input.setAttribute('autocomplete', 'off');
        input.setAttribute('spellcheck', 'false');

        const wrap = input.parentElement;
        if (getComputedStyle(wrap).position === 'static') wrap.style.position = 'relative';
        const list = document.createElement('div');
        list.className = 'tag-ac-list';
        wrap.appendChild(list);

        const widget = { input, list, items: [], active: -1 };

        function close() {
            list.style.display = 'none';
            list.innerHTML = '';
            widget.items = [];
            widget.active = -1;
            if (openWidget === widget) openWidget = null;
        }

        function currentToken() {
            const start = input.value.lastIndexOf(',') + 1;
            return input.value.slice(start).trim().toLowerCase();
        }

        function accept(tag) {
            const start = input.value.lastIndexOf(',') + 1;
            const lead = start === 0 ? '' : input.value.slice(0, start) + ' ';
            input.value = lead + tag + ', ';
            close();
            input.focus();
            input.dispatchEvent(new Event('input', { bubbles: true }));
        }
        widget.accept = accept;

        function highlight(idx) {
            widget.active = idx;
            [...list.children].forEach((el, i) => el.classList.toggle('active', i === idx));
            if (idx >= 0) list.children[idx].scrollIntoView({ block: 'nearest' });
        }
        widget.highlight = highlight;
        widget.close = close;

        function render() {
            if (!widget.items.length) { close(); return; }
            list.innerHTML = widget.items
                .map(t => `<div class="tag-ac-item">${escHtml(t)}</div>`)
                .join('');
            [...list.children].forEach((el, i) => {
                // mousedown, not click: fires before the input loses focus
                el.addEventListener('mousedown', e => { e.preventDefault(); accept(widget.items[i]); });
                el.addEventListener('mouseenter', () => highlight(i));
            });
            // Open upward when the input sits near the bottom of the viewport
            // (e.g. the gallery bulk bar) so the list isn't clipped.
            const r = input.getBoundingClientRect();
            const below = window.innerHeight - r.bottom;
            list.classList.toggle('up', below < 220 && r.top > below);
            list.style.display = 'block';
            widget.active = -1;
            openWidget = widget;
        }

        function update() {
            const token = currentToken();
            if (!token) { close(); return; }
            const taken = new Set(
                input.value.split(',').map(t => t.trim().toLowerCase()).filter(Boolean)
            );
            allTags().then(tags => {
                if (currentToken() !== token) return;   // input changed meanwhile
                const lower = t => t.toLowerCase();
                const starts = [], contains = [];
                for (const t of tags) {
                    const lt = lower(t);
                    if (lt === token || taken.has(lt)) continue;
                    if (lt.startsWith(token)) starts.push(t);
                    else if (lt.includes(token)) contains.push(t);
                }
                widget.items = starts.concat(contains).slice(0, MAX_ITEMS);
                render();
            });
        }

        input.addEventListener('input', update);
        input.addEventListener('focus', update);
        input.addEventListener('focusout', close);
        input.__tagAc = widget;
    }

    document.addEventListener('focusin', e => {
        const inp = e.target;
        if (inp.matches && inp.matches('input[data-tag-ac]') && !inp.__tagAc) attach(inp);
    });

    // Capture phase on document: runs before the pages' own handlers
    // (form submit on Enter, feed "apply on Enter", gallery Escape), so an
    // open suggestion list can consume navigation keys first.
    document.addEventListener('keydown', e => {
        const w = openWidget;
        if (!w || e.target !== w.input) return;
        if (e.key === 'ArrowDown') {
            e.preventDefault();
            w.highlight((w.active + 1) % w.items.length);
        } else if (e.key === 'ArrowUp') {
            e.preventDefault();
            w.highlight((w.active - 1 + w.items.length) % w.items.length);
        } else if ((e.key === 'Enter' || e.key === 'Tab') && w.active >= 0) {
            e.preventDefault();
            e.stopPropagation();
            w.accept(w.items[w.active]);
        } else if (e.key === 'Enter') {
            w.close();   // fall through: let the form/page handle the submit
        } else if (e.key === 'Escape') {
            e.stopPropagation();
            w.close();
        }
    }, true);
})();
