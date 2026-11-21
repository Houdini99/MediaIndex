document.addEventListener('DOMContentLoaded', () => {
    const form        = document.getElementById('uploadForm');
    const filesInput  = document.getElementById('filesInput');
    const previewsDiv = document.getElementById('previews');
    const dropZone    = document.getElementById('dropZone');
    const summary     = document.getElementById('uploadSummary');
    const uploadBtn   = document.getElementById('uploadBtn');
    const progress    = document.getElementById('uploadProgress');
    const progressFill = document.getElementById('uploadProgressFill');
    const progressText = document.getElementById('uploadProgressText');
    const progressPct  = document.getElementById('uploadProgressPct');
    const messages    = document.getElementById('uploadMessages');

    let uploading = false;

    function fmtSize(bytes) {
        const units = ['B', 'KB', 'MB', 'GB'];
        let i = 0, n = bytes;
        while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
        return n.toFixed(n >= 10 || i === 0 ? 0 : 1) + ' ' + units[i];
    }

    function pickFiles() {
        if (!uploading) filesInput.click();
    }

    dropZone.addEventListener('click', () => {
        // The whole drop zone opens the picker, including the "Select files"
        // <em>, which has no handler of its own.
        pickFiles();
    });
    dropZone.addEventListener('keydown', e => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pickFiles(); }
    });
    dropZone.addEventListener('dragover', e => { e.preventDefault(); dropZone.classList.add('drag-over'); });
    dropZone.addEventListener('dragleave', () => dropZone.classList.remove('drag-over'));
    dropZone.addEventListener('drop', e => {
        e.preventDefault();
        dropZone.classList.remove('drag-over');
        if (uploading) return;
        filesInput.files = e.dataTransfer.files;
        filesInput.dispatchEvent(new Event('change'));
    });

    function renderSummary(files) {
        if (!files.length) { summary.hidden = true; return; }
        let total = 0;
        Array.from(files).forEach(f => { total += f.size; });
        summary.textContent = `${files.length} file${files.length !== 1 ? 's' : ''} · ${fmtSize(total)}`;
        summary.hidden = false;
    }

    filesInput.addEventListener('change', event => {
        const files = event.target.files;
        previewsDiv.innerHTML = '';
        renderSummary(files);
        if (!files.length) return;

        Array.from(files).forEach((file, i) => {
            const div = document.createElement('div');
            div.className = 'file-row preview-item';

            const url = URL.createObjectURL(file);
            let media;
            if (file.type.startsWith('image/')) {
                media = document.createElement('img');
                media.src = url;
                media.addEventListener('load', () => URL.revokeObjectURL(url), { once: true });
            } else if (file.type.startsWith('video/')) {
                media = document.createElement('video');
                media.src = url;
                media.muted = true;
                media.playsInline = true;
                // Enough to paint the first frame as the preview on iOS,
                // which otherwise shows a blank box until played.
                media.preload = 'metadata';
                media.addEventListener('loadeddata', () => URL.revokeObjectURL(url), { once: true });
            } else {
                media = document.createElement('div');
                media.textContent = file.name.split('.').pop().toUpperCase();
                media.classList.add('preview-thumb-placeholder');
                URL.revokeObjectURL(url);
            }
            media.classList.add('preview-thumb');

            const details  = document.createElement('div');
            details.className = 'preview-details';
            const fname    = document.createElement('div');
            fname.className = 'preview-filename';
            const nameText = document.createElement('span');
            nameText.className = 'preview-name';
            nameText.textContent = file.name;
            nameText.title = file.name;
            const sizeText = document.createElement('span');
            sizeText.className = 'preview-size';
            sizeText.textContent = fmtSize(file.size);
            fname.appendChild(nameText);
            fname.appendChild(sizeText);
            const tagInput = document.createElement('input');
            tagInput.type = 'text';
            tagInput.name = 'tags_' + i;
            tagInput.placeholder = 'Tags (separated by commas)…';
            tagInput.setAttribute('enterkeyhint', 'next');
            // Comma-aware autocomplete (js/tag-autocomplete.js): the native
            // <datalist> matched the whole value, so it died after the first comma.
            tagInput.setAttribute('data-tag-ac', '');
            // Enter would submit the whole upload from the first field;
            // on a phone keyboard it moves on to the next file's tags.
            tagInput.addEventListener('keydown', e => {
                if (e.key !== 'Enter') return;
                e.preventDefault();
                const inputs = [...previewsDiv.querySelectorAll('input[type="text"]')];
                const next = inputs[inputs.indexOf(tagInput) + 1];
                if (next) next.focus(); else tagInput.blur();
            });

            const privacyLabel = document.createElement('label');
            privacyLabel.className = 'privacy-toggle';
            privacyLabel.title = 'Only you will see this file';
            const privacyInput = document.createElement('input');
            privacyInput.type = 'checkbox';
            privacyInput.name = 'private_' + i;
            privacyInput.value = '1';
            const privacyText = document.createElement('span');
            privacyText.textContent = 'Private (only visible to you)';
            privacyLabel.appendChild(privacyInput);
            privacyLabel.appendChild(privacyText);

            details.appendChild(fname);
            details.appendChild(tagInput);
            details.appendChild(privacyLabel);
            div.appendChild(media);
            div.appendChild(details);
            previewsDiv.appendChild(div);
        });
    });

    // ── Upload with progress ──────────────────────────────────────────────
    // A plain form POST gives no sign of life until the server answers —
    // minutes, for a video sent over a phone's uplink, with nothing on
    // screen to say it hasn't hung. XHR reports the bytes as they go. The
    // route answers an XHR with its results as JSON, rendered here like the
    // flash messages the form POST would have produced.
    const FLASH_ICONS = { success: '✓', danger: '✕' };

    function showMessages(list) {
        messages.innerHTML = '';
        list.forEach(m => {
            const row = document.createElement('div');
            row.className = 'flash flash-' + (m.category || 'warning');
            const icon = document.createElement('div');
            icon.className = 'flash-icon';
            icon.textContent = FLASH_ICONS[m.category] || '!';
            const text = document.createElement('span');
            text.textContent = m.text;
            row.appendChild(icon);
            row.appendChild(text);
            messages.appendChild(row);
        });
        messages.hidden = !list.length;
        if (list.length) messages.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

    function setBusy(on) {
        uploading = on;
        uploadBtn.disabled = on;
        form.classList.toggle('is-uploading', on);
        previewsDiv.querySelectorAll('input').forEach(inp => { inp.disabled = on; });
        if (!on) progress.hidden = true;
    }

    function setProgress(fraction, loaded, total) {
        const pct = Math.round(fraction * 100);
        progress.hidden = false;
        progressFill.style.width = pct + '%';
        progressPct.textContent = pct + '%';
        progressText.textContent = total
            ? `Uploading… ${fmtSize(loaded)} of ${fmtSize(total)}`
            : 'Uploading…';
    }

    function resetForm() {
        form.reset();
        previewsDiv.innerHTML = '';
        renderSummary([]);
    }

    // Leaving mid-upload throws the upload away; say so first.
    window.addEventListener('beforeunload', e => {
        if (!uploading) return;
        e.preventDefault();
        e.returnValue = '';
    });

    form.addEventListener('submit', e => {
        if (!window.FormData || !window.XMLHttpRequest) return;   // plain POST
        e.preventDefault();
        if (uploading) return;
        if (!filesInput.files.length) { pickFiles(); return; }

        // FormData is read before the inputs are disabled: disabled fields
        // are left out of it.
        const body = new FormData(form);
        setBusy(true);
        setProgress(0, 0, 0);
        messages.hidden = true;

        const url = form.getAttribute('action') || window.location.pathname;
        MI.postWithProgress(url, body, (loaded, total) => {
            if (loaded !== null) {
                if (total) setProgress(loaded / total, loaded, total);
                return;
            }
            // Every byte is sent; the server is still hashing, encrypting
            // and thumbnailing before it answers.
            progressFill.style.width = '100%';
            progressPct.textContent = '';
            progressText.textContent = 'Processing…';
        }).then(xhr => {
            const data = xhr.response;
            setBusy(false);
            // An expired session answers with the login page, followed to
            // as a redirect: no JSON, and a responseURL that says where.
            if (xhr.responseURL && new URL(xhr.responseURL).pathname.endsWith('/login')) {
                window.location.href = xhr.responseURL;
                return;
            }
            if (xhr.status !== 200 || !data || !Array.isArray(data.messages)) {
                showMessages([{ category: 'danger', text: `Upload failed (${xhr.status || 'no answer'}) — try again.` }]);
                return;
            }
            showMessages(data.messages);
            resetForm();
        }).catch(() => {
            setBusy(false);
            showMessages([{ category: 'danger', text: 'Upload failed — check your connection and try again.' }]);
        });
    });
});
