"""One status message per Bunkr album, edited as its files settle.

An album is one link that becomes many files, and each file used to post
its own status line ("✅ `IMG_1.jpg` indexed for **alice** — tags: …"),
twenty lines for one link. An AlbumStatus collects them into one message
instead: the album, who its files were indexed for and under which tags,
what was left out of it, then one short line per file, added by editing
the message as each file settles.

Discord caps a message at 2000 characters. When the next line would not
fit, the message is left as it is and the album carries on in a new one
("(continued)"), so a long album is a few messages rather than one per
file.

Edits are throttled to one per `interval` seconds, because Discord
rate-limits them and a run of small images settles faster than that. A
line that arrives inside the window is shown by a delayed edit once the
window ends, so it never waits for the next file — which may be a video
that takes minutes. finish() writes whatever is left and drops the
progress line.

Nothing here knows about Discord itself: the bot hands in how to post a
message and how to edit one (IndexBot._report / _edit_report), which
keeps the bot-logs channel vs DM decision in one place.
"""
import asyncio
import contextlib
import time

#: Seconds between two edits of one album's message.
EDIT_INTERVAL = 2.0

#: The most one file's line may take up, so a long duplicate note (it
#: links the item and its tags) can never fill a message on its own.
MAX_LINE = 400


class AlbumStatus:
    """The status message(s) for one album's files.

    `post(text)` posts a new status message and returns it (None when it
    couldn't); `edit(sent, text)` replaces a posted message's text and
    returns whether that worked; `room` is how many characters one
    message may hold.
    """

    def __init__(self, album, post, edit, room, interval=EDIT_INTERVAL,
                 clock=time.monotonic):
        self.album = album
        self._post, self._edit, self._room = post, edit, room
        self._interval, self._clock = interval, clock
        self._summary = None
        self._pages = [[]]          # file lines, one list per message
        self._sent = None           # the message showing the last page
        self._done = 0
        self._dirty = False         # the last page has news not yet shown
        self._last = None           # when the last page was last written
        self._timer = None          # the delayed edit, while one is due
        self._lock = asyncio.Lock()

    def indexed_as(self, summary):
        """Who the files are indexed for and under which tags ("Indexed
        for **alice** — tags: `cats`"). The same for every file of one
        message, so it is said once, at the top; the first one stays."""
        if self._summary is None:
            self._summary = summary

    async def add(self, line):
        """One file has settled: its line goes on the list."""
        if len(line) > MAX_LINE:
            line = line[:MAX_LINE - 1] + '…'
        self._done += 1
        page = self._pages[-1]
        if page and len(self._render(len(self._pages) - 1, page + [line],
                                     final=False)) > self._room:
            # Full: the message keeps what it has, and the album goes on
            # in a new one.
            await self._write(final=True)
            self._pages.append([])
            self._sent = None
        self._pages[-1].append(line)
        self._dirty = True
        await self._flush()

    async def finish(self):
        """Every file has settled: show the rest, without the progress
        line."""
        if self._timer is not None:
            self._timer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._timer
            self._timer = None
        if self._done:
            await self._write(final=True)

    # ── Rendering ──────────────────────────────────────────────────────
    def _render(self, index, lines, final):
        title = f" “{self.album.title}”" if self.album.title else ""
        head = [f"📚 <{self.album.url}>{title}"
                + (" (continued)" if index else "")]
        if index == 0:
            if self._summary:
                head.append(self._summary)
            note = self.album.note()
            if note:
                head.append(note[:1].upper() + note[1:])
        foot = [] if final else \
            [f"⬇️ {self._done}/{len(self.album.files)}…"]
        return '\n'.join(head + lines + foot)

    # ── Writing ────────────────────────────────────────────────────────
    async def _flush(self):
        """Write the last page now, or schedule the write for when the
        edit window ends."""
        if self._sent is not None and self._last is not None:
            wait = self._interval - (self._clock() - self._last)
            if wait > 0:
                if self._timer is None:
                    self._timer = asyncio.ensure_future(self._later(wait))
                return
        await self._write(final=False)

    async def _later(self, wait):
        await asyncio.sleep(wait)
        self._timer = None
        await self._write(final=False)

    async def _write(self, final):
        """Post or edit the last page. Under a lock: the delayed edit and
        the next file's line must not both decide to post a new message."""
        async with self._lock:
            if not self._dirty and not final:
                return
            text = self._render(len(self._pages) - 1, self._pages[-1], final)
            if self._sent is None or not await self._edit(self._sent, text):
                self._sent = await self._post(text)
            self._last = self._clock()
            self._dirty = False
