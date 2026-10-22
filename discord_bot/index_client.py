"""The Discord bot's identity calls on top of the shared index client
(bots_common/index_client.py).

`lookup_discord_user` asks which index account linked a Discord user id,
and `verify_discord_link` redeems the link code a user DMs the bot.
"""
from bots_common import index_client


class IndexClient(index_client.IndexClient):
    async def lookup_discord_user(self, discord_id):
        """Index username that linked this Discord user id, or None.
        Cached, and raises like any identity lookup (see _linked_owner)."""
        return await self._linked_owner(
            discord_id, '/api/discord/user',
            {'discord_id': str(discord_id)})

    async def verify_discord_link(self, discord_id, code):
        """Redeem a link code the user DMed us, on behalf of `discord_id`
        (the DM's sender as Discord reported it). Returns the index username
        it linked, or None for a wrong, used or expired code; the index
        answers 400 for a code that is not even well-formed. Raises like
        lookup_discord_user otherwise."""
        username = await self._identity_call(
            'POST', '/api/discord/verify', not_found=(400, 404),
            data={'discord_id': str(discord_id), 'code': code})
        if username:
            # The cached "not linked" (or the previous owner) is stale.
            self._forget_owner(discord_id)
        return username
