"""Run inside the existing authorized MTProto container. Never prints tokens."""
import asyncio
import json
import os
from pathlib import Path
import re

from telethon import TelegramClient, functions, types, utils
from telethon.sessions import StringSession

STATE = Path('/app/data/instagram-inbox-bootstrap.json')


def save(state):
    fd = os.open(STATE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(state, stream)


async def main():
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    client = TelegramClient(StringSession(os.environ['TG_SESSION_STRING']),
                            int(os.environ['TG_API_ID']), os.environ['TG_API_HASH'])
    await client.connect()
    try:
        me = await client.get_me()
        if not me or me.bot:
            raise RuntimeError('Authorized human Telegram session required')
        state['owner_id'] = me.id
        if not state.get('bot_token'):
            async with client.conversation('BotFather', timeout=45) as conv:
                await conv.send_message('/cancel')
                await conv.get_response()
                await conv.send_message('/newbot')
                answer = (await conv.get_response()).raw_text
                if 'name' not in answer.lower():
                    raise RuntimeError('BotFather did not offer bot creation: ' + answer[:240])
                await conv.send_message('TEXNIKACH Instagram')
                answer = (await conv.get_response()).raw_text
                if 'username' not in answer.lower():
                    raise RuntimeError('BotFather did not request username: ' + answer[:240])
                for name in ['texnikach_instagram_desk_bot', 'texnikach_ig_managers_bot']:
                    await conv.send_message(name)
                    answer = (await conv.get_response()).raw_text
                    token = re.search(r'\b\d{6,15}:[A-Za-z0-9_-]{30,}\b', answer)
                    if token:
                        state.update(bot_token=token.group(), bot_username=name)
                        save(state)
                        break
                    if 'taken' not in answer.lower():
                        raise RuntimeError('BotFather username setup did not complete')
                if not state.get('bot_token'):
                    raise RuntimeError('Proposed bot usernames unavailable')
        bot = await client.get_entity(state['bot_username'])
        if not state.get('group_id'):
            # Recover an existing exact-title group owned by this account before creating.
            candidates = [d.entity async for d in client.iter_dialogs()
                          if d.name == 'TEXNIKACH — Instagram' and getattr(d.entity, 'creator', False)]
            if len(candidates) > 1:
                raise RuntimeError('Multiple matching groups; refuse ambiguous provisioning')
            if candidates:
                group = candidates[0]
            else:
                result = await client(functions.channels.CreateChannelRequest(
                    title='TEXNIKACH — Instagram',
                    about='Instagram Direct: история клиентов и ответы с подтверждением менеджера.',
                    megagroup=True, forum=True))
                group = result.chats[0]
            state['group_id'] = utils.get_peer_id(group)
            state['group_access_hash'] = group.access_hash
            save(state)
        group = types.InputChannel(int(str(state['group_id'])[4:]), state['group_access_hash'])
        current = await client.get_entity(group)
        if not getattr(current, 'forum', False):
            await client(functions.channels.ToggleForumRequest(group, enabled=True, tabs=False))
        try:
            await client(functions.channels.InviteToChannelRequest(group, [bot]))
        except Exception as exc:
            if type(exc).__name__ != 'UserAlreadyParticipantError':
                raise
        await client(functions.channels.EditAdminRequest(
            group, bot,
            types.ChatAdminRights(change_info=True, delete_messages=True,
                                  invite_users=True, pin_messages=True, manage_topics=True),
            rank='Instagram'))
        if not state.get('invite_link'):
            result = await client(functions.messages.ExportChatInviteRequest(group))
            state['invite_link'] = result.link
            save(state)
        print(json.dumps({k:state[k] for k in ['owner_id','bot_username','group_id','invite_link']}, ensure_ascii=False))
    finally:
        await client.disconnect()


asyncio.run(main())
