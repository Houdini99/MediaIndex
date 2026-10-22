const container = document.getElementById('feedContainer');
const initialLoader = document.getElementById('initialLoader');

const STORAGE_KEY = 'mediaindex.feedSettings';
const DEFAULT_SETTINGS = {
    types: ['image', 'gif', 'video'],
    includeTags: '',
    excludeTags: '',
    hideAi: false,
    volume: 1.0,
    muted: true,
    slideshow: false,
    slideshowTimer: 5,   // seconds per image/gif; videos advance when they end
};

function loadSettings() {
    try {
        const raw = JSON.parse(localStorage.getItem(STORAGE_KEY) || '{}');
        const merged = { ...DEFAULT_SETTINGS, ...raw };
        if (!Array.isArray(merged.types) || !merged.types.length) merged.types = [...DEFAULT_SETTINGS.types];
        merged.volume = Math.max(0, Math.min(1, Number(merged.volume) || 0));
        merged.muted = !!merged.muted;
        merged.hideAi = !!merged.hideAi;
        merged.slideshow = !!merged.slideshow;
        let t = Math.round(Number(merged.slideshowTimer));
        if (!isFinite(t) || t < 1) t = DEFAULT_SETTINGS.slideshowTimer;
        merged.slideshowTimer = Math.max(1, Math.min(120, t));
        return merged;
    } catch (_) {
        return { ...DEFAULT_SETTINGS };
    }
}
function saveSettings() {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(settings)); } catch (_) {}
}
// The volume slider fires on `input`, i.e. per pixel of drag, so the
// synchronous stringify + setItem is debounced. The value is applied to
// the videos immediately either way; only the write waits.
let saveTimer = null;
function saveSettingsSoon() {
    if (saveTimer) clearTimeout(saveTimer);
    saveTimer = setTimeout(() => { saveTimer = null; saveSettings(); }, 250);
}

const settings = loadSettings();

let seed = Math.floor(Math.random() * 2147483647);
let page = 0;
let loading = false;
let exhausted = false;
let firstLoad = true;

//: IDs shown recently, newest last, used to keep the reshuffle that makes
//  the feed endless from handing back what was just on screen.
//  Filtering happens here rather than in the query because `/api/feed`
//  paginates by slicing a seeded shuffle: dropping rows server-side would
//  move every page boundary underneath a walk already in progress.
const RECENT_MEMORY = 60;
let recentIds = [];
//: Set on the reshuffle so the *next* page knows to skip what it has seen.
let justReshuffled = false;

function rememberIds(ids) {
    recentIds = recentIds.concat(ids).slice(-RECENT_MEMORY);
}

// Never filter a cycle down to nothing, and never down to almost nothing: a
// small library has few unseen items to offer, and excluding all but one
// turns every reshuffle into a single-slide page — one request per slide.
// Half the library is the compromise: the most recently watched items cannot
// come back, and a cycle still arrives in one useful batch.
function dropRecentlySeen(media, total) {
    const budget = Math.max(0, Math.min(recentIds.length, Math.floor((total || 0) / 2)));
    if (!budget) return media;
    const skip = new Set(recentIds.slice(-budget));
    const fresh = media.filter(m => !skip.has(m.id));
    return fresh.length ? fresh : media;
}

const esc = MI.esc;   // shared helper (static/js/common.js)

let fullscreen = false;

// Slide sizing is CSS's job (`--feed-nav-h` in feed.css). Inline pixel
// heights set here would silently beat the stylesheet's `dvh` rules — the
// mobile URL bar would resize the viewport, `resize` would rewrite every
// slide, and the scroll would land off its snap point mid-swipe.

// The live slide list. Rebuilding it with querySelectorAll on every keypress,
// volume tick and slideshow tick was O(total slides) against a list that only
// ever grew; appended and pruned in one place instead.
let slides = [];

//: Live, not cached — a reader can flip the OS setting mid-session.
const reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
function scrollBehavior() {
    return reduceMotion.matches ? 'auto' : 'smooth';
}

function slideIndexFromScroll() {
    if (!slides.length) return 0;
    // getBoundingClientRect, not clientHeight: a slide is only a whole number
    // of pixels tall when the display is unscaled. At 125%/150% the height is
    // fractional (841.59px for a nominal 842), clientHeight rounds it, and the
    // error compounds with every slide — far enough down the feed it picks the
    // wrong slide entirely.
    const h = slides[0].getBoundingClientRect().height || 1;
    return Math.max(0, Math.min(Math.round(container.scrollTop / h), slides.length - 1));
}
function forEachVideo(fn) {
    for (const s of slides) {
        const v = s.querySelector('video');
        if (v) fn(v);
    }
}

function fmtTime(t) {
    if (!isFinite(t) || isNaN(t) || t < 0) return '0:00';
    const m = Math.floor(t / 60);
    const s = Math.floor(t % 60).toString().padStart(2, '0');
    return `${m}:${s}`;
}

function buildSlide(m) {
    const slide = document.createElement('div');
    slide.className = 'feed-slide';
    slide.dataset.id = m.id;
    slide.dataset.type = m.type;
    // Geometry, background and scroll-snap all come from `.feed-slide`.

    const isVideo = m.type === 'video';
    if (isVideo) slide.classList.add('has-video');

    const initial = (m.uploaded_by || '?')[0].toUpperCase();

    const privateBadge = m.is_private
        ? `<span class="feed-type private" title="Only visible to you">private</span>`
        : '';
    const dlName = m.download_name || m.filepath;
    // Only the owner (or an admin) can actually delete — the server refuses
    // anyone else. Offering the button regardless meant the refusal looked
    // exactly like a success.
    const deleteBtn = m.can_delete
        ? `<button class="feed-btn is-danger" data-action="feed-delete" data-id="${m.id}" title="Delete">×</button>`
        : '';
    slide.innerHTML = `
      <span class="feed-type ${esc(m.type)}">${esc(m.type)}</span>
      ${privateBadge}
      <div class="feed-actions">
        <a class="feed-btn" href="/media/${esc(m.filepath)}" download="${esc(dlName)}" title="Download">↓</a>
        ${deleteBtn}
      </div>
      <div class="feed-overlay">
        <div class="feed-uploader">
          <div class="user-avatar">${esc(initial)}</div>
          <span>${esc(m.uploaded_by || 'Unknown')}</span>
        </div>
        <div class="feed-tags">${MI.tagPills(m.tags)}</div>
      </div>
    `;

    const mediaEl = document.createElement(isVideo ? 'video' : 'img');
    mediaEl.className = 'feed-media';
    mediaEl.dataset.src = '/media/' + m.filepath;
    // Positioning and object-fit live in `.feed-media` only; setting them
    // here too would start an `!important` fight with the stylesheet.
    if (isVideo) {
        mediaEl.muted = settings.muted;
        mediaEl.volume = settings.volume;
        mediaEl.loop = !settings.slideshow;   // loop off during slideshow so `ended` fires
        mediaEl.playsInline = true;
        mediaEl.preload = 'metadata';
        mediaEl.addEventListener('click', () => toggleMute());
    } else {
        // Click-to-zoom, dispatched by common.js. Not on video: a click
        // there already toggles mute.
        mediaEl.classList.add('is-zoomable');
        mediaEl.dataset.action = 'feed-zoom';
        // The image *is* the content, so `alt=""` (decorative) was wrong. Tags
        // are the only description the index holds.
        mediaEl.alt = m.tags
            ? `${m.type} tagged ${m.tags}`
            : `Untagged ${m.type} from ${m.uploaded_by || 'an unknown uploader'}`;
    }
    slide.insertBefore(mediaEl, slide.firstChild);

    if (isVideo) attachVideoProgress(slide, mediaEl);

    return slide;
}

// ── Video progress bar (per-slide) ────────────────────────────────────────
// One shared drag state + a single pair of document listeners. Attaching
// mousemove/mouseup to `document` *per slide* would leak one handler for
// every video ever built — a long feed would run hundreds on each move.
let activeProgressDrag = null;

function progressSeek(drag, e) {
    const rect = drag.track.getBoundingClientRect();
    const clientX = e.touches ? e.touches[0].clientX : e.clientX;
    const pct = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
    if (drag.video.duration) drag.video.currentTime = pct * drag.video.duration;
}
function endProgressDrag() {
    if (!activeProgressDrag) return;
    activeProgressDrag.bar.classList.remove('is-dragging');
    activeProgressDrag = null;
}
document.addEventListener('mousemove', (e) => { if (activeProgressDrag) progressSeek(activeProgressDrag, e); });
document.addEventListener('mouseup', endProgressDrag);

function attachVideoProgress(slide, video) {
    const bar = document.createElement('div');
    bar.className = 'feed-progress';
    bar.innerHTML = `
      <div class="feed-progress-time">
        <span data-progress-current>0:00</span>
        <span data-progress-total>0:00</span>
      </div>
      <div class="feed-progress-track" data-progress-track>
        <div class="feed-progress-fill" data-progress-fill></div>
        <div class="feed-progress-handle" data-progress-handle></div>
      </div>
    `;
    slide.appendChild(bar);

    const track = bar.querySelector('[data-progress-track]');
    const fill = bar.querySelector('[data-progress-fill]');
    const handle = bar.querySelector('[data-progress-handle]');
    const cur = bar.querySelector('[data-progress-current]');
    const tot = bar.querySelector('[data-progress-total]');

    const onMeta = () => { tot.textContent = fmtTime(video.duration); };
    if (video.readyState >= 1) onMeta(); else video.addEventListener('loadedmetadata', onMeta);

    video.addEventListener('timeupdate', () => {
        if (!video.duration) return;
        const pct = (video.currentTime / video.duration) * 100;
        fill.style.width = pct + '%';
        handle.style.left = pct + '%';
        cur.textContent = fmtTime(video.currentTime);
    });

    const drag = { video, track, bar };
    const begin = (e) => {
        e.stopPropagation();
        activeProgressDrag = drag;
        bar.classList.add('is-dragging');
        progressSeek(drag, e);
    };
    track.addEventListener('mousedown', begin);
    track.addEventListener('click', (e) => e.stopPropagation());
    track.addEventListener('touchstart', begin, { passive: true });
    track.addEventListener('touchmove', (e) => {
        if (activeProgressDrag !== drag) return;
        e.preventDefault();
        progressSeek(drag, e);
    }, { passive: false });
    track.addEventListener('touchend', endProgressDrag);
    track.addEventListener('touchcancel', endProgressDrag);
}

// ── Seeking (Left / Right) ────────────────────────────────────────────────
const SEEK_STEP = 5;   // seconds per arrow press
let seekFlashBar = null;
let seekFlashTimer = null;

// The scrubber only shows on hover, so a keyboard seek would otherwise give no
// feedback at all — flash it for a moment after each jump. (Fullscreen mode
// hides the bar outright; seeking still works, silently, which is the point.)
function flashProgress(slide) {
    const bar = slide.querySelector('.feed-progress');
    if (!bar) return;
    if (seekFlashTimer) clearTimeout(seekFlashTimer);
    if (seekFlashBar && seekFlashBar !== bar) seekFlashBar.classList.remove('is-seeking');
    bar.classList.add('is-seeking');
    seekFlashBar = bar;
    seekFlashTimer = setTimeout(() => {
        bar.classList.remove('is-seeking');
        seekFlashBar = null;
        seekFlashTimer = null;
    }, 900);
}

function seekSlideVideo(slide, delta) {
    if (!slide) return;
    const v = slide.querySelector('video');
    if (!v || !v.getAttribute('src')) return;   // windowed-out slides have no source
    const d = v.duration;
    let t = v.currentTime + delta;
    // Stop just short of the end rather than exactly on it: landing on the
    // duration fires `ended`, which would restart a looping video — or skip to
    // the next slide during a slideshow — off a 5-second nudge.
    if (isFinite(d) && d > 0) t = Math.min(t, Math.max(0, d - 0.25));
    v.currentTime = Math.max(0, t);
    flashProgress(slide);
}

// ── Volume / mute ─────────────────────────────────────────────────────────
const volumeBtn = document.getElementById('feedVolumeBtn');
const volumeSlider = document.getElementById('feedVolumeSlider');
const volumeIcon = volumeBtn.querySelector('.feed-icon');

function applyAudioToVideos() {
    forEachVideo(v => {
        v.volume = settings.volume;
        v.muted = settings.muted;
    });
}
function updateVolumeUI() {
    volumeSlider.value = String(Math.round(settings.volume * 100));
    const muted = settings.muted || settings.volume === 0;
    const wave2 = volumeIcon.querySelector('[data-vol-wave="2"]');
    const wave3 = volumeIcon.querySelector('[data-vol-wave="3"]');
    const muteX = volumeIcon.querySelector('[data-vol-mute]');
    if (wave2) wave2.style.opacity = (!muted && settings.volume >= 0.34) ? '1' : '0';
    if (wave3) wave3.style.opacity = (!muted && settings.volume >= 0.67) ? '1' : '0';
    if (muteX) muteX.style.display = muted ? '' : 'none';
    volumeBtn.classList.toggle('is-muted', muted);
}
function setMuted(m) {
    settings.muted = !!m;
    if (!settings.muted && settings.volume === 0) { settings.volume = 0.5; }
    saveSettings();
    applyAudioToVideos();
    updateVolumeUI();
}
function toggleMute() { setMuted(!settings.muted); }
function setVolume(v) {
    settings.volume = Math.max(0, Math.min(1, v));
    settings.muted = settings.volume === 0;
    saveSettingsSoon();
    applyAudioToVideos();
    updateVolumeUI();
}

volumeBtn.addEventListener('click', () => toggleMute());
volumeSlider.addEventListener('input', (e) => setVolume(Number(e.target.value) / 100));
updateVolumeUI();

// ── Settings panel ────────────────────────────────────────────────────────
const settingsBtn = document.getElementById('feedSettingsBtn');
const settingsPanel = document.getElementById('feedSettingsPanel');
const settingsClose = document.getElementById('feedSettingsClose');
const settingsApply = document.getElementById('feedSettingsApply');
const settingsReset = document.getElementById('feedSettingsReset');
const includeInput = document.getElementById('feedIncludeTags');
const excludeInput = document.getElementById('feedExcludeTags');
const typeChecks = document.querySelectorAll('[data-type-toggle]');
const hideAiCheck = document.getElementById('feedHideAi');

function syncSettingsPanel() {
    typeChecks.forEach(cb => {
        cb.checked = settings.types.includes(cb.dataset.typeToggle);
    });
    includeInput.value = settings.includeTags;
    excludeInput.value = settings.excludeTags;
    if (hideAiCheck) hideAiCheck.checked = settings.hideAi;
    if (slideshowToggle) slideshowToggle.checked = settings.slideshow;
    if (slideshowTimerInput) slideshowTimerInput.value = String(settings.slideshowTimer);
}
// Focus handling for the panel. It calls itself a dialog, so it
// has to behave like one: keep Tab inside it while it is open, and hand focus
// back to the button that opened it on the way out — otherwise Tab walks off
// into a feed the reader cannot see.
const FOCUSABLE = 'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])';

function panelFocusables() {
    return [...settingsPanel.querySelectorAll(FOCUSABLE)].filter(el => !el.disabled && el.offsetParent !== null);
}

function trapPanelTab(e) {
    if (e.key !== 'Tab' || settingsPanel.hidden) return;
    const items = panelFocusables();
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
}

function openSettings() {
    syncSettingsPanel();
    settingsPanel.hidden = false;
    settingsPanel.setAttribute('aria-modal', 'true');
    settingsBtn.setAttribute('aria-expanded', 'true');
    document.addEventListener('keydown', trapPanelTab, true);
    requestAnimationFrame(() => {
        settingsPanel.classList.add('is-open');
        const first = panelFocusables()[0];
        if (first) first.focus();
    });
}
function closeSettings() {
    if (settingsPanel.hidden) return;
    const hadFocus = settingsPanel.contains(document.activeElement);
    settingsPanel.classList.remove('is-open');
    settingsBtn.setAttribute('aria-expanded', 'false');
    document.removeEventListener('keydown', trapPanelTab, true);
    if (hadFocus) settingsBtn.focus();
    setTimeout(() => { if (!settingsPanel.classList.contains('is-open')) settingsPanel.hidden = true; }, 180);
}
function toggleSettings() {
    if (settingsPanel.hidden) openSettings(); else closeSettings();
}

settingsBtn.addEventListener('click', (e) => { e.stopPropagation(); toggleSettings(); });
settingsClose.addEventListener('click', closeSettings);
document.addEventListener('click', (e) => {
    if (settingsPanel.hidden) return;
    if (settingsPanel.contains(e.target) || settingsBtn.contains(e.target)) return;
    closeSettings();
});
// Escape unwinds one layer at a time: the settings panel first (it's the thing
// on top), fullscreen only once the panel is closed.
document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    if (!settingsPanel.hidden) closeSettings();
    else if (feedZoom.scale) feedZoom.out();
    else if (fullscreen) setFullscreen(false);
});

function readSettingsForm() {
    const types = [];
    typeChecks.forEach(cb => { if (cb.checked) types.push(cb.dataset.typeToggle); });
    return {
        types: types.length ? types : [...DEFAULT_SETTINGS.types],
        includeTags: includeInput.value.trim(),
        excludeTags: excludeInput.value.trim(),
        hideAi: !!(hideAiCheck && hideAiCheck.checked),
    };
}
function settingsChanged(next) {
    const cur = settings;
    if (cur.includeTags !== next.includeTags) return true;
    if (cur.excludeTags !== next.excludeTags) return true;
    if (cur.hideAi !== next.hideAi) return true;
    if (cur.types.length !== next.types.length) return true;
    const a = [...cur.types].sort().join(',');
    const b = [...next.types].sort().join(',');
    return a !== b;
}
function applySettings() {
    const next = readSettingsForm();
    const changed = settingsChanged(next);
    Object.assign(settings, next);
    saveSettings();
    closeSettings();
    if (changed) resetFeed();
}
// Filters only — volume, mute and slideshow are deliberately left alone, and
// the button says "Reset filters" so that is no surprise.
function resetSettings() {
    settings.types = [...DEFAULT_SETTINGS.types];
    settings.includeTags = '';
    settings.excludeTags = '';
    settings.hideAi = false;
    saveSettings();
    syncSettingsPanel();
    resetFeed();
    closeSettings();
}

settingsApply.addEventListener('click', applySettings);
settingsReset.addEventListener('click', resetSettings);
[includeInput, excludeInput].forEach(inp => {
    inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); applySettings(); } });
});

// ── Fullscreen mode ───────────────────────────────────────────────────────
// Strips the page down to the media: nav, toolbar, badges, action rail,
// uploader/tags overlay and the video scrubber all go (CSS, via the body
// class), and the slides grow into the 58px the nav gave back. Deliberately
// not persisted in `settings` — landing on a chrome-less feed with no visible
// way out is a trap, so it always starts off.
const fullscreenBtn = document.getElementById('feedFullscreenBtn');
let fsHintEl = null;

function showFullscreenHint() {
    if (fsHintEl) fsHintEl.remove();
    fsHintEl = document.createElement('div');
    fsHintEl.className = 'feed-fs-hint';
    fsHintEl.textContent = 'Press ESC to exit fullscreen';
    document.body.appendChild(fsHintEl);
    const el = fsHintEl;
    setTimeout(() => { el.remove(); if (fsHintEl === el) fsHintEl = null; }, 2800);
}

function updateFullscreenUI() {
    fullscreenBtn.classList.toggle('is-active', fullscreen);
    fullscreenBtn.setAttribute('aria-pressed', fullscreen ? 'true' : 'false');
    fullscreenBtn.title = fullscreen ? 'Exit fullscreen (Esc)' : 'Fullscreen (f)';
    const expand = fullscreenBtn.querySelector('[data-fs-expand]');
    const collapse = fullscreenBtn.querySelector('[data-fs-collapse]');
    if (expand) expand.hidden = fullscreen;
    if (collapse) collapse.hidden = !fullscreen;
}

function setFullscreen(on) {
    const next = !!on;
    if (next === fullscreen) return;

    // Slides change height here — `--feed-nav-h` goes to 0 and they take
    // the nav's 58px — so the offset that sat on a snap boundary no longer
    // does. Remember which slide we're on and put it back. (The mobile URL
    // bar collapsing never gets here: `dvh` absorbs it in CSS.)
    const oldH = slides.length ? (slides[0].clientHeight || 1) : 0;
    const idx = oldH ? Math.round(container.scrollTop / oldH) : 0;

    fullscreen = next;
    document.body.classList.toggle('is-feed-fullscreen', fullscreen);
    if (fullscreen) closeSettings();
    updateFullscreenUI();

    if (oldH) {
        const newH = slides[0].clientHeight || oldH;
        container.scrollTo({ top: idx * newH, behavior: 'auto' });
    }
    if (fullscreen) showFullscreenHint();
    else if (fsHintEl) { fsHintEl.remove(); fsHintEl = null; }
}

fullscreenBtn.addEventListener('click', () => setFullscreen(!fullscreen));
updateFullscreenUI();

// ── Feed loading ──────────────────────────────────────────────────────────
// `/delete/<id>` answers scripted callers with JSON (MI.post sends
// X-Requested-With) and a 403 when the row isn't yours. A redirect would be
// followed by fetch, so `r.ok` would be true even for a refused delete and
// the slide would vanish from a feed the server had not changed.
function deleteSlide(id) {
    if (!confirm('Are you sure you want to delete this file?')) return;
    MI.post(`/delete/${id}`)
        .then(r => r.json().catch(() => ({ deleted: false })))
        .then(data => {
            if (!data.deleted) throw new Error('refused');
            removeSlide(container.querySelector(`.feed-slide[data-id="${id}"]`));
        })
        .catch(() => alert('Could not delete this file — it may not be yours.'));
}
// Delegated: the CSP forbids the inline handler this replaces, and slides
// are built dynamically, so document-level dispatch beats per-slide wiring.
MI.on('feed-delete', el => deleteSlide(+el.dataset.id));

// ── Media windowing ───────────────────────────────────────────────────────
// Only slides near the viewport keep their media loaded. When a slide scrolls
// far away we tear its <img>/<video> source down so the browser can release the
// decoded bitmap and (for video) the buffered data + decoder. Without this,
// every file you scroll past stays resident until the tab has to be refreshed,
// and long sessions slow down. The window
// (±1.5 viewports) is wide enough that media reloads — from the HTTP cache, so
// no re-download — well before a slide scrolls back into view.
function loadSlideMedia(slide) {
    const el = slide.querySelector('.feed-media');
    if (!el || el.src) return;
    el.src = el.dataset.src;
    if (el.tagName === 'VIDEO') el.load();
}
function unloadSlideMedia(slide) {
    const el = slide.querySelector('.feed-media');
    if (!el || !el.getAttribute('src')) return;
    if (el.tagName === 'VIDEO') {
        el.pause();
        el.removeAttribute('src');
        el.load();                       // frees buffered data + decoder
        const fill = slide.querySelector('[data-progress-fill]');
        const handle = slide.querySelector('[data-progress-handle]');
        const cur = slide.querySelector('[data-progress-current]');
        if (fill) fill.style.width = '0%';
        if (handle) handle.style.left = '0%';
        if (cur) cur.textContent = '0:00';
    } else {
        el.removeAttribute('src');       // drops the decoded bitmap
    }
}

const mediaWindowObs = new IntersectionObserver((entries) => {
    entries.forEach(e => {
        if (e.isIntersecting) loadSlideMedia(e.target);
        else unloadSlideMedia(e.target);
    });
}, { root: container, rootMargin: '150% 0px', threshold: 0 });

// `muted` is persisted, so a feed reopened with sound on calls play() with no
// user gesture behind it and the autoplay policy rejects. Swallowing that
// rejection would leave the video frozen and, during a slideshow, `ended`
// would never fire and the whole thing would stop.
// Falling back to muted playback keeps the feed moving, and the volume UI is
// told so it stops claiming there is sound.
function playVideo(v) {
    const p = v.play();
    if (!p || !p.catch) return;
    p.catch(() => {
        if (v.muted) return;            // rejected for some other reason
        setMuted(true);
        const retry = v.play();
        if (retry && retry.catch) retry.catch(() => {});
    });
}

const playObs = new IntersectionObserver((entries) => {
    entries.forEach(e => {
        const v = e.target.querySelector('video');
        if (!v) return;
        if (e.isIntersecting && e.intersectionRatio > 0.6) {
            loadSlideMedia(e.target);   // ensure source is attached before play
            v.muted = settings.muted;
            v.volume = settings.volume;
            playVideo(v);
        } else {
            v.pause();
        }
    });
}, { root: container, threshold: [0, 0.6, 1] });

const sentinelObs = new IntersectionObserver((entries) => {
    entries.forEach(e => { if (e.isIntersecting) loadMore(); });
}, { root: container, rootMargin: '600px 0px' });

// ── Slide lifecycle ───────────────────────────────────────────────────────
// An IntersectionObserver holds a *strong* reference to everything it
// observes, and all four here live as long as the page. Dropping slides with
// `innerHTML = ''` therefore leaked the entire previous feed on every filter
// change, and appending forever leaked it during one.

//: How many slides to keep behind the current one. Two viewports of history
//  is enough to scroll back through comfortably; past that the media is
//  unloaded anyway, so the node is an empty shell holding listeners.
const KEEP_BEHIND = 12;

function registerSlide(slide) {
    slides.push(slide);
    mediaWindowObs.observe(slide);
    playObs.observe(slide);
    slideshowObs.observe(slide);
}

function forgetSlide(slide) {
    mediaWindowObs.unobserve(slide);
    playObs.unobserve(slide);
    slideshowObs.unobserve(slide);
    unloadSlideMedia(slide);
    if (slideshowEndedVideo && slide.contains(slideshowEndedVideo)) clearSlideshowSchedule();
    if (currentSlide === slide) currentSlide = null;
}

function removeSlide(slide) {
    if (!slide) return;
    const i = slides.indexOf(slide);
    if (i !== -1) slides.splice(i, 1);
    forgetSlide(slide);
    slide.remove();
}

function clearAllSlides() {
    feedZoom.out();            // the zoomed element is about to be discarded
    slides.forEach(forgetSlide);
    slides = [];
    clearSlideshowSchedule();
    container.innerHTML = '';
}

// Trimming above the viewport would shift every slide below it up by the
// removed height, yanking the feed out from under the reader. So each removal
// is paid back by subtracting its height from scrollTop, leaving the current
// slide visually still. The container sets `scroll-behavior: smooth`, which
// would animate that correction into the very jump it exists to prevent —
// hence the temporary override.
function pruneBehind() {
    const cutoff = slideIndexFromScroll() - KEEP_BEHIND;
    if (cutoff <= 0) return;
    // Anchored on a slide that survives, and moved by exactly how far that
    // slide shifted. Summing offsetHeight instead rounds every removed slide
    // to a whole pixel, so on a scaled display the feed came to rest a few
    // pixels off the snap point (~4.9px for a prune of 12) and mandatory snap
    // dragged it back — the shudder you see just as the next slide arrives.
    const anchor = slides[cutoff];
    const before = anchor.getBoundingClientRect().top;
    for (const slide of slides.slice(0, cutoff)) {
        forgetSlide(slide);
        slide.remove();
    }
    slides = slides.slice(cutoff);
    const delta = anchor.getBoundingClientRect().top - before;
    if (!delta) return;
    const prev = container.style.scrollBehavior;
    container.style.scrollBehavior = 'auto';
    container.scrollTop += delta;
    container.style.scrollBehavior = prev;
}

// ── Slideshow (auto-advance) ──────────────────────────────────────────────
// Images/GIFs advance after `slideshowTimer` seconds; videos advance only once
// they have actually finished playing. Looping videos never fire the native
// `ended` event, so we flip `loop` off while the slideshow is running.
const slideshowBtn = document.getElementById('feedSlideshowBtn');
const slideshowToggle = document.getElementById('feedSlideshowToggle');
const slideshowTimerInput = document.getElementById('feedSlideshowTimer');

let currentSlide = null;
let slideshowTimeoutId = null;
let slideshowEndedVideo = null;
let slideshowEndedHandler = null;

function applyLoopToVideos() {
    const loop = !settings.slideshow;
    forEachVideo(v => { v.loop = loop; });
}

function getCurrentSlide() {
    return slides.length ? slides[slideIndexFromScroll()] : null;
}

function advanceSlide() {
    if (!slides.length) return;
    const next = slides[slideIndexFromScroll() + 1];
    if (next) {
        next.scrollIntoView({ behavior: scrollBehavior(), block: 'start' });
    } else {
        // Next slide not built yet — scroll to the bottom so the sentinel loads more.
        container.scrollTo({ top: container.scrollHeight, behavior: scrollBehavior() });
    }
}

function clearSlideshowSchedule() {
    if (slideshowTimeoutId) { clearTimeout(slideshowTimeoutId); slideshowTimeoutId = null; }
    if (slideshowEndedVideo && slideshowEndedHandler) {
        for (const ev of SLIDESHOW_VIDEO_EVENTS) {
            slideshowEndedVideo.removeEventListener(ev, slideshowEndedHandler);
        }
    }
    slideshowEndedVideo = null;
    slideshowEndedHandler = null;
}

//: A video can fail to reach `ended` in more ways than it can reach it: a
//  decode error, a 404, an empty source. Each of these means "this slide is
//  done" just as much as finishing does.
const SLIDESHOW_VIDEO_EVENTS = ['ended', 'error'];

//: Ceiling on how long one video may hold the slideshow. A video that stalls
//  mid-buffer fires neither `ended` nor `error`, so nothing above catches it.
const SLIDESHOW_VIDEO_MAX_MS = 10 * 60 * 1000;

function scheduleSlideshow(slide) {
    clearSlideshowSchedule();
    if (!settings.slideshow || !slide) return;
    // The guard belongs here, not only on `visibilitychange`: each advance
    // scrolls, which re-fires slideshowObs, which lands right back in this
    // function — so clearing the schedule on hide is undone a moment later
    // unless arming itself refuses.
    if (document.hidden) return;
    const advance = () => { if (settings.slideshow) advanceSlide(); };
    if (slide.dataset.type === 'video') {
        const v = slide.querySelector('video');
        // `error` is a one-shot that usually fires while the slide is loading,
        // i.e. before the slideshow is even started — so a listener alone
        // never hears it and the feed parks on a dead frame forever. Anything
        // already known to be unplayable falls back to the image timer.
        if (!v || v.error) {
            slideshowTimeoutId = setTimeout(advance, settings.slideshowTimer * 1000);
            return;
        }
        v.loop = false;
        slideshowEndedVideo = v;
        slideshowEndedHandler = advance;
        for (const ev of SLIDESHOW_VIDEO_EVENTS) v.addEventListener(ev, advance);
        // Backstop, not a timer: a healthy video always wins this race.
        slideshowTimeoutId = setTimeout(advance, SLIDESHOW_VIDEO_MAX_MS);
    } else {
        slideshowTimeoutId = setTimeout(advance, settings.slideshowTimer * 1000);
    }
}

// Slides are images and videos with no accessible name of their own, and the
// feed moves under the reader without a page change to announce it. This is
// the only thing telling a screen reader what is on screen now.
const liveRegion = document.createElement('div');
liveRegion.className = 'sr-only';
liveRegion.setAttribute('aria-live', 'polite');
liveRegion.setAttribute('aria-atomic', 'true');
document.body.appendChild(liveRegion);

function announceSlide(slide) {
    const media = slide.querySelector('.feed-media');
    const uploader = slide.querySelector('.feed-uploader span');
    const parts = [slide.dataset.type];
    if (uploader && uploader.textContent) parts.push(`by ${uploader.textContent}`);
    if (media && media.alt) parts.push(media.alt);
    liveRegion.textContent = parts.join(', ');
}

// Tracks whichever slide is currently snapped in view and (re)arms the timer/
// ended-listener for it. Fires once per slide as it crosses the 0.6 threshold,
// so a stationary slide isn't rescheduled repeatedly.
const slideshowObs = new IntersectionObserver((entries) => {
    entries.forEach(e => {
        if (e.isIntersecting && e.intersectionRatio > 0.6) {
            currentSlide = e.target;
            announceSlide(e.target);
            scheduleSlideshow(e.target);
        }
    });
}, { root: container, threshold: [0, 0.6, 1] });

function updateSlideshowUI() {
    const on = settings.slideshow;
    slideshowBtn.classList.toggle('is-active', on);
    slideshowBtn.setAttribute('aria-pressed', on ? 'true' : 'false');
    slideshowBtn.title = on ? 'Stop slideshow (s)' : 'Start slideshow (s)';
    const play = slideshowBtn.querySelector('[data-ss-play]');
    const pause = slideshowBtn.querySelector('[data-ss-pause]');
    if (play) play.hidden = on;
    if (pause) pause.hidden = !on;
    if (slideshowToggle) slideshowToggle.checked = on;
    if (slideshowTimerInput && document.activeElement !== slideshowTimerInput) {
        slideshowTimerInput.value = String(settings.slideshowTimer);
    }
}

function setSlideshow(on) {
    settings.slideshow = !!on;
    saveSettings();
    applyLoopToVideos();
    updateSlideshowUI();
    if (settings.slideshow) scheduleSlideshow(currentSlide || getCurrentSlide());
    else clearSlideshowSchedule();
}

function setSlideshowTimer(v) {
    let t = Math.round(Number(v));
    if (!isFinite(t) || t < 1) t = DEFAULT_SETTINGS.slideshowTimer;
    settings.slideshowTimer = Math.max(1, Math.min(120, t));
    saveSettings();
    // Re-arm the current image/gif timer with the new interval.
    if (settings.slideshow && currentSlide && currentSlide.dataset.type !== 'video') {
        scheduleSlideshow(currentSlide);
    }
}

// A backgrounded tab kept advancing on the timer, pulling media nobody was
// looking at. The setting is untouched — only the schedule is —
// so the slideshow resumes by itself on return.
document.addEventListener('visibilitychange', () => {
    if (document.hidden) clearSlideshowSchedule();
    else if (settings.slideshow) scheduleSlideshow(currentSlide || getCurrentSlide());
});

slideshowBtn.addEventListener('click', () => setSlideshow(!settings.slideshow));
if (slideshowToggle) slideshowToggle.addEventListener('change', (e) => setSlideshow(e.target.checked));
if (slideshowTimerInput) {
    slideshowTimerInput.addEventListener('change', (e) => { setSlideshowTimer(e.target.value); updateSlideshowUI(); });
}
updateSlideshowUI();

function buildFeedURL() {
    const url = new URL('/api/feed', location.origin);
    url.searchParams.set('seed', seed);
    url.searchParams.set('page', page);
    if (settings.types.length && settings.types.length < 3) {
        url.searchParams.set('types', settings.types.join(','));
    }
    if (settings.includeTags) url.searchParams.set('include_tags', settings.includeTags);
    if (settings.excludeTags) url.searchParams.set('exclude_tags', settings.excludeTags);
    // The same AI_TAGS list the gallery and the API filter on.
    if (settings.hideAi) url.searchParams.set('hide_ai', '1');
    return url;
}

function showEmptyState() {
    const empty = document.createElement('div');
    empty.className = 'feed-empty';
    empty.innerHTML = `
      <div class="empty-icon">&#128247;</div>
      <div>No media matches your filters</div>
      <div style="font-size:.78rem;">Try loosening tag filters or enabling more types.</div>
    `;
    container.appendChild(empty);
}

function resetFeed() {
    clearAllSlides();          // unobserves; innerHTML alone leaked them
    recentIds = [];
    seed = Math.floor(Math.random() * 2147483647);
    page = 0;
    loading = false;
    exhausted = false;
    firstLoad = true;
    const loader = document.createElement('div');
    loader.className = 'feed-loader-slide';
    loader.innerHTML = '<div class="feed-loader"></div><div>Loading feed…</div>';
    container.appendChild(loader);
    loadMore(loader);
}

// A feed page is only "loaded" if it parsed as JSON. `@login_required`
// answers an expired session with a redirect to the login form, which fetch
// follows and hands back as HTML. Feeding that straight to `r.json()` would
// make an expired session look like a generic network blip.
function fetchFeedPage() {
    return fetch(buildFeedURL(), { headers: { 'Accept': 'application/json' } })
        .then(r => {
            if (r.status === 401 || r.status === 403 || r.redirected) {
                const e = new Error('auth');
                e.authExpired = true;
                throw e;
            }
            if (!r.ok) throw new Error('HTTP ' + r.status);
            return r.json();
        });
}

// The sentinel doubles as the error surface. It has to: it is already
// intersecting when a load fails, and an IntersectionObserver fires on
// transitions, so nothing would ever re-trigger `loadMore` on its own.
function showLoadError(authExpired) {
    const sentinel = document.getElementById('feedSentinel') || (() => {
        const el = document.createElement('div');
        el.id = 'feedSentinel';
        el.className = 'feed-loader-slide';
        container.appendChild(el);
        return el;
    })();
    sentinelObs.unobserve(sentinel);
    sentinel.classList.add('is-error');
    sentinel.innerHTML = authExpired
        ? `<div>Your session expired.</div>
           <a class="feed-retry" href="/login">Log in again</a>`
        : `<div>Could not load more.</div>
           <button type="button" class="feed-retry" data-action="feed-retry">Retry</button>`;
}

function retryLoad() {
    const sentinel = document.getElementById('feedSentinel');
    if (sentinel) {
        sentinel.classList.remove('is-error');
        sentinel.innerHTML = '<div class="feed-loader"></div><div>Loading more…</div>';
    }
    loadMore();
}
MI.on('feed-retry', retryLoad);

function loadMore(loaderEl) {
    if (loading || exhausted) return;
    loading = true;
    page++;
    const requestedPage = page;
    fetchFeedPage()
        .then(data => {
            if (firstLoad) {
                firstLoad = false;
                if (loaderEl && loaderEl.parentNode) loaderEl.remove();
                else if (initialLoader && initialLoader.parentNode) initialLoader.remove();
                if (!data.media || data.media.length === 0) {
                    showEmptyState();
                    exhausted = true;
                    loading = false;
                    return;
                }
            }
            const oldSentinel = document.getElementById('feedSentinel');
            if (oldSentinel) oldSentinel.remove();

            let media = data.media || [];
            if (justReshuffled) {
                media = dropRecentlySeen(media, data.total);
                justReshuffled = false;
            }
            rememberIds(media.map(m => m.id));

            media.forEach((m, idx) => {
                const slide = buildSlide(m);
                container.appendChild(slide);
                registerSlide(slide);
                if (requestedPage === 1 && idx === 0) {
                    const hint = document.createElement('div');
                    hint.className = 'scroll-hint';
                    hint.textContent = 'Scroll ↓';
                    slide.appendChild(hint);
                    setTimeout(() => hint.remove(), 4000);
                }
            });

            pruneBehind();

            const sentinel = document.createElement('div');
            sentinel.id = 'feedSentinel';
            sentinel.className = 'feed-loader-slide';
            if (data.has_more) {
                sentinel.innerHTML = '<div class="feed-loader"></div><div>Loading more…</div>';
            } else {
                seed = Math.floor(Math.random() * 2147483647);
                page = 0;
                justReshuffled = true;
                sentinel.innerHTML = '<div class="feed-loader"></div><div>Reshuffling…</div>';
            }
            container.appendChild(sentinel);
            sentinelObs.observe(sentinel);

            loading = false;
        })
        .catch((err) => {
            console.error('feed load failed', err);
            // Give the page back so a retry re-requests it instead of
            // silently skipping it.
            if (page === requestedPage) page--;
            loading = false;
            if (firstLoad) {
                firstLoad = false;
                if (loaderEl && loaderEl.parentNode) loaderEl.remove();
                else if (initialLoader && initialLoader.parentNode) initialLoader.remove();
            }
            showLoadError(!!err.authExpired);
        });
}

// No resize handler: the stylesheet sizes slides in `dvh` against
// `--feed-nav-h`, so the viewport changing is not this file's problem.

syncSettingsPanel();
loadMore();

document.addEventListener('keydown', e => {
    if (e.target && (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA')) return;
    if (!settingsPanel.hidden) return;
    // `e.key` is 'f' for Ctrl+F too, so without this the browser's Find bar
    // toggled fullscreen and Ctrl/Cmd+S started the slideshow.
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (!slides.length) return;
    const idx = slideIndexFromScroll();
    let target = idx;
    if (e.key === 'ArrowDown' || e.key === 'j') target = Math.min(idx + 1, slides.length - 1);
    else if (e.key === 'ArrowUp' || e.key === 'k') target = Math.max(idx - 1, 0);
    else if (e.key === 'm') { toggleMute(); return; }
    else if (e.key === 's') { setSlideshow(!settings.slideshow); return; }
    else if (e.key === 'f') { setFullscreen(!fullscreen); return; }
    else if (e.key === 'ArrowRight') { e.preventDefault(); seekSlideVideo(slides[idx], SEEK_STEP); return; }
    else if (e.key === 'ArrowLeft')  { e.preventDefault(); seekSlideVideo(slides[idx], -SEEK_STEP); return; }
    else return;
    e.preventDefault();
    slides[target].scrollIntoView({ behavior: scrollBehavior(), block: 'start' });
});

// ── Zoom ─────────────────────────────────────────────────────────────────
// The same gesture as the gallery lightbox (MI.zoom in common.js), with its
// own geometry, because the geometry here is nothing like the lightbox's. `.feed-media` is an inset:0 layer with object-fit:contain, so
// the element box is the whole slide and the picture sits letterboxed inside
// it. Two things follow: `.feed-slide` is already overflow:hidden and crops
// the scaled image with no clip box of its own, and the cursor has to be
// clamped to the *picture* rather than the element, or pointing at a
// letterbox bar would zoom into black and leave the image's own edges
// unreachable.
let feedZoomScrollTop = 0;        // where the feed was when the zoom started

/* Where object-fit: contain actually paints the picture, and the element box
 * it is painted in. Both are measured off the slide, because once zoomed the
 * element's own rect is the scaled one. */
function feedZoomRects(img) {
    const box = img.parentElement.getBoundingClientRect();
    const nw = img.naturalWidth, nh = img.naturalHeight;
    if (!nw || !nh) return { content: box, box };      // not decoded yet
    const s = Math.min(box.width / nw, box.height / nh);
    const w = nw * s, h = nh * s;
    return {
        content: {
            left: box.left + (box.width - w) / 2,
            top:  box.top  + (box.height - h) / 2,
            width: w, height: h,
        },
        box,
    };
}

function setFeedZoomOrigin(ev, img) {
    const { content, box } = feedZoomRects(img);
    const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
    const x = clamp(ev.clientX, content.left, content.left + content.width);
    const y = clamp(ev.clientY, content.top,  content.top  + content.height);
    img.style.transformOrigin =
        ((x - box.left) / box.width  * 100) + '% ' +
        ((y - box.top)  / box.height * 100) + '%';
}

/* feedZoom.out() is safe to call unconditionally. */
const feedZoom = MI.zoom({
    origin: setFeedZoomOrigin,
    enter(img) {
        feedZoomScrollTop = container.scrollTop;
        img.classList.add('is-zoomed');
        // Hold the auto-advance without touching the saved setting: a slide
        // that scrolls away mid-inspection defeats the point, but the user
        // never asked for the slideshow to be switched off.
        clearSlideshowSchedule();
    },
    leave(img, was) {
        if (img) img.classList.remove('is-zoomed');
        // Hand the slideshow back the schedule zooming in took away.
        if (was && settings.slideshow) scheduleSlideshow(currentSlide || getCurrentSlide());
    },
});

MI.on('feed-zoom', (el, ev) => feedZoom.scale ? feedZoom.out() : feedZoom.in(ev, el));

// Bound to the container, which outlives every slide in it. Reading the
// pointer off the event rather than its target means the badges and buttons
// layered over the image don't interrupt the pan.
container.addEventListener('pointermove', (e) => feedZoom.move(e));

container.addEventListener('wheel', (e) => {
    if (!feedZoom.scale) return;
    // A rebuild (settings applied, feed reloaded) can take the element out
    // from under the zoom; without this the feed would refuse to scroll.
    if (!feedZoom.el.isConnected) { feedZoom.out(); return; }
    e.preventDefault();                    // the wheel is the zoom's while zoomed
    feedZoom.wheel(e);
}, { passive: false });

// A real scroll — a keyboard nav, a swipe, a slideshow advance — carries the
// slide away, so the zoom goes with it. Measured against where the scroll was
// when the zoom started, because a snap settling into place emits a burst of
// scroll events worth a few pixels, and exiting on those made the zoom
// impossible to hold while the feed was still coming to rest.
const FEED_ZOOM_SCROLL_SLACK = 40;   // px; a navigation is a whole slide
container.addEventListener('scroll', () => {
    if (!feedZoom.scale) return;
    if (Math.abs(container.scrollTop - feedZoomScrollTop) > FEED_ZOOM_SCROLL_SLACK) feedZoom.out();
}, { passive: true });
