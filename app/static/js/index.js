/* Empty-state markup for a card or lightbox with no tags. Passed to
 * MI.tagPills, which otherwise renders an empty string. */
const NO_TAGS = '<span class="no-tags">No tags</span>';

/* ── Lazy image observer ── */
    const lazyObserver = new IntersectionObserver((entries, obs) => {
        entries.forEach(e => {
            if (e.isIntersecting) {
                e.target.src = e.target.dataset.src;
                obs.unobserve(e.target);
            }
        });
    }, { rootMargin: '150px', threshold: 0.05 });

    document.addEventListener('DOMContentLoaded', function () {
        document.querySelectorAll('img.thumb').forEach(img => lazyObserver.observe(img));

        const sentinel = document.getElementById('scroll-sentinel');
        const gallery  = document.querySelector('.gallery');
        if (!sentinel || !gallery) return;

        let page = 1, loading = false, exhausted = false;
        const params = new URLSearchParams(window.location.search);

        const esc = MI.esc;   // shared helper (static/js/common.js)

        function createCard(media) {
            const id   = media.id;
            const type = media.type;
            const div  = document.createElement('div');
            div.className = 'gallery-item';
            div.dataset.id = id;
            div.innerHTML =
                `<div class="action-buttons">
                   <button class="action-btn edit-btn" data-action="edit" data-id="${id}" data-stop title="Edit Tags">✎</button>
                   <button class="action-btn delete-btn" data-action="delete" data-id="${id}" data-stop title="Delete File">×</button>
                 </div>
                 <div class="thumb-wrapper">
                   <img class="thumb" data-action="open" data-id="${id}"
                        data-src="/thumbs/${esc(media.thumb)}"
                        src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
                        alt=""/>
                   <div class="thumb-overlay"></div>
                   ${type ? `<span class="type-badge ${esc(type)}">${esc(type)}</span>` : ''}
                   ${media.is_private ? `<span class="type-badge private" title="Only visible to you">private</span>` : ''}
                 </div>
                 <div class="card-body">
                   <div class="card-tags">${MI.tagPills(media.tags, NO_TAGS)}</div>
                 </div>
                 <div id="edit-form-${id}" class="edit-form">
                   <input type="text" id="edit-input-${id}" value="${esc(media.tags||'')}" placeholder="Tags separated by commas…" data-tag-ac>
                   <div class="edit-form-btns">
                     <button class="cancel-btn" data-action="edit-cancel" data-id="${id}">Cancel</button>
                     <button class="save-btn" data-action="edit-save" data-id="${id}">Save</button>
                   </div>
                 </div>`;
            return div;
        }

        function loadNextPage(onDone) {
            if (loading || exhausted) { if (onDone) onDone(false); return; }
            loading = true;
            sentinel.innerHTML = '<div class="loader"></div>';
            page++;
            const url = new URL('/api/media', location.origin);
            ['type','search','exclude_tags','hide_ai','private_only','sort','sort_seed'].forEach(k => {
                const v = params.get(k);
                if (v) url.searchParams.set(k, v);
            });
            url.searchParams.set('page', page);
            fetch(url).then(r => r.json()).then(data => {
                data.media.forEach(m => {
                    const card = createCard(m);
                    gallery.insertBefore(card, sentinel);
                    lazyObserver.observe(card.querySelector('img.thumb'));
                });
                sentinel.innerHTML = '';
                if (!data.has_more) {
                    exhausted = true;
                    sentinel.style.display = 'none';
                    scrollObs.disconnect();
                }
                loading = false;
                if (onDone) onDone(data.media.length > 0);
            }).catch(() => { loading = false; sentinel.innerHTML = ''; if (onDone) onDone(false); });
        }

        // Lets the lightbox pull in the next page when navigating past the
        // last loaded card.
        window.__galleryLoadMore = loadNextPage;

        const scrollObs = new IntersectionObserver(
            entries => { if (entries[0].isIntersecting) loadNextPage(); },
            { rootMargin: '250px' }
        );
        scrollObs.observe(sentinel);
    });

    /* ── Modal ── */
    function openModal(id) {
        fetch('/mediadata/' + id).then(r => r.json()).then(d => {
            const modal    = document.getElementById('modal-bg');
            const filename = d.filepath.split('/').pop();
            const isVideo  = d.type === 'video';
            const container = modal.querySelector('.modal-view-content');
            zoomOut();            // clear any zoom left over from the last image
            container.innerHTML = '';
            const el = document.createElement(isVideo ? 'video' : 'img');
            el.className = 'modal-media';
            el.src = '/media/' + filename;
            if (isVideo) {
                el.controls = true;
            } else {
                el.alt = '';
                // Click-to-zoom, dispatched by common.js. Images and GIFs
                // only — on a video a click belongs to play/pause.
                el.classList.add('zoomable');
                el.dataset.action = 'zoom';
            }
            container.appendChild(el);

            /* Tags in footer */
            const footer = modal.querySelector('.modal-tags');
            footer.innerHTML = d.tags
                ? '<span class="modal-tag-label">Tags:</span>' + MI.tagPills(d.tags)
                : NO_TAGS;

            const dl = document.getElementById('modal-download');
            const dlName = d.download_name || filename;
            dl.href = '/media/' + filename;
            dl.setAttribute('download', dlName);
            dl.textContent = '↓ ' + dlName;

            modal.dataset.currentId = id;
            modal.classList.add('active');
            updateModalNavButtons();
            if (slideshowOn) scheduleSlideshowAdvance();
        });
    }

    function closeModal() {
        stopSlideshow();
        zoomOut();
        const modal = document.getElementById('modal-bg');
        modal.classList.remove('active');
        // Stop video playback
        const v = modal.querySelector('video');
        if (v) { v.pause(); v.src = ''; }
        modal.querySelector('.modal-view-content').innerHTML = '';
        modal.querySelector('.modal-tags').innerHTML = '';
    }

    /* ── Preview zoom ────────────────────────────────────────────────────
     * Click the preview image to magnify it about the cursor; while zoomed
     * the wheel steps the level. scale() with a transform-origin that
     * tracks the pointer holds the point under the cursor still, so moving
     * the mouse pans the magnified image. Only the <img> is transformed and
     * .modal-view-content clips it, so nothing else on the page moves.
     *
     * The clip box is pinned to the image's rendered box in pixels rather
     * than left to CSS: .modal-view-content is a shrink-to-fit flex item,
     * and wherever it comes out wider than a height-capped image the crop
     * would show backdrop down the image's sides. Pinning it only while
     * zoomed leaves the normal layout exactly as it was.
     */
    const ZOOM_DEFAULT = 2.5;
    const ZOOM_MIN     = 1.5;
    const ZOOM_MAX     = 5;
    const ZOOM_STEP    = .5;
    let zoomScale = 0;                    // 0 = not zoomed

    function zoomViewport() {
        return document.querySelector('#modal-bg .modal-view-content');
    }

    function zoomImage() {
        const vp = zoomViewport();
        return vp ? vp.querySelector('img.modal-media') : null;
    }

    /* The cursor as a percentage of the image box. Measured on the viewport,
     * not the image: once zoomed, the image's own rect is the scaled one. */
    function setZoomOrigin(ev, img) {
        const r = img.parentElement.getBoundingClientRect();
        const pct = (v, size) => Math.max(0, Math.min(100, size ? v / size * 100 : 50));
        img.style.transformOrigin =
            pct(ev.clientX - r.left, r.width) + '% ' +
            pct(ev.clientY - r.top,  r.height) + '%';
    }

    function zoomIn(ev, img) {
        const vp = img.parentElement;
        const r  = img.getBoundingClientRect();   // still the untransformed box
        vp.style.width  = r.width + 'px';
        vp.style.height = r.height + 'px';
        vp.classList.add('zoom-viewport');
        img.classList.add('zoomed');
        document.getElementById('modal-bg').classList.add('zoom-active');
        // An image that slides away five seconds into a close look is useless.
        stopSlideshow();
        zoomScale = ZOOM_DEFAULT;
        setZoomOrigin(ev, img);
        img.style.transform = 'scale(' + zoomScale + ')';
    }

    /* Safe to call unconditionally — openModal and closeModal both do. */
    function zoomOut() {
        zoomScale = 0;
        const vp = zoomViewport();
        if (vp) {
            vp.classList.remove('zoom-viewport');
            vp.style.width  = '';
            vp.style.height = '';
            const img = vp.querySelector('img.modal-media');
            if (img) {
                img.classList.remove('zoomed');
                img.style.transform = '';
                img.style.transformOrigin = '';
            }
        }
        const bg = document.getElementById('modal-bg');
        if (bg) bg.classList.remove('zoom-active');
    }

    function zoomBy(step, ev, img) {
        const next = Math.max(ZOOM_MIN, Math.min(ZOOM_MAX, zoomScale + step));
        if (next === zoomScale) return;
        zoomScale = next;
        setZoomOrigin(ev, img);
        img.style.transform = 'scale(' + zoomScale + ')';
    }

    /* ── Lightbox navigation ── */
    function galleryIds() {
        return [...document.querySelectorAll('.gallery-item[data-id]')]
            .map(el => parseInt(el.dataset.id, 10));
    }

    function modalIndex() {
        const cur = parseInt(document.getElementById('modal-bg').dataset.currentId, 10);
        const ids = galleryIds();
        return { ids, idx: ids.indexOf(cur) };
    }

    function modalNav(dir) {
        const { ids, idx } = modalIndex();
        if (idx === -1) return false;
        const target = idx + dir;
        if (target < 0) return false;
        if (target >= ids.length) {
            // Past the last loaded card — fetch the next page, then continue.
            if (window.__galleryLoadMore) {
                window.__galleryLoadMore(gotMore => {
                    const more = galleryIds();
                    if (gotMore && target < more.length) openModal(more[target]);
                    else stopSlideshow();
                });
            } else {
                stopSlideshow();
            }
            return false;
        }
        openModal(ids[target]);
        // Prefetch when the slideshow/user approaches the end of the page.
        if (ids.length - target <= 5 && window.__galleryLoadMore) window.__galleryLoadMore();
        return true;
    }

    function updateModalNavButtons() {
        const { ids, idx } = modalIndex();
        document.querySelector('.modal-nav-prev').disabled = idx <= 0;
        document.querySelector('.modal-nav-next').disabled = idx === -1;
    }

    /* ── Slideshow ── */
    const SLIDESHOW_MS = 5000;
    let slideshowOn = false;
    let slideshowTimer = null;

    function scheduleSlideshowAdvance() {
        clearTimeout(slideshowTimer);
        const modal = document.getElementById('modal-bg');
        const v = modal.querySelector('video');
        if (v) {
            // Let videos play through instead of cutting them off mid-play.
            v.onended = () => { if (slideshowOn) modalNav(1); };
            v.muted = true;   // autoplay is blocked for unmuted video
            v.play().catch(() => {
                slideshowTimer = setTimeout(() => { if (slideshowOn) modalNav(1); }, SLIDESHOW_MS);
            });
        } else {
            slideshowTimer = setTimeout(() => { if (slideshowOn) modalNav(1); }, SLIDESHOW_MS);
        }
    }

    function toggleSlideshow() {
        slideshowOn ? stopSlideshow() : startSlideshow();
    }

    function startSlideshow() {
        slideshowOn = true;
        const btn = document.getElementById('modal-slideshow');
        btn.textContent = '⏸ Stop';
        btn.classList.add('active');
        scheduleSlideshowAdvance();
    }

    function stopSlideshow() {
        if (!slideshowOn && !slideshowTimer) return;
        slideshowOn = false;
        clearTimeout(slideshowTimer);
        slideshowTimer = null;
        const btn = document.getElementById('modal-slideshow');
        if (btn) {
            btn.textContent = '▶ Slideshow';
            btn.classList.remove('active');
        }
    }

    function modalDelete() {
        const id = document.getElementById('modal-bg').dataset.currentId;
        if (!id || !confirm('Are you sure you want to delete this file?')) return;
        MI.post(`/delete/${id}`)
            .then(r => { if (r.ok) location.reload(); else alert('Error deleting file.'); })
            .catch(() => alert('Error deleting file.'));
    }

    /* ── Bulk select / delete ── */
    let selectMode = false;
    const selectedIds = new Set();

    function toggleSelectMode() {
        selectMode = !selectMode;
        document.body.classList.toggle('select-mode', selectMode);
        const toggle = document.getElementById('select-toggle');
        toggle.classList.toggle('active', selectMode);
        toggle.textContent = selectMode ? '✕ Done' : '☑ Select';
        if (selectMode) {
            // Open edit forms would swallow the selection clicks.
            document.querySelectorAll('.edit-form').forEach(f => f.classList.remove('active'));
        } else {
            closeBulkTagEditor();
            bulkClear();
        }
    }

    function updateBulkBar() {
        const n = selectedIds.size;
        document.getElementById('bulk-count').textContent =
            `${n} selected`;
        document.querySelectorAll('.bulk-needs-sel').forEach(b => { b.disabled = n === 0; });
    }

    function bulkClear() {
        selectedIds.clear();
        document.querySelectorAll('.gallery-item.selected').forEach(el => el.classList.remove('selected'));
        updateBulkBar();
    }

    function bulkSelectAll() {
        document.querySelectorAll('.gallery-item[data-id]').forEach(el => {
            selectedIds.add(parseInt(el.dataset.id, 10));
            el.classList.add('selected');
        });
        updateBulkBar();
    }

    function bulkDelete() {
        const ids = [...selectedIds];
        if (!ids.length) return;
        if (!confirm(`Are you sure you want to delete ${ids.length} file${ids.length !== 1 ? 's' : ''}? This cannot be undone.`)) return;
        const btn = document.getElementById('bulk-delete');
        btn.disabled = true;
        btn.textContent = 'Deleting…';
        MI.postJSON('/delete-bulk', { ids })
            .then(r => { if (r.ok) location.reload(); else throw new Error(); })
            .catch(() => {
                alert('Error deleting files.');
                btn.textContent = '🗑 Delete selected';
                updateBulkBar();
            });
    }

    function bulkSetPrivacy(makePrivate) {
        const ids = [...selectedIds];
        if (!ids.length) return;
        const s = ids.length !== 1 ? 's' : '';
        const msg = makePrivate
            ? `Make ${ids.length} file${s} private? They will be encrypted and only visible to their owner.`
            : `Make ${ids.length} file${s} public? They will be decrypted and visible to every user.`;
        if (!confirm(msg)) return;
        MI.postJSON('/privacy-bulk', { ids, private: makePrivate })
            .then(r => { if (r.ok) location.reload(); else throw new Error(); })
            .catch(() => alert('Error changing privacy.'));
    }

    /* Bulk tag editor (add/remove tags on the selection) */
    let bulkTagMode = null;

    function openBulkTagEditor(mode) {
        bulkTagMode = mode;
        const input = document.getElementById('bulk-tag-input');
        input.placeholder = mode === 'add'
            ? 'Tags to add, comma separated…'
            : 'Tags to remove, comma separated…';
        document.getElementById('bulk-tag-apply').textContent = mode === 'add' ? 'Add' : 'Remove';
        input.value = '';
        document.getElementById('bulk-tag-editor').classList.add('active');
        input.focus();
    }

    function closeBulkTagEditor() {
        bulkTagMode = null;
        document.getElementById('bulk-tag-editor').classList.remove('active');
    }

    function applyBulkTagEdit() {
        const tags = document.getElementById('bulk-tag-input').value
            .split(',').map(t => t.trim()).filter(Boolean).join(',');
        if (!bulkTagMode || !tags || !selectedIds.size) return;
        const btn = document.getElementById('bulk-tag-apply');
        btn.disabled = true;
        MI.postJSON('/edit-bulk', {
            ids: [...selectedIds],
            add: bulkTagMode === 'add' ? tags : '',
            remove: bulkTagMode === 'remove' ? tags : ''
        })
            .then(r => { if (r.ok) location.reload(); else throw new Error(); })
            .catch(() => { alert('Error updating tags.'); btn.disabled = false; });
    }

    document.addEventListener('DOMContentLoaded', () => {
        document.getElementById('bulk-tag-input').addEventListener('keydown', e => {
            if (e.key === 'Enter') { e.preventDefault(); applyBulkTagEdit(); }
        });
    });

    // Capture-phase: in select mode a click anywhere on a card toggles its
    // selection instead of triggering the card's own inline handlers.
    document.addEventListener('click', e => {
        if (!selectMode) return;
        const item = e.target.closest('.gallery-item');
        if (!item || !item.dataset.id) return;
        e.preventDefault();
        e.stopPropagation();
        const id = parseInt(item.dataset.id, 10);
        if (selectedIds.has(id)) {
            selectedIds.delete(id);
            item.classList.remove('selected');
        } else {
            selectedIds.add(id);
            item.classList.add('selected');
        }
        updateBulkBar();
    }, true);

    document.addEventListener('keydown', e => {
        if (e.key === 'Escape') {
            if (zoomScale) zoomOut();
            else if (document.getElementById('modal-bg').classList.contains('active')) closeModal();
            else if (bulkTagMode) closeBulkTagEditor();
            else if (selectMode) toggleSelectMode();
            return;
        }
        const modal = document.getElementById('modal-bg');
        if (!modal.classList.contains('active')) return;
        if (/^(input|textarea|select)$/i.test(e.target.tagName)) return;
        if (e.key === 'ArrowRight') { e.preventDefault(); modalNav(1); }
        else if (e.key === 'ArrowLeft') { e.preventDefault(); modalNav(-1); }
    });

    function showEditForm(id) {
        document.querySelectorAll('.edit-form').forEach(f => f.classList.remove('active'));
        document.getElementById('edit-form-' + id).classList.add('active');
    }
    function hideEditForm(id) {
        document.getElementById('edit-form-' + id).classList.remove('active');
    }
    function saveEditedTags(id) {
        const input = document.getElementById('edit-input-' + id);
        const fd = new FormData();
        fd.append('tags', input.value.trim());
        MI.post(`/edit/${id}`, fd)
            .then(r => { if (r.ok) location.reload(); else alert('Error saving.'); })
            .catch(() => alert('Error saving.'));
    }
    function deleteMedia(id) {
        if (!confirm('Are you sure you want to delete this file?')) return;
        MI.post(`/delete/${id}`)
            .then(r => { if (r.ok) location.reload(); else alert('Error deleting file.'); })
            .catch(() => alert('Error deleting file.'));
    }

    /* ── Delegated action wiring ────────────────────────────────────────
     * Replaces the inline handler attributes the CSP doesn't permit. One
     * registration serves both the server-rendered cards and the ones
     * createCard() builds during infinite scroll, because common.js
     * dispatches from a document-level listener.
     */
    MI.on('open',        el => openModal(+el.dataset.id));
    MI.on('edit',        el => showEditForm(+el.dataset.id));
    MI.on('edit-cancel', el => hideEditForm(+el.dataset.id));
    MI.on('edit-save',   el => saveEditedTags(+el.dataset.id));
    MI.on('delete',      el => deleteMedia(+el.dataset.id));

    MI.on('select-mode',    () => toggleSelectMode());
    MI.on('bulk-all',       () => bulkSelectAll());
    MI.on('bulk-clear',     () => bulkClear());
    MI.on('bulk-tags',      el => openBulkTagEditor(el.dataset.mode));
    MI.on('bulk-privacy',   el => bulkSetPrivacy(el.dataset.private === '1'));
    MI.on('bulk-delete',    () => bulkDelete());
    MI.on('bulk-tag-apply', () => applyBulkTagEdit());
    MI.on('bulk-tag-close', () => closeBulkTagEditor());

    MI.on('slideshow',   () => toggleSlideshow());
    MI.on('modal-delete', () => modalDelete());
    MI.on('modal-close', () => closeModal());
    MI.on('modal-prev',  () => modalNav(-1));
    MI.on('modal-next',  () => modalNav(1));
    MI.on('zoom',        (el, ev) => zoomScale ? zoomOut() : zoomIn(ev, el));

    // Backdrop click closes the lightbox. The target check is what the
    // old markup achieved with a stopPropagation() handler on .modal-box:
    // only a click on the backdrop itself counts, not one that bubbled up
    // from the content inside it.
    document.addEventListener('DOMContentLoaded', () => {
        const bg = document.getElementById('modal-bg');
        if (bg) bg.addEventListener('click', e => { if (e.target === bg) closeModal(); });

        // The viewport div outlives every image openModal puts in it, so the
        // zoom listeners are bound once here rather than per image. Both are
        // no-ops until a click has actually zoomed in.
        const vp = zoomViewport();
        if (!vp) return;
        vp.addEventListener('pointermove', e => {
            if (!zoomScale) return;
            const img = zoomImage();
            if (img) setZoomOrigin(e, img);
        });
        // passive:false — .modal-body scrolls, and the wheel is ours while zoomed.
        vp.addEventListener('wheel', e => {
            if (!zoomScale) return;
            const img = zoomImage();
            if (!img) return;
            e.preventDefault();
            zoomBy(e.deltaY < 0 ? ZOOM_STEP : -ZOOM_STEP, e, img);
        }, { passive: false });
        // A resize re-letterboxes the image, leaving the pinned clip box the
        // wrong size for it. Dropping out of the zoom is the honest answer —
        // the next click re-measures.
        window.addEventListener('resize', () => { if (zoomScale) zoomOut(); });
    });
