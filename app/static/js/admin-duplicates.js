/* Admin → Near Duplicates.
 *
 * The page shell is server-rendered; this drives the two live parts:
 *   - the scan progress bar, polled from /admin/duplicates/status because a
 *     scan outlives the request that started it (and may be running in the
 *     other gunicorn worker);
 *   - the result grid, fetched from /admin/duplicates/groups whenever the
 *     match settings change, so re-grouping never costs a page reload.
 *
 * Every node carrying user-controlled text (tags, filenames, usernames) is
 * built with textContent — never innerHTML.
 */
document.addEventListener('DOMContentLoaded', () => {
    const groupsEl   = document.getElementById('groups');
    const emptyEl    = document.getElementById('groupsEmpty');
    const noteEl     = document.getElementById('groupsNote');
    const countEl    = document.getElementById('groupCount');
    const pagerEl    = document.getElementById('pager');
    const pageLabel  = document.getElementById('pageLabel');
    const matchForm  = document.getElementById('matchForm');
    const progress   = document.getElementById('progress');
    const fill       = document.getElementById('progressFill');
    const meta       = document.getElementById('progressMeta');
    const scanBtn    = document.getElementById('scanBtn');
    const cancelBtn  = document.getElementById('cancelBtn');
    const mergeTags  = document.getElementById('mergeTags');
    const exportBtn  = document.getElementById('exportBtn');
    const thresholdInput = document.getElementById('thresholdInput');
    const thresholdNote  = document.getElementById('thresholdNote');

    let page = 1;
    let pollTimer = null;
    let wasRunning = false;

    // ── helpers ───────────────────────────────────────────────────────────
    function el(tag, className, text) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== undefined && text !== null) node.textContent = String(text);
        return node;
    }

    /* Thin local wrapper: every call site here destructures { ok, data },
     * so the envelope stays even though the request itself is shared. */
    function postJSON(url, body) {
        return MI.postJSON(url, body)
            .then(r => r.json().then(data => ({ ok: r.ok, data })));
    }

    function fmtDuration(seconds) {
        if (seconds === null || seconds === undefined) return '—';
        const s = Math.round(seconds);
        if (s < 60) return s + 's';
        const m = Math.floor(s / 60);
        return m < 60 ? `${m}m ${s % 60}s` : `${Math.floor(m / 60)}h ${m % 60}m`;
    }

    /* Match settings live in the query string, so a reload — or a shared
     * link — reproduces exactly the view being looked at. */
    function currentParams() {
        const params = new URLSearchParams(window.location.search);
        new FormData(matchForm).forEach((value, key) => params.set(key, value));
        if (!matchForm.cross_uploader_only.checked) params.delete('cross_uploader_only');
        return params;
    }

    function syncUrl(params) {
        const query = params.toString();
        window.history.replaceState({}, '', query ? `?${query}` : window.location.pathname);
        if (exportBtn) exportBtn.href = `/admin/duplicates/export.csv?${query}`;
    }

    // ── result grid ───────────────────────────────────────────────────────
    function itemTile(group, item) {
        const tile = el('div', 'dupe-item' + (item.suggested ? ' suggested' : ''));
        tile.dataset.id = item.id;

        const thumbWrap = el('label', 'dupe-thumb-wrap');
        if (item.thumb_url) {
            const img = el('img', 'dupe-thumb');
            img.loading = 'lazy';
            img.src = item.thumb_url;
            img.alt = `media ${item.id}`;
            thumbWrap.appendChild(img);
        } else {
            thumbWrap.appendChild(el('div', 'dupe-thumb dupe-thumb-missing', 'no thumb'));
        }

        const keep = el('input', 'dupe-keep');
        keep.type = 'radio';
        keep.name = `keep-${group.key}`;
        keep.value = item.id;
        keep.checked = item.id === group.keeper_id;
        keep.title = 'Keep this one';
        thumbWrap.appendChild(keep);
        if (item.suggested) thumbWrap.appendChild(el('span', 'dupe-best', '★ best'));
        tile.appendChild(thumbWrap);

        const body = el('div', 'dupe-item-body');
        const head = el('div', 'dupe-item-head');
        head.appendChild(el('span', 'dupe-id', `#${item.id}`));
        head.appendChild(el('span', 'dupe-type', item.ftype));
        if (item.frames > 1) head.appendChild(el('span', 'dupe-type', `${item.frames} frames`));
        body.appendChild(head);

        const dims = (item.width && item.height) ? `${item.width}×${item.height}` : 'unknown size';
        body.appendChild(el('div', 'dupe-meta', `${dims} · ${item.size_h}`));
        body.appendChild(el('div', 'dupe-meta', `${item.uploader} · ${(item.upload_date || '').slice(0, 10)}`));

        const tags = el('div', 'dupe-tags', item.tags || '(no tags)');
        tags.title = item.tags || '';
        body.appendChild(tags);

        const links = el('div', 'dupe-links');
        const open = el('a', 'dupe-link', 'Open file');
        open.href = item.media_url;
        open.target = '_blank';
        open.rel = 'noopener';
        links.appendChild(open);
        body.appendChild(links);

        tile.appendChild(body);
        return tile;
    }

    function groupCard(group) {
        const card = el('div', 'dupe-group');
        card.dataset.key = group.key;

        const head = el('div', 'dupe-group-head');
        const title = el('div', 'dupe-group-title');
        title.appendChild(el('span', 'dupe-similarity', `${group.similarity}% alike`));
        title.appendChild(el('span', 'dupe-group-sub',
            `${group.size} items · ${group.wasted_h} reclaimable · distance ${group.distance}`));
        head.appendChild(title);

        const badges = el('div', 'dupe-badges');
        group.types.forEach(t => badges.appendChild(el('span', 'dupe-badge', t)));
        if (group.cross_uploader) badges.appendChild(el('span', 'dupe-badge warn', 'different uploaders'));
        if (group.has_subset) badges.appendChild(el('span', 'dupe-badge warn', 'subset match'));
        head.appendChild(badges);
        card.appendChild(head);

        const items = el('div', 'dupe-items');
        group.items.forEach(item => items.appendChild(itemTile(group, item)));
        card.appendChild(items);

        const actions = el('div', 'dupe-group-actions');
        const resolve = el('button', 'btn btn-primary btn-sm', 'Keep selected · quarantine the rest');
        resolve.type = 'button';
        resolve.addEventListener('click', () => resolveGroup(card, group, resolve));
        actions.appendChild(resolve);

        const dismiss = el('button', 'btn btn-ghost btn-sm', 'Not duplicates');
        dismiss.type = 'button';
        dismiss.addEventListener('click', () => dismissGroup(card, group, dismiss));
        actions.appendChild(dismiss);

        const status = el('span', 'dupe-group-status');
        actions.appendChild(status);
        card.appendChild(actions);
        return card;
    }

    function resolveGroup(card, group, button) {
        const chosen = card.querySelector('.dupe-keep:checked');
        if (!chosen) return;
        const keepId = parseInt(chosen.value, 10);
        const losers = group.ids.filter(id => id !== keepId);
        if (!window.confirm(
            `Quarantine ${losers.length} item(s) and keep #${keepId}?\n\n` +
            'Nothing is deleted yet — quarantined items are tagged and listed ' +
            'below, where they can be restored or permanently deleted.')) return;

        button.disabled = true;
        const status = card.querySelector('.dupe-group-status');
        status.textContent = 'Quarantining…';
        postJSON('/admin/duplicates/resolve', {
            keep_id: keepId,
            ids: group.ids,
            merge_tags: mergeTags ? mergeTags.checked : false,
            distance: Math.round(group.distance),
        }).then(({ ok, data }) => {
            if (!ok) {
                status.textContent = data.error || 'failed';
                button.disabled = false;
                return;
            }
            card.classList.add('resolved');
            status.textContent = `${data.quarantined} quarantined` +
                (data.failed ? `, ${data.failed} failed` : '') + ' — reload to refresh the list';
        }).catch(() => {
            status.textContent = 'request failed';
            button.disabled = false;
        });
    }

    function dismissGroup(card, group, button) {
        if (!window.confirm('Mark these as NOT duplicates? They will be excluded from future groupings.')) return;
        button.disabled = true;
        const status = card.querySelector('.dupe-group-status');
        postJSON('/admin/duplicates/dismiss', { ids: group.ids }).then(({ ok, data }) => {
            if (!ok) {
                status.textContent = data.error || 'failed';
                button.disabled = false;
                return;
            }
            card.classList.add('resolved');
            status.textContent = `${data.dismissed} pair(s) dismissed`;
        }).catch(() => {
            status.textContent = 'request failed';
            button.disabled = false;
        });
    }

    function renderGroups(payload) {
        groupsEl.innerHTML = '';
        const groups = payload.groups || [];
        countEl.textContent = payload.stats.groups;
        countEl.classList.toggle('ok', payload.stats.groups === 0);

        document.getElementById('statGroups').textContent = payload.stats.groups;
        document.getElementById('statRedundant').textContent = payload.stats.redundant;
        document.getElementById('statWasted').textContent = payload.stats.wasted_h;

        noteEl.hidden = !payload.truncated;
        if (payload.truncated) {
            noteEl.textContent = 'Too many candidate pairs to compare exhaustively — ' +
                'the list is partial. Lower the max distance for a complete result.';
        }

        if (!groups.length) {
            emptyEl.hidden = false;
            emptyEl.textContent = payload.stats.compared
                ? `No near-duplicates among ${payload.stats.compared} fingerprinted item(s) at this setting. Try a higher max distance.`
                : 'No fingerprints yet — build the index first.';
            pagerEl.hidden = true;
            return;
        }
        emptyEl.hidden = true;
        groups.forEach(group => groupsEl.appendChild(groupCard(group)));

        pagerEl.hidden = payload.pages <= 1;
        pageLabel.textContent = `Page ${payload.page} of ${payload.pages}`;
        document.getElementById('prevPage').disabled = payload.page <= 1;
        document.getElementById('nextPage').disabled = !payload.has_more;
    }

    function loadGroups() {
        const params = currentParams();
        syncUrl(params);
        params.set('page', page);
        countEl.textContent = '…';
        fetch(`/admin/duplicates/groups?${params.toString()}`)
            .then(r => r.json())
            .then(renderGroups)
            .catch(() => {
                emptyEl.hidden = false;
                emptyEl.textContent = 'Could not load groups.';
            });
    }

    // ── scan progress ─────────────────────────────────────────────────────
    function renderStatus(status) {
        document.getElementById('statIndexed').textContent = status.coverage.hashed;
        document.getElementById('statPending').textContent = status.coverage.pending;

        const running = !!status.running;
        progress.hidden = !running && status.state !== 'error';
        cancelBtn.hidden = !running;
        scanBtn.disabled = running;

        if (running) {
            fill.style.width = `${status.percent}%`;
            fill.classList.remove('error');
            meta.textContent =
                `${status.processed} / ${status.total} · ${status.hashed} hashed · ` +
                `${status.skipped} already indexed · ${status.failed} failed · ` +
                `elapsed ${fmtDuration(status.elapsed)} · ETA ${fmtDuration(status.eta)}` +
                (status.cancel_requested ? ' · cancelling…' : '');
        } else if (status.state === 'error') {
            fill.style.width = '100%';
            fill.classList.add('error');
            meta.textContent = `Scan failed: ${status.error || 'unknown error'}`;
        }

        if (wasRunning && !running) {
            // A finished scan changes the index — pull fresh groups.
            wasRunning = false;
            loadGroups();
        }
        wasRunning = running;
        clearTimeout(pollTimer);
        pollTimer = setTimeout(pollStatus, running ? 2000 : 15000);
    }

    function pollStatus() {
        fetch('/admin/duplicates/status')
            .then(r => r.json())
            .then(renderStatus)
            .catch(() => { pollTimer = setTimeout(pollStatus, 15000); });
    }

    cancelBtn.addEventListener('click', () => {
        cancelBtn.disabled = true;
        postJSON('/admin/duplicates/cancel', {}).finally(() => {
            cancelBtn.disabled = false;
            pollStatus();
        });
    });

    // ── bulk resolve, always previewed ────────────────────────────────────
    document.getElementById('resolveAllBtn').addEventListener('click', () => {
        const params = {};
        currentParams().forEach((value, key) => { params[key] = value; });
        const merge = mergeTags ? mergeTags.checked : false;
        postJSON('/admin/duplicates/resolve-all', { params, merge_tags: merge, dry_run: true })
            .then(({ data }) => {
                if (!data.quarantined) {
                    window.alert('Nothing to resolve at the current settings.');
                    return;
                }
                if (!window.confirm(
                    `Keep the suggested best copy in each of ${data.groups} group(s) and ` +
                    `quarantine ${data.quarantined} duplicate(s), freeing about ${data.bytes_h} once purged?\n\n` +
                    'Nothing is deleted by this step.')) return;
                return postJSON('/admin/duplicates/resolve-all',
                                { params, merge_tags: merge, dry_run: false })
                    .then(({ data: done }) => {
                        window.alert(`${done.quarantined} item(s) quarantined` +
                                     (done.failed ? `, ${done.failed} failed` : '') + '.');
                        window.location.reload();
                    });
            })
            .catch(() => window.alert('Request failed.'));
    });

    // ── wiring ────────────────────────────────────────────────────────────
    matchForm.addEventListener('submit', e => e.preventDefault());
    matchForm.addEventListener('change', () => { page = 1; loadGroups(); });
    if (thresholdInput) {
        thresholdInput.addEventListener('input', () => {
            thresholdNote.textContent = `${thresholdInput.value} bits`;
        });
    }
    document.getElementById('prevPage').addEventListener('click', () => {
        if (page > 1) { page--; loadGroups(); }
    });
    document.getElementById('nextPage').addEventListener('click', () => {
        page++; loadGroups();
    });

    // The scan form must post the *current* match settings too, so a scan
    // started from a tweaked view comes back to that same view.
    document.getElementById('scanForm').addEventListener('submit', () => {
        const params = currentParams();
        ['algo', 'threshold', 'min_group', 'sort'].forEach(name => {
            const field = document.getElementById('scanForm').elements[name];
            if (field && params.has(name)) field.value = params.get(name);
        });
    });

    syncUrl(currentParams());
    loadGroups();
    pollStatus();
});
