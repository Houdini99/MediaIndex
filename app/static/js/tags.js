/* Server-side values arrive on <body data-*> rather than through an inline
 * <script> block, which the CSP doesn't allow. */
const MEDIA_INDEX = {
    totalTags: Number(document.body.dataset.totalTags || 0),
    indexUrl: document.body.dataset.indexUrl || '/',
    renameTagUrl: document.body.dataset.renameTagUrl || '',
    removeTagUrl: document.body.dataset.removeTagUrl || '',
};

  const totalCount = MEDIA_INDEX.totalTags;

  function filterTags() {
      const q = document.getElementById('tagSearch').value.toLowerCase();
      let count = 0;
      document.querySelectorAll('.tag-item').forEach(el => {
          const show = el.dataset.tag.includes(q);
          el.style.display = show ? '' : 'none';
          if (show) count++;
      });
      const countEl = document.getElementById('tagCount');
      if (countEl) countEl.textContent = count + (q ? ' of ' + totalCount : '') + ' Tags';
  }

  // Replaces the inline input handler — this script loads at the end of
  // <body>, so the input already exists.
  const tagSearch = document.getElementById('tagSearch');
  if (tagSearch) tagSearch.addEventListener('input', filterTags);

  document.querySelectorAll('.tag-item').forEach(el => {
      el.addEventListener('click', function () {
          const tag = this.querySelector('.tag-name').textContent.trim();
          window.location.href = MEDIA_INDEX.indexUrl + '?search=' + encodeURIComponent(tag);
      });
  });

  // ── Admin-only inline tag actions (buttons only rendered for admins) ──
  function postTagAction(url, fields) {
      const fd = new FormData();
      for (const k in fields) fd.append(k, fields[k]);
      MI.post(url, fd)
          .then(() => location.reload())
          .catch(() => alert('The tag action failed. Please try again.'));
  }

  document.querySelectorAll('.tag-act').forEach(btn => {
      btn.addEventListener('click', function (e) {
          e.stopPropagation();  // don't trigger the row's search navigation
          const tag = this.dataset.tag;
          if (this.classList.contains('tag-act-rename')) {
              const renamed = prompt('Rename tag "' + tag + '" to:', tag);
              if (!renamed || !renamed.trim() || renamed.trim() === tag) return;
              postTagAction(MEDIA_INDEX.renameTagUrl, { old: tag, new: renamed.trim() });
          } else {
              if (!confirm('Remove tag "' + tag + '" from all media? This cannot be undone.')) return;
              postTagAction(MEDIA_INDEX.removeTagUrl, { tag: tag });
          }
      });
  });
