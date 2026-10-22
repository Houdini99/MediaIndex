"""The Telegram bot's identity call on top of the shared index client
(bots_common/index_client.py).

`lookup_telegram_user` is the ingest gate: "is this sender allowed to
upload, and as whom?" is a question for the web app.
"""
from bots_common import index_client


class IndexClient(index_client.IndexClient):
    async def lookup_telegram_user(self, telegram_id, username=None):
        """Index username that claimed this Telegram account, or None.

        None means "not linked" and the caller must drop the message
        without downloading anything. An outage raises (see
        _linked_owner), so the gate still fails closed without the caller
        mistaking it for a verdict on the sender.
        """
        params = {'telegram_id': str(telegram_id)}
        if username:
            params['username'] = username
        return await self._linked_owner(telegram_id, '/api/telegram/user',
                                        params)
