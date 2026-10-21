document.addEventListener('DOMContentLoaded', () => {
    // ── Telegram account linking ──────────────────────────────────────────
    const tgInput    = document.getElementById('tgIdentity');
    const tgSaveBtn  = document.getElementById('tgSaveBtn');
    const tgUnlinkBtn= document.getElementById('tgUnlinkBtn');
    const tgStatus   = document.getElementById('tgStatus');

    // The server owns the verdict on what a valid identity is (and on
    // whether someone else already claimed it), so this only renders the
    // answer — it never decides one locally.
    function tgSubmit(identity) {
        const body = new FormData();
        body.append('identity', identity);
        tgSaveBtn.disabled = true;
        return MI.post('/api/telegram/link', body)
            .then(r => r.json().then(data => ({ ok: r.ok, data })))
            .then(({ ok, data }) => {
                if (!ok) {
                    tgStatus.textContent = data.error || 'Could not save that.';
                    return;
                }
                if (data.status === 'unlinked') {
                    tgStatus.textContent = 'Not linked';
                    tgInput.value = '';
                    tgUnlinkBtn.hidden = true;
                } else if (data.telegram_id) {
                    tgStatus.textContent = 'Linked to ID ' + data.telegram_id;
                    tgUnlinkBtn.hidden = false;
                } else {
                    tgStatus.textContent = 'Linked to @' + data.telegram_username
                                         + ' (unverified)';
                    tgUnlinkBtn.hidden = false;
                }
            })
            .finally(() => { tgSaveBtn.disabled = false; });
    }

    tgSaveBtn.addEventListener('click', () => tgSubmit(tgInput.value.trim()));
    tgInput.addEventListener('keydown', e => {
        if (e.key === 'Enter') { e.preventDefault(); tgSaveBtn.click(); }
    });
    tgUnlinkBtn.addEventListener('click', () => {
        if (!confirm('Unlink Telegram? The bot will stop accepting your messages.')) return;
        tgSubmit('');
    });

    // ── Discord account linking ───────────────────────────────────────────
    // A link is a round trip through Discord: this page gets a one-time
    // code, the user DMs it to the bot, and the bot redeems it with the
    // sender's id. While a code is on screen the page polls for that.
    const dcStatus    = document.getElementById('dcStatus');
    const dcCodeBtn   = document.getElementById('dcCodeBtn');
    const dcUnlinkBtn = document.getElementById('dcUnlinkBtn');
    const dcCodeBox   = document.getElementById('dcCodeBox');
    const dcCode      = document.getElementById('dcCode');
    const dcCodeHint  = document.getElementById('dcCodeHint');
    const dcCopyBtn   = document.getElementById('dcCopyBtn');
    let dcPoll = null;

    function dcStopPolling() {
        if (dcPoll) { clearInterval(dcPoll); dcPoll = null; }
    }

    // Ids stay strings end to end: a 19-digit snowflake does not survive
    // being a JavaScript number.
    function dcShowLinked(discordId) {
        dcStopPolling();
        dcCodeBox.hidden = true;
        dcStatus.textContent = discordId ? 'Linked to ID ' + discordId : 'Not linked';
        dcUnlinkBtn.hidden = !discordId;
        dcCodeBtn.textContent = discordId ? 'Link a different account' : 'Get link code';
    }

    function dcWatch(expiresAt) {
        dcStopPolling();
        dcPoll = setInterval(() => {
            fetch('/api/discord/status', { headers: { 'X-Requested-With': 'XMLHttpRequest' } })
                .then(r => r.ok ? r.json() : null)
                .then(data => {
                    if (!data || data.pending) return;
                    if (Date.now() < expiresAt) {
                        dcShowLinked(data.discord_id);       // redeemed
                    } else {
                        dcStopPolling();
                        dcCodeBox.hidden = true;
                        dcStatus.textContent = 'That code expired — get a new one.';
                    }
                })
                .catch(() => {});                           // next tick retries
        }, 3000);
    }

    dcCodeBtn.addEventListener('click', () => {
        dcCodeBtn.disabled = true;
        MI.post('/api/discord/link/code', new FormData())
            .then(r => r.json().then(data => ({ ok: r.ok, data })))
            .then(({ ok, data }) => {
                if (!ok) {
                    dcStatus.textContent = data.error || 'Could not get a code.';
                    return;
                }
                dcCode.textContent = 'link ' + data.code;
                dcCodeHint.textContent = 'Works once, for '
                    + Math.round(data.expires_in / 60) + ' minutes. '
                    + 'This page updates when the bot has linked you.';
                dcCodeBox.hidden = false;
                dcWatch(Date.now() + data.expires_in * 1000);
            })
            .catch(() => { dcStatus.textContent = 'Could not get a code.'; })
            .finally(() => { dcCodeBtn.disabled = false; });
    });

    dcCopyBtn.addEventListener('click', () => {
        navigator.clipboard.writeText(dcCode.textContent).then(() => {
            dcCopyBtn.textContent = 'Copied';
            setTimeout(() => { dcCopyBtn.textContent = 'Copy'; }, 1500);
        }, () => {});
    });

    dcUnlinkBtn.addEventListener('click', () => {
        if (!confirm('Unlink Discord? Your posts will be credited to the bot, '
                     + 'and the bot will stop accepting your DMs.')) return;
        MI.post('/api/discord/unlink', new FormData())
            .then(r => { if (r.ok) dcShowLinked(null); });
    });
});
