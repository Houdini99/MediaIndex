document.addEventListener('DOMContentLoaded', () => {
    const queueList  = document.getElementById('queueList');
    const queueEmpty = document.getElementById('queueEmpty');

    const STATUS_LABELS = {
        pending:     'processing…',
        promoting:   'saving…',
        duplicate:   'duplicate — already in your index',
        unsupported: 'unsupported file type',
        error:       'processing failed — re-upload or discard',
    };

    fetch('/api/tags').then(r => r.json()).then(tags => {
        const dl = document.getElementById('tag-suggestions');
        tags.forEach(t => { const o = document.createElement('option'); o.value = t; dl.appendChild(o); });
    }).catch(() => {});

    /* Deliberately not shared with the admin pages, which format sizes
     * server-side via app/admin/format.py. Two differences make this the
     * wrong function to merge with that one:
     *   - a staging row's `size` is nullable (a file seen but not yet
     *     measured), and an unknown size must render as nothing, not as a
     *     confident "0 B";
     *   - it drops the decimal past 10 ("40 MB", not "40.1 MB"), which
     *     suits a dense queue list. */
    function fmtSize(bytes) {
        if (bytes == null) return '';
        const units = ['B', 'KB', 'MB', 'GB'];
        let i = 0, n = bytes;
        while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
        return n.toFixed(n >= 10 || i === 0 ? 0 : 1) + ' ' + units[i];
    }

    // Preserve the user's half-typed tags across the 10 s refresh.
    function drafts() {
        const d = {};
        queueList.querySelectorAll('.queue-item').forEach(item => {
            const tags = item.querySelector('input[type="text"]');
            const priv = item.querySelector('input[type="checkbox"]');
            d[item.dataset.id] = {
                tags: tags ? tags.value : '',
                priv: priv ? priv.checked : false,
                focused: item.contains(document.activeElement),
            };
        });
        return d;
    }

    function render(items) {
        const saved = drafts();
        // Skip re-rendering while the user is typing in a row.
        if (Object.values(saved).some(d => d.focused)) return;

        queueList.innerHTML = '';
        queueEmpty.hidden = items.length > 0;

        items.forEach(item => {
            const row = document.createElement('div');
            row.className = 'queue-item';
            row.dataset.id = item.id;

            let media;
            if (item.thumb_url) {
                media = document.createElement('img');
                media.src = item.thumb_url;
                media.loading = 'lazy';
                media.addEventListener('click', () => window.open(item.file_url, '_blank'));
                media.className = 'queue-thumb';
            } else {
                media = document.createElement('div');
                media.textContent = (item.fname.split('.').pop() || '?').toUpperCase();
                media.className = 'queue-thumb queue-thumb-placeholder';
            }

            const details = document.createElement('div');
            details.className = 'queue-details';

            const head = document.createElement('div');
            head.className = 'queue-item-head';
            const fname = document.createElement('div');
            fname.className = 'queue-filename';
            fname.textContent = item.fname;
            fname.title = item.fname;
            const size = document.createElement('span');
            size.className = 'queue-size';
            size.textContent = fmtSize(item.size);
            head.appendChild(fname);
            head.appendChild(size);
            details.appendChild(head);

            if (item.status !== 'ready') {
                const chip = document.createElement('span');
                chip.className = 'status-chip status-' + item.status;
                chip.textContent = STATUS_LABELS[item.status] || item.status;
                details.appendChild(chip);
            }

            const actions = document.createElement('div');
            actions.className = 'queue-actions';

            if (item.status === 'ready') {
                const tagInput = document.createElement('input');
                tagInput.type = 'text';
                tagInput.placeholder = 'Tags (separated by commas)…';
                tagInput.setAttribute('list', 'tag-suggestions');

                const privacyLabel = document.createElement('label');
                privacyLabel.className = 'privacy-toggle';
                privacyLabel.title = 'Only you will see this file';
                const privacyInput = document.createElement('input');
                privacyInput.type = 'checkbox';
                privacyInput.value = '1';
                const privacyText = document.createElement('span');
                privacyText.textContent = 'Private (only visible to you)';
                privacyLabel.appendChild(privacyInput);
                privacyLabel.appendChild(privacyText);

                const draft = saved[item.id];
                if (draft) {
                    tagInput.value = draft.tags;
                    privacyInput.checked = draft.priv;
                }

                const submitBtn = document.createElement('button');
                submitBtn.className = 'btn btn-primary';
                submitBtn.textContent = 'Save to index';
                submitBtn.addEventListener('click', () => {
                    submitBtn.disabled = true;
                    const body = new FormData();
                    body.append('tags', tagInput.value);
                    if (privacyInput.checked) body.append('private', '1');
                    MI.post(`/api/staging/${item.id}/promote`, body)
                        .then(r => r.json().then(data => ({ ok: r.ok, data })))
                        .then(({ ok, data }) => {
                            if (ok && data.status === 'duplicate') {
                                alert('That file was already in your index — removed from the queue.');
                            } else if (!ok && data.error) {
                                alert(data.error);
                            }
                            refresh();
                        })
                        .catch(() => { submitBtn.disabled = false; });
                });

                details.appendChild(tagInput);
                details.appendChild(privacyLabel);
                actions.appendChild(submitBtn);
            }

            if (item.status !== 'pending' && item.status !== 'promoting') {
                const rejectBtn = document.createElement('button');
                rejectBtn.className = 'btn btn-danger';
                rejectBtn.textContent = item.status === 'ready' ? 'Reject' : 'Discard';
                rejectBtn.addEventListener('click', () => {
                    if (!confirm(`Delete "${item.fname}" from your WebDAV folder?`)) return;
                    rejectBtn.disabled = true;
                    MI.post(`/api/staging/${item.id}/reject`, new FormData())
                        .then(refresh)
                        .catch(() => { rejectBtn.disabled = false; });
                });
                actions.appendChild(rejectBtn);
            }

            details.appendChild(actions);
            row.appendChild(media);
            row.appendChild(details);
            queueList.appendChild(row);
        });
    }

    function refresh() {
        return fetch('/api/staging')
            .then(r => r.json())
            .then(data => render(data.items))
            .catch(() => {});
    }

    refresh();
    // Skip background polling while the tab is hidden (saves battery and
    // server round-trips on phones); catch up as soon as it's visible again.
    setInterval(() => { if (!document.hidden) refresh(); }, 10000);
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden) refresh();
    });

    // ── WebDAV app-password management ────────────────────────────────────
    const generateBtn = document.getElementById('davGenerateBtn');
    const revokeBtn   = document.getElementById('davRevokeBtn');
    const reveal      = document.getElementById('davReveal');
    const passwordEl  = document.getElementById('davPassword');
    const statusEl    = document.getElementById('davStatus');

    generateBtn.addEventListener('click', () => {
        generateBtn.disabled = true;
        MI.post('/api/dav/password', new FormData())
            .then(r => r.json())
            .then(data => {
                passwordEl.textContent = data.password;
                reveal.hidden = false;
                revokeBtn.hidden = false;
                statusEl.textContent = 'App password set';
                generateBtn.textContent = 'Regenerate password';
            })
            .finally(() => { generateBtn.disabled = false; });
    });

    revokeBtn.addEventListener('click', () => {
        if (!confirm('Revoke the WebDAV password? Mounted folders will stop working.')) return;
        MI.post('/api/dav/password/revoke', new FormData())
            .then(() => {
                reveal.hidden = true;
                revokeBtn.hidden = true;
                statusEl.textContent = 'Not set up yet';
                generateBtn.textContent = 'Generate password';
            });
    });

    document.getElementById('davCopyBtn').addEventListener('click', () => {
        navigator.clipboard.writeText(passwordEl.textContent).catch(() => {});
    });
});
