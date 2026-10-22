"""Entrypoint: Discord gateway client wiring both directions.

Channel A (NOTIFY_CHANNEL_ID): announcements from the Redis event queue.
Channel B (INGEST_CHANNEL_ID): media is downloaded and uploaded to the
index — attachments (incl. forwarded ones), direct media links in the
text, media behind embeds (GIF picker, unfurled image links), and
platform links (YouTube, TikTok, …) resolved to files by yt-dlp, logged
in with the site login the index stores for the link's site (see
bots_common/site_logins.py). The
result is signalled by reacting to the message (✅ ok, ♻️ duplicate,
🚫 unsupported type, ❌ error, ⏳ auth backoff; ⬇️ marks a yt-dlp
download in progress, 🔁 that a transient failure was queued for retry).

DMs: a member holding the ingest role may DM the bot instead. Everything
above applies, except that the media is indexed *privately* for them and
the status lines that would go to the bot-logs channel are sent back into
the DM — a shared channel never learns what someone filed privately.

Who owns an ingest is the index's answer: a user links their Discord
account by DMing the bot the `link CODE` line from the index's /settings
page, which the bot redeems with the sender's id.
"""
import asyncio
import contextlib
import logging
import os
import tempfile
import uuid

import aiohttp
import discord

from bots_common import resolver, site_logins, sniff
from bots_common.download import TooLarge
from bots_common.index_client import AuthError, IndexUnavailable

from . import config, download, notifier, parsing, retry
from .index_client import IndexClient

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s %(levelname)s %(name)s: %(message)s')

# Privileged intent: also enable "Message Content Intent" for the bot in
# the Discord Developer Portal, or attachments/content arrive empty.
intents = discord.Intents.default()
intents.message_content = True

# Reaction vocabulary. Outcomes: OK, DUPLICATE, UNSUPPORTED, ERROR,
# BACKOFF. Progress markers (posted then removed): DOWNLOADING, QUEUED.
OK = '✅'
DUPLICATE = '♻️'
UNSUPPORTED = '🚫'
ERROR = '❌'
BACKOFF = '⏳'
DOWNLOADING = '⬇️'
QUEUED = '🔁'
TAGGED = '🏷️'


class _Outcome:
    """One source's result: the reaction to post, whether trying again
    later could plausibly succeed, and a short reason for the retry log."""
    __slots__ = ('emoji', 'retryable', 'reason')

    def __init__(self, emoji, retryable=False, reason=None):
        self.emoji = emoji
        self.retryable = retryable
        self.reason = reason


class _UrlSource:
    """Adapter so media behind links/embeds flows through _ingest exactly
    like an attachment (same .url/.filename/.size surface)."""
    __slots__ = ('url', 'filename', 'size')

    def __init__(self, url, filename):
        self.url = url
        self.filename = filename
        self.size = 0   # unknown until downloaded


class _LocalSource:
    """Adapter for a file yt-dlp already wrote to disk — flows through
    _ingest like an attachment, minus the download step."""
    __slots__ = ('path', 'url', 'filename', 'size')

    def __init__(self, path, filename, url):
        self.path = path
        self.filename = filename
        self.url = url
        self.size = os.path.getsize(path)


class _PendingForward:
    """A forwarded ingest waiting out its tag grace window: forwards can't
    carry a comment, so the author's tag list arrives as a separate text
    message right after (see IndexBot._ingest_forward)."""
    def __init__(self):
        self.tags = []
        self.event = asyncio.Event()


class IndexBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.index = IndexClient(config.INDEX_INTERNAL_URL,
                                 config.INDEX_BOT_USERNAME,
                                 config.INDEX_BOT_APP_PASSWORD,
                                 auth_cooldown=config.AUTH_RETRY_SECONDS,
                                 lookup_ttl=config.LOOKUP_TTL)
        # Strong references: asyncio only weakly references scheduled
        # tasks, so without these the GC destroys the loops mid-await.
        self._notify_task = None
        self._retry_task = None
        # (channel_id, author_id) -> [_PendingForward, …]. Keyed by channel
        # too, so a tag list typed in a DM never tags the same person's
        # channel forwards, or the other way round.
        self._pending_forwards = {}
        self._forward_tasks = set()   # strong refs, same asyncio gotcha
        self._downloader = download.CdnDownloader()
        self._log_channel = None      # resolved bot-logs channel (cache)
        self._announced = False       # startup line posted once per process
        self._retry = retry.RetryQueue(
            config.REDIS_URL, key=config.RETRY_KEY,
            max_attempts=config.INGEST_RETRY_ATTEMPTS,
            base_delay=config.INGEST_RETRY_DELAY,
            max_delay=config.INGEST_RETRY_MAX_DELAY,
        ) if config.INGEST_RETRY else None

    async def setup_hook(self):
        self._notify_task = self.loop.create_task(notifier.notify_loop(self))
        self._notify_task.add_done_callback(self._loop_task_done('notifier'))
        if self._retry is not None:
            self._retry_task = self.loop.create_task(self._retry_loop())
            self._retry_task.add_done_callback(self._loop_task_done('retry'))

    @staticmethod
    def _loop_task_done(name):
        def done(task):
            if task.cancelled():
                return
            logging.error("%s: loop exited unexpectedly", name,
                          exc_info=task.exception())
        return done

    async def on_ready(self):
        logging.info("gateway: logged in as %s", self.user)
        if not self._announced:
            self._announced = True
            if config.YTDLP_ENABLED and not resolver.available():
                logging.warning("resolver: yt-dlp is not installed — "
                                "platform links will be skipped")
            if not config.INGEST_ROLE:
                logging.warning(
                    "ingest: DISCORD_INGEST_ROLE is not set — ANY member who "
                    "can post in the ingest channel can feed arbitrary media "
                    "to yt-dlp and to ffmpeg in the web container")
            imp = "" if resolver.impersonation_available() \
                else ", no impersonation"
            ytdlp = (f"on (yt-dlp {resolver.version()}{imp}, "
                     f"≤{config.YTDLP_QUALITY}p)"
                     if self._resolver_enabled() else "off")
            logins = await self._logins_status()
            tokens = await self._pot_status()
            queued = await self._retry.pending() if self._retry else 0
            retries = (f"on, {queued} queued" if self._retry else "off")
            gate = (f"@{config.INGEST_ROLE} only" if config.INGEST_ROLE
                    else "⚠️ OPEN TO EVERYONE")
            dms = ("private" if self._dm_enabled()
                   else "off (needs DISCORD_INGEST_ROLE)" if config.DM_INGEST
                   else "off")
            await self._log(
                f"🤖 online as **{self.user}** — watching "
                f"<#{config.INGEST_CHANNEL_ID}> for media "
                f"(ingest: {gate}, DMs: {dms}, "
                f"forward tag wait: {config.FORWARD_TAG_WAIT:g}s, "
                f"link resolver: {ytdlp}, "
                f"site logins: {logins}, "
                f"PO tokens: {tokens}, "
                f"retries: {retries})")

    async def _logins_status(self):
        """Which sites the index holds a login for, for the startup line.
        Names only — the cookies never leave the index for this. It can
        never raise out of on_ready."""
        if not self._resolver_enabled():
            return "off"
        try:
            logins = await self.index.site_logins()
        except Exception as e:
            reason = str(e) or type(e).__name__
            logging.warning("site-logins: startup lookup failed — %s", reason)
            return f"⚠️ unknown — {reason}"
        if not logins:
            return "none stored"
        names = []
        for entry in logins:
            name = entry.get('name') or entry.get('platform') or '?'
            if not entry.get('readable'):
                name += " (⚠️ unreadable)"
            elif not entry.get('logged_in') and entry.get('platform') != 'other':
                name += " (⚠️ no login cookie)"
            names.append(name)
        return ", ".join(names)

    async def _pot_status(self):
        """Whether the PO-token server answers, for the startup line — so a
        missing sidecar shows at boot, not as a YouTube bot check later."""
        if not (self._resolver_enabled() and config.YTDLP_POT_PROVIDER):
            return "off"
        try:
            async with self._downloader.session().get(
                    f"{config.YTDLP_POT_PROVIDER}/ping",
                    timeout=aiohttp.ClientTimeout(total=10)) as resp:
                resp.raise_for_status()
                data = await resp.json(content_type=None)
        except Exception as e:
            logging.warning("resolver: PO-token server %s unreachable — %s",
                            config.YTDLP_POT_PROVIDER, type(e).__name__)
            return f"⚠️ unreachable — {type(e).__name__}"
        version = data.get('version') if isinstance(data, dict) else None
        return f"on (bgutil {version or '?'})"

    @staticmethod
    def _resolver_enabled():
        return config.YTDLP_ENABLED and resolver.available()

    @staticmethod
    def _dm_enabled():
        return config.DM_INGEST and bool(config.INGEST_ROLE)

    @staticmethod
    def _is_dm(message):
        return isinstance(message.channel, discord.DMChannel)

    async def _report(self, message, text):
        """Status line about one message. A channel ingest's goes to the
        bot-logs channel with a link back; a DM's goes back into the DM,
        because its filenames, tags and links describe private media."""
        if not self._is_dm(message):
            await self._log(f"{text}\n{message.jump_url}")
            return
        try:
            await message.channel.send(
                text[:2000], suppress_embeds=True,
                reference=message.to_reference(fail_if_not_exists=False))
        except discord.HTTPException:
            logging.warning("dm: could not reply to message %s", message.id,
                            exc_info=True)

    async def _log(self, text):
        """Best-effort status line to the bot-logs channel; never raises.
        The channel is optional (DISCORD_LOG_CHANNEL_ID unset = disabled)."""
        if not config.LOG_CHANNEL_ID:
            return
        try:
            channel = self._log_channel or self.get_channel(config.LOG_CHANNEL_ID)
            if channel is None:
                channel = await self.fetch_channel(config.LOG_CHANNEL_ID)
            self._log_channel = channel
            await channel.send(text[:2000], suppress_embeds=True)
        except Exception:
            logging.warning("botlog: could not post to channel %s",
                            config.LOG_CHANNEL_ID, exc_info=True)

    async def on_message(self, message):
        # Loop guard: never react to our own messages, even if the notify
        # and ingest channels are misconfigured to be the same one.
        if self.user and message.author.id == self.user.id:
            return
        if self._is_dm(message):
            code = parsing.parse_link_command(message.content)
            if code is not None:
                # Ahead of the DM gate on purpose: a link also decides who
                # owns channel posts, so it must work with DM ingest off,
                # and it runs nothing but one call to the index.
                await self._link_account(message, code)
                return
            await self._on_dm(message)
            return
        if message.channel.id != config.INGEST_CHANNEL_ID:
            return
        # Ingest gate. Deliberately the only place this is checked: the
        # retry loop calls _handle_message directly, but the retry queue is
        # only ever filled from inside _handle_message, so everything in it
        # already passed here. Re-checking on redrive would just add a
        # stale-member-state failure mode.
        if not config.author_is_verified(message.author):
            logging.info(
                "ingest: ignoring message %s — author %s lacks the %r role",
                message.id, message.author.id, config.INGEST_ROLE)
            return
        await self._handle_message(message)

    async def _on_dm(self, message):
        """The DM ingest gate — the same role as the channel's, checked
        against the ingest channel's server. Fails closed: with the role
        gate off, an unreachable member lookup, or no role, the DM is
        ignored."""
        if not self._dm_enabled():
            return
        member = await self._home_member(message.author.id)
        if member is None or not config.author_is_verified(member):
            logging.info(
                "ingest: ignoring DM %s — author %s lacks the %r role",
                message.id, message.author.id, config.INGEST_ROLE)
            return
        await self._handle_message(message)

    async def _home_member(self, user_id):
        """The DM author as a member of the ingest channel's server, or None.

        Always fetched rather than read from the member cache: without the
        privileged members intent the cache neither holds most members nor
        hears about role changes, so a cached Member could still carry a
        role that was taken away.
        """
        try:
            channel = self.get_channel(config.INGEST_CHANNEL_ID) \
                or await self.fetch_channel(config.INGEST_CHANNEL_ID)
            return await channel.guild.fetch_member(user_id)
        except discord.NotFound:
            return None      # not (or no longer) on the server
        except discord.HTTPException:
            logging.warning("dm: member lookup for %s failed", user_id,
                            exc_info=True)
            return None

    async def _dm_sender_linked(self, message, attempts, tags):
        """Whether a DM may go ahead: its sender linked an index account,
        so there is someone to store it privately for. Otherwise says why
        in the DM — and settles the message as retryable when the index
        simply could not be asked."""
        try:
            if await self.index.lookup_discord_user(message.author.id):
                return True
        except AuthError:
            outcome = await self._auth_failed(message, 'dm')
        except IndexUnavailable as e:
            outcome = _Outcome(ERROR, True, f"couldn't look up your account "
                                            f"— {e}")
        else:
            await self._tell_unlinked(message)
            return False
        await self._react(message, outcome.emoji)
        await self._report(message, f"{outcome.emoji} {outcome.reason}")
        await self._settle(message, [outcome], attempts, tags)
        return False

    async def _tell_unlinked(self, message):
        await self._report(
            message,
            f"🔒 This Discord account isn't linked to an index account, so "
            f"nothing was saved.\n\n{self._how_to_link()}")

    @staticmethod
    def _how_to_link():
        return (f"To link it, open {config.INDEX_PUBLIC_URL}/settings, press "
                f"**Get link code** under Connect Discord, and send me the "
                f"`link …` message it shows.")

    async def _link_account(self, message, code):
        """`link CODE` in a DM: redeem the code /settings issued, linking
        this Discord account to the index account that asked for it.
        Discord vouches for the sender's id, the code for the index account,
        so neither side can be claimed by typing someone else's."""
        if not code:
            await self._report(message, self._how_to_link())
            return
        try:
            username = await self.index.verify_discord_link(message.author.id,
                                                            code)
        except (AuthError, IndexUnavailable) as e:
            logging.warning("link: could not redeem a code for %s — %s",
                            message.author.id, e)
            text = ("⏳ I can't reach the index right now. Send the same "
                    "code again in a few minutes.")
        else:
            if username is None:
                text = (f"❌ That code is wrong, already used, or expired. Get "
                        f"a new one at {config.INDEX_PUBLIC_URL}/settings.")
            else:
                logging.info("link: discord id %s linked to %s",
                             message.author.id, username)
                text = (f"✅ Linked to index account **{username}**. What you "
                        f"post in the ingest channel is now credited to you")
                text += (", and media you DM me is saved privately (if you "
                         "have the ingest role)." if self._dm_enabled()
                         else ".")
        await self._report(message, text)

    async def _handle_message(self, message, attempts=0, tags=None):
        """Ingest everything ingestible in one message.

        `attempts` is how many attempts have already been made (0 for a
        live message, ≥1 when the retry queue redrives it). `tags`
        overrides the message's own tag list — the retry path passes the
        tags that were in effect the first time, since a forward's tags
        arrived as a follow-up message that will not be sent again.
        """
        # Checked here rather than in _on_dm so a redrive re-checks it too,
        # and before anything is downloaded.
        if self._is_dm(message) \
                and not await self._dm_sender_linked(message, attempts, tags):
            return
        if attempts:
            # A previous attempt's verdict no longer applies.
            for emoji in (ERROR, BACKOFF):
                await self._unreact(message, emoji)
        sources, tag_text, urls = await self._gather_sources(message)
        user_tags = list(tags) if tags is not None \
            else parsing.parse_message_tags(tag_text)
        if not sources:
            if urls:
                # Links, but no directly ingestible media behind them —
                # a platform page (YouTube, TikTok, …) may still resolve
                # through yt-dlp; otherwise it's an article/dead link.
                handled, outcomes = await self._resolve_and_ingest(
                    message, urls, user_tags)
                if not handled:
                    await self._react(message, UNSUPPORTED)
                    return
                await self._settle(message, outcomes, attempts, user_tags)
                return
            if tags is None:
                await self._apply_pending_tags(message)
            return
        backoff = await self._backing_off(message)
        if backoff:
            await self._settle(message, [backoff], attempts, user_tags)
            return
        if tags is None and message.message_snapshots and not user_tags \
                and config.FORWARD_TAG_WAIT > 0:
            # A forward can't carry its own comment — give the author a
            # grace window to post the tag list as the next message.
            self._schedule_forward_ingest(message, sources, attempts)
            return
        await self._ingest_all(message, sources, user_tags, attempts)

    async def _ingest_all(self, message, sources, tags, attempts):
        """Ingest each source with the same tags, react with each outcome,
        and settle the message."""
        outcomes = []
        for src in sources:
            outcome = await self._ingest(src, message, tags)
            await self._react(message, outcome.emoji)
            outcomes.append(outcome)
        await self._settle(message, outcomes, attempts, tags)

    # ── Index login backoff ────────────────────────────────────────────
    async def _backing_off(self, message):
        """While the index client cools down from a rejected app
        password, nothing is tried: every call would be refused. Reacts ⏳
        and returns the retryable outcome; None when not backing off."""
        if not self.index.blocked():
            return None
        await self._react(message, BACKOFF)
        return _Outcome(BACKOFF, True, "the index login is backing off")

    async def _auth_failed(self, message, where, what=None):
        """The index rejected the app password (the client starts its
        cooldown). Logs it, reports it on `what` (a file or link; without
        one the caller reports the outcome itself) and returns the
        retryable outcome, so the retry queue tries again later."""
        wait = self.index.blocked()
        logging.exception("%s: index auth failed; backing off %.0fs",
                          where, wait)
        if what:
            await self._report(message, f"⏳ {what} — index login failed, "
                                        f"backing off {wait:.0f}s")
        return _Outcome(BACKOFF, True, "the index rejected our login")

    # ── Retry queue ────────────────────────────────────────────────────
    async def _settle(self, message, outcomes, attempts, tags):
        """Close out a message: queue a retry when any outcome was a
        transient failure, otherwise clear the queued marker."""
        retryable = [o for o in outcomes if o.retryable]
        if not retryable:
            await self._unreact(message, QUEUED)
            return
        if self._retry is None:      # retries disabled — the ❌ stands
            return
        reason = retryable[0].reason or "a transient failure"
        if len(retryable) > 1:
            reason += f" (+{len(retryable) - 1} more)"
        delay = await self._retry.schedule(message.channel.id, message.id,
                                           reason, attempts, tags)
        if delay is None:
            await self._unreact(message, QUEUED)
            await self._report(
                message,
                f"❌ giving up after {attempts + 1} attempt(s) — {reason}")
            return
        await self._react(message, QUEUED)
        await self._report(
            message,
            f"🔁 retrying in {parsing.format_duration(delay)} "
            f"(attempt {attempts + 2}/{config.INGEST_RETRY_ATTEMPTS + 1}) "
            f"— {reason}")

    async def _retry_loop(self):
        """Redrive due entries from the retry queue.

        Nothing is drained while the index auth backoff is active: the
        entries would only fail the same way and burn an attempt each.
        """
        await self.wait_until_ready()
        while not self.is_closed():
            await asyncio.sleep(config.INGEST_RETRY_POLL)
            if self.index.blocked():
                continue
            for entry in await self._retry.due():
                try:
                    await self._retry_one(entry)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logging.exception("retry: attempt for message %s failed",
                                      entry.get('message_id'))
                    # Claiming already removed it; put it back so a blip on
                    # Discord's side doesn't silently end the retry. The
                    # attempt count came back incremented, so a permanently
                    # broken entry still runs out of budget.
                    await self._retry.schedule(
                        entry['channel_id'], entry['message_id'],
                        "the retry attempt itself failed",
                        entry['attempts'], entry.get('tags'))

    async def _retry_one(self, entry):
        channel = self.get_channel(entry['channel_id']) \
            or await self.fetch_channel(entry['channel_id'])
        try:
            message = await channel.fetch_message(entry['message_id'])
        except discord.NotFound:
            logging.info("retry: message %s is gone — dropping it",
                         entry['message_id'])
            return
        await self._unreact(message, QUEUED)
        logging.info("retry: attempt %d for message %s (%s)",
                     entry['attempts'] + 1, message.id,
                     entry.get('reason'))
        await self._handle_message(message, attempts=entry['attempts'],
                                   tags=entry.get('tags') or None)

    # ── Source discovery ───────────────────────────────────────────────
    async def _gather_sources(self, message):
        """Everything ingestible in a message: attachments (incl. forwarded
        snapshots), direct media links in the text, and media behind embeds
        (GIF picker, unfurled image/GIF links). Returns (sources, tag_text,
        urls) — tag_text is the author's text with URLs removed, so a link
        post can carry a tag list too; urls feed the yt-dlp fallback when
        nothing was directly ingestible."""
        sources, seen = [], set()
        texts = [message.content or '']
        attachments = list(message.attachments)
        embeds = list(message.embeds)
        for snapshot in message.message_snapshots:
            attachments.extend(snapshot.attachments)
            embeds.extend(getattr(snapshot, 'embeds', None) or [])
            texts.append(getattr(snapshot, 'content', None) or '')
        sources.extend(attachments)

        urls = [u for text in texts for u in parsing.extract_urls(text)]
        for url in urls:
            fname = parsing.media_url_info(url, config.ALLOWED_EXT)
            if fname and url not in seen:
                seen.add(url)
                sources.append(_UrlSource(url, fname))
        served = await self._sniff_links([u for u in urls if u not in seen])
        for url, (fetch_url, fname) in served.items():
            seen.update((url, fetch_url))
            sources.append(_UrlSource(fetch_url, fname))

        # Page links (Tenor, image hosts) carry their media in an embed,
        # which Discord unfurls asynchronously — it may not exist yet at
        # message time. Re-fetch once before giving up on the links.
        if urls and not sources and not embeds:
            await asyncio.sleep(config.EMBED_UNFURL_WAIT)
            with contextlib.suppress(discord.HTTPException):
                refreshed = await message.channel.fetch_message(message.id)
                embeds = list(refreshed.embeds)

        for embed in embeds:
            if embed.url and embed.url in seen:
                continue   # the raw link itself is already being ingested
            for candidate in self._embed_media(embed):
                fname = candidate and parsing.media_url_info(candidate,
                                                             config.ALLOWED_EXT)
                if not fname or candidate in seen:
                    continue
                seen.add(candidate)
                if embed.url:
                    seen.add(embed.url)
                sources.append(_UrlSource(candidate, fname))
                break

        return sources, parsing.strip_urls(message.content), urls

    async def _sniff_links(self, urls):
        """{url: (fetch_url, filename)} for links that name no file but
        serve one — a CDN path like `…/ab/cd/<md5>` can be an mp4,
        and only the server's Content-Type says so. Discord's unfurler
        asks the server; so does this, with one HEAD per link.

        Links a yt-dlp extractor claims (YouTube, TikTok, …) are never
        asked. So in practice this is the few links per message that would
        otherwise have gone to yt-dlp's metadata probe — which can't see a
        bare file as media, because it has no codec or height to show.
        """
        candidates = list(dict.fromkeys(urls))
        if candidates and self._resolver_enabled():
            # First call compiles every extractor's URL regex — off the loop.
            claimed = set(await asyncio.to_thread(resolver.filter_resolvable,
                                                  candidates))
            candidates = [u for u in candidates if u not in claimed]
        if not candidates:
            return {}
        return await sniff.sniff_many(self._downloader.session(), candidates,
                                      config.ALLOWED_EXT)

    @staticmethod
    def _embed_media(embed):
        """Candidate direct-media URLs of one embed, best first. Thumbnails
        only count for image/GIF embeds — on article/link embeds they are
        page previews, not media anyone meant to ingest."""
        candidates = [getattr(getattr(embed, 'video', None), 'url', None),
                      getattr(getattr(embed, 'image', None), 'url', None)]
        if getattr(embed, 'type', '') in ('image', 'gifv'):
            candidates.append(
                getattr(getattr(embed, 'thumbnail', None), 'url', None))
        return [c for c in candidates if c]

    # ── Link fallback: yt-dlp ──────────────────────────────────────────
    async def _resolve_and_ingest(self, message, urls, user_tags):
        """Second chance for links with no directly ingestible media:
        yt-dlp, logged in with whatever the index stores for the link's
        site. A link a real extractor claims is resolved straight away; one
        that only the generic extractor would take is probed first.
        Returns (handled, outcomes) — handled is False when there is
        nothing to try and the caller posts the 🚫."""
        if not self._resolver_enabled():
            return False, []
        # First call compiles every extractor's URL regex — off the loop.
        claimed = set(await asyncio.to_thread(resolver.filter_resolvable,
                                              urls))
        plan = self._plan_links(urls, claimed)
        if not plan:
            return False, []
        # Checked before anything downloads, so no transfer is spent on a
        # file the index cannot accept right now.
        backoff = await self._backing_off(message)
        if backoff:
            return True, [backoff]
        await self._react(message, DOWNLOADING)
        outcomes = []
        try:
            for url, probe in plan:
                outcome = await self._resolve_one(url, probe, message,
                                                  user_tags)
                await self._react(message, outcome.emoji)
                outcomes.append(outcome)
        finally:
            await self._unreact(message, DOWNLOADING)
        return True, outcomes

    @staticmethod
    def _plan_links(urls, claimed):
        """(url, probe first?) for every link worth handing to yt-dlp.

        A link a real extractor claims goes straight to a download. One
        nothing claims may still hide a video only the generic extractor
        finds, but that extractor "accepts" any page, so a download must
        not start on a hunch: it is probed first, and only the first
        YTDLP_MAX_PROBES of them — a link dump must not become a crawl.
        """
        plan, probes = [], 0
        for url in dict.fromkeys(urls):
            if url in claimed:
                plan.append((url, False))
            elif probes < config.YTDLP_MAX_PROBES:
                probes += 1
                plan.append((url, True))
        return plan

    async def _resolve_one(self, url, probe, message, user_tags):
        """Resolve one link and ingest the result, falling back cleanly on
        any failure. The site login is fetched first: an index that can't
        be asked is reported as itself, never as "login required"."""
        try:
            async with site_logins.cookies_for(
                    self.index, url, tempfile.gettempdir(),
                    config.YTDLP_COOKIES) as login:
                res = await self._run_resolver(url, probe, login, message)
        except AuthError:
            return await self._auth_failed(message, 'resolver', f"<{url}>")
        except IndexUnavailable as e:
            reason = f"couldn't fetch the site login — {e}"
            await self._report(message, f"❌ <{url}> — {reason}")
            return _Outcome(ERROR, True, reason)
        if isinstance(res, _Outcome):
            return res
        # The source site (youtube, tiktok, …) rides along as a tag.
        tags = list(user_tags) + ([res.site] if res.site else [])
        return await self._ingest(_LocalSource(res.path, res.filename, url),
                                  message, tags)

    async def _run_resolver(self, url, probe, login, message):
        """yt-dlp's Resolved for one link, or the _Outcome that settled it.
        `login` is the site_logins.LoginRun the download runs with."""
        if probe:
            verdict = await self._probe(url, login, message)
            if verdict is not None:
                return verdict          # not media, or a playlist
        try:
            res = await asyncio.wait_for(
                asyncio.to_thread(resolver.resolve, url,
                                  tempfile.gettempdir(),
                                  config.YTDLP_MAX_BYTES,
                                  login.path,
                                  config.YTDLP_STALL_TIMEOUT,
                                  quality=config.YTDLP_QUALITY,
                                  pot_provider=config.YTDLP_POT_PROVIDER),
                timeout=config.YTDLP_MAX_DURATION)
        except asyncio.TimeoutError:
            # The stall watchdog handles a frozen download; this only trips
            # when a resolve hangs before the download stage (extraction).
            logging.warning("resolver: exceeded the %gs hard ceiling for %s",
                            config.YTDLP_MAX_DURATION, url)
            reason = (f"resolve hung past the {config.YTDLP_MAX_DURATION:g}s "
                      f"ceiling before downloading")
            await self._report(message, f"❌ <{url}> — {reason}")
            return _Outcome(ERROR, True, reason)
        except resolver.ResolveError as e:
            # Typed, specific reason (stalled, over cap, blocked, no video,
            # login required, download failed) — surface it verbatim in the
            # log channel, with what an admin can do about a login.
            reason = e.reason + login.hint(e)
            logging.warning("resolver: %s — %s", url, reason)
            await self._report(message, f"{e.emoji} <{url}> — {reason}")
            return _Outcome(e.emoji, e.retryable, reason)
        except Exception as e:
            logging.exception("resolver: unexpected failure for %s", url)
            await self._report(
                message, f"❌ <{url}> — unexpected error: "
                f"{type(e).__name__} (see container log)")
            return _Outcome(ERROR)
        if res is None:
            # Only reached if the resolver is disabled (guarded upstream).
            await self._report(
                message, f"🚫 <{url}> — link resolver is unavailable")
            return _Outcome(UNSUPPORTED)
        login.used = res.used_cookies
        return res

    async def _probe(self, url, login, message):
        """Decide whether a link nothing claims has a video worth
        downloading. Returns None to go ahead, or the _Outcome that settles
        it here.

        Only the generic extractor is left for such a link, and it reads
        whatever page it is pointed at — so the address is checked first,
        exactly as a sniff checks it, and the probe fetches metadata only.
        """
        try:
            await asyncio.to_thread(sniff.require_public, url)
        except sniff.Blocked as e:
            await self._report(message, f"🚫 <{url}> — {e}")
            return _Outcome(UNSUPPORTED, False, str(e))
        try:
            meta = await asyncio.wait_for(
                asyncio.to_thread(resolver.probe, url, login.path,
                                  config.YTDLP_POT_PROVIDER),
                timeout=config.YTDLP_PROBE_TIMEOUT)
        except asyncio.TimeoutError:
            reason = (f"reading the page took longer than "
                      f"{config.YTDLP_PROBE_TIMEOUT:g}s")
            logging.info("resolver: probe of %s — %s", url, reason)
            await self._report(message, f"❌ <{url}> — {reason}")
            return _Outcome(ERROR, True, reason)
        except resolver.ResolveError as e:
            # yt-dlp's own reason for not being able to read the page,
            # which is genuinely informative — surface it verbatim.
            reason = e.reason + login.hint(e)
            await self._report(message, f"{e.emoji} <{url}> — {reason}")
            return _Outcome(e.emoji, e.retryable, reason)
        except Exception:
            logging.exception("resolver: probe of %s failed", url)
            return _Outcome(ERROR, True, "the metadata probe failed")
        if meta is None:
            return _Outcome(UNSUPPORTED)    # resolver unavailable
        if meta.is_playlist and meta.entry_count > 1:
            # Some page with several videos on it. The resolver would take
            # the first, and on a page nothing claims, "first" is whatever
            # the generic extractor happened to find first — say so instead.
            reason = (f"that page holds {meta.entry_count} videos — post a "
                      f"link to the one you mean")
            await self._report(message, f"🚫 <{url}> — {reason}")
            return _Outcome(UNSUPPORTED, False, reason)
        if not meta.has_media():
            reason = "no video behind that link"
            await self._report(message, f"🚫 <{url}> — {reason}")
            return _Outcome(UNSUPPORTED, False, reason)
        return None

    # ── Forward tag grace window ───────────────────────────────────────
    async def _apply_pending_tags(self, message):
        """Text-only message: when it parses as a tag list and the author
        has forwards waiting in their grace window, tag them all."""
        tags = parsing.parse_message_tags(message.content)
        if not tags:
            return
        pending = self._pending_forwards.pop(self._forward_key(message), None)
        if not pending:
            return
        for entry in pending:
            entry.tags = tags
            entry.event.set()
        await self._react(message, TAGGED)
        await self._report(
            message,
            f"🏷️ tags `{', '.join(tags)}` from **{message.author.display_name}** "
            f"applied to {len(pending)} waiting forward(s)")

    @staticmethod
    def _forward_key(message):
        return (message.channel.id, message.author.id)

    def _schedule_forward_ingest(self, message, attachments, attempts=0):
        entry = _PendingForward()
        self._pending_forwards.setdefault(self._forward_key(message),
                                          []).append(entry)
        task = self.loop.create_task(
            self._ingest_forward(message, attachments, entry, attempts))
        self._forward_tasks.add(task)
        task.add_done_callback(self._forward_task_done)

    def _forward_task_done(self, task):
        self._forward_tasks.discard(task)
        if not task.cancelled() and task.exception():
            logging.error("ingest: forward grace-window task crashed",
                          exc_info=task.exception())
            self.loop.create_task(self._log(
                "❌ a forward ingest task crashed — check the container logs"))

    async def _ingest_forward(self, message, attachments, entry, attempts=0):
        """Wait for the author's follow-up tag message (or the grace-window
        timeout), then ingest the forwarded attachments."""
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(entry.event.wait(), config.FORWARD_TAG_WAIT)
        # Timeout path: the entry is still registered — retire it.
        key = self._forward_key(message)
        lst = self._pending_forwards.get(key)
        if lst and entry in lst:
            lst.remove(entry)
            if not lst:
                del self._pending_forwards[key]
        backoff = await self._backing_off(message)
        if backoff:
            await self._settle(message, [backoff], attempts, entry.tags)
            return
        await self._ingest_all(message, attachments, entry.tags, attempts)

    # ── Ingest ─────────────────────────────────────────────────────────
    async def _ingest(self, att, message, user_tags):
        """Download one attachment and upload it to the index; returns the
        _Outcome describing the result. The message author is the Discord
        user who posted (or forwarded) the attachment — when they linked
        their Discord account to an index account, the index entry is owned
        by that user instead of the bot account. A DM's is stored private
        to that user, and refused when there is none. `user_tags` (a list)
        is applied on top of INGEST_TAGS; all attachments of a message get
        the same tags."""
        size_mb = (att.size or 0) / 1e6
        label = f"`{att.filename}`" + (f" ({size_mb:.1f} MB)" if size_mb else "")
        ext = os.path.splitext(att.filename)[1].lstrip('.').lower()
        if ext not in config.ALLOWED_EXT:
            logging.info("ingest: skipping %s (unsupported type)", att.filename)
            if getattr(att, 'path', None):   # yt-dlp temp file, ours to drop
                with contextlib.suppress(FileNotFoundError):
                    os.remove(att.path)
            await self._report(message, f"🚫 {label} — unsupported type")
            return _Outcome(UNSUPPORTED)
        private = self._is_dm(message)
        base_tags = [t.strip() for t in config.INGEST_TAGS.split(',') if t.strip()]
        tags = ','.join(dict.fromkeys(list(user_tags) + base_tags))
        # A _LocalSource (yt-dlp) is already on disk; everything else is
        # downloaded first. Either way the temp file is ours to delete.
        local = getattr(att, 'path', None)
        tmp = local or os.path.join(tempfile.gettempdir(),
                                    f"ingest-{uuid.uuid4().hex}.{ext}")
        try:
            # Asked before a byte is downloaded.
            uploaded_for = await self.index.lookup_discord_user(
                message.author.id)
            if private and not uploaded_for:
                # _handle_message turned unlinked DMs away, but the link can
                # be removed while a forward waits or a download runs — and
                # an unowned private item would land, encrypted, in the bot
                # account's own library.
                await self._tell_unlinked(message)
                return _Outcome(UNSUPPORTED)
            owner = uploaded_for or config.INDEX_BOT_USERNAME
            logging.info("ingest: %s (%.1f MB) from %s as %s%s",
                         att.filename, size_mb, message.author.id, owner,
                         " (private)" if private else "")
            if not local:
                await self._downloader.download(att, tmp)
            result = await self.index.upload(tmp, att.filename, tags,
                                             uploaded_for=uploaded_for,
                                             private=private)
        except AuthError:
            return await self._auth_failed(message, 'ingest', label)
        except IndexUnavailable as e:
            reason = f"couldn't look up who posted it — {e}"
            await self._report(message, f"❌ {label} — {reason}")
            return _Outcome(ERROR, True, reason)
        except sniff.Blocked as e:
            # A link to a host that isn't on the public internet. Say which,
            # since the sender may simply have pasted a LAN URL by accident.
            await self._report(message, f"🚫 {label} — {e}")
            return _Outcome(UNSUPPORTED)
        except TooLarge as e:
            size = f" ({e.size / 1e6:.0f} MB)" if e.size else ""
            await self._report(
                message, f"🚫 {label}{size} is over the "
                f"{config.LINK_MAX_BYTES / 1e6:.0f} MB link limit")
            return _Outcome(UNSUPPORTED)
        except aiohttp.ClientResponseError as e:
            logging.warning("ingest: download of %s failed HTTP %s",
                            att.filename, e.status)
            reason = (f"couldn't fetch the file (source returned "
                      f"HTTP {e.status})")
            await self._report(message, f"❌ {label} — {reason}")
            # 5xx/429 is the source having a bad moment; a 404/410 is not
            # going to start existing later.
            return _Outcome(ERROR, e.status >= 500 or e.status == 429, reason)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logging.warning("ingest: transfer of %s failed: %s",
                            att.filename, type(e).__name__)
            reason = f"network error during transfer ({type(e).__name__})"
            await self._report(message, f"❌ {label} — {reason}")
            return _Outcome(ERROR, True, reason)
        except Exception as e:
            logging.exception("ingest: failed for %s", att.filename)
            await self._report(
                message, f"❌ {label} — {type(e).__name__} (see container log)")
            return _Outcome(ERROR)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.remove(tmp)
        logging.info("ingest: %s → %s", att.filename, result.status)
        if result.status == 'ok':
            how = "privately for" if private else "for"
            await self._report(
                message, f"✅ {label} indexed {how} **{owner}** — tags: `{tags}`")
            return _Outcome(OK)
        if result.status == 'duplicate':
            await self._report(
                message, f"♻️ {label} — {self._duplicate_note(result)}")
            return _Outcome(DUPLICATE)
        reason = result.detail or 'the index rejected the upload'
        await self._report(message, f"❌ {label} — {reason}")
        return _Outcome(ERROR, result.retryable, reason)

    @staticmethod
    def _duplicate_note(result):
        """Human line for a rejected duplicate.

        In API mode the index names the item the file collided with, so
        say which one and link it. A private collision is reported without
        its tags or owner — the index withholds them on purpose, and a
        Discord channel is the wrong place to learn what is sitting in
        someone else's private library.
        """
        dup = result.duplicate
        if not dup or not dup.get('filepath'):
            if dup and dup.get('is_private'):
                return (f"already indexed as a private item "
                        f"(#{dup.get('id', '?')})")
            return "already indexed (duplicate)"
        link = f"{config.INDEX_PUBLIC_URL}/media/{dup['filepath']}"
        note = (f"already indexed as [#{dup.get('id', '?')}]({link}) for "
                f"**{dup.get('uploaded_by') or 'Unknown'}**")
        tag_links = parsing.format_tag_links(dup.get('tags'),
                                             config.INDEX_PUBLIC_URL)
        return f"{note} — tags: {tag_links}" if tag_links else note

    # ── Reactions / lifecycle ──────────────────────────────────────────
    @staticmethod
    async def _react(message, emoji):
        with contextlib.suppress(discord.HTTPException):
            await message.add_reaction(emoji)

    async def _unreact(self, message, emoji):
        with contextlib.suppress(discord.HTTPException):
            await message.remove_reaction(emoji, self.user)

    async def close(self):
        await self.index.close()
        await self._downloader.close()
        if self._retry is not None:
            with contextlib.suppress(Exception):
                await self._retry.close()
        await super().close()


if __name__ == '__main__':
    IndexBot().run(config.BOT_TOKEN)
