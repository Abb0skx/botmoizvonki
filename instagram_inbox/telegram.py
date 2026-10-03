from urllib.parse import urlparse
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyParameters


def keyboard(draft_id, retry=False, manual=False):
    rows = [[InlineKeyboardButton(text='🔄 Повторить' if retry else '✅ Отправить',
            callback_data=f'ig:{"retry" if retry else "send"}:{draft_id}')]]
    if not retry:
        if not manual:
            rows.append([InlineKeyboardButton(text='🔄 Обновить ответ', callback_data=f'ig:regenerate:{draft_id}')])
        rows.append([InlineKeyboardButton(text='❌ Не отвечать', callback_data=f'ig:cancel:{draft_id}')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def media_url(attachment):
    payload = attachment.get('payload') or {}
    value = payload.get('url') or payload.get('link') or ''
    try:
        parsed = urlparse(value)
        host = (parsed.hostname or '').lower()
        if parsed.scheme == 'https' and not parsed.username and any(
            host == domain or host.endswith('.' + domain)
            for domain in ('instagram.com', 'cdninstagram.com', 'fbcdn.net', 'facebook.com')):
            return value
    except (ValueError, TypeError):
        pass
    return ''


class TelegramAPI:
    def __init__(self, settings):
        self.settings = settings
        self.bot = Bot(settings.telegram_bot_token)

    async def check_setup(self):
        me = await self.bot.get_me()
        chat = await self.bot.get_chat(self.settings.telegram_group_id)
        member = await self.bot.get_chat_member(chat.id, me.id)
        if not chat.is_forum or not getattr(member, 'can_manage_topics', False):
            raise RuntimeError('Bot must be forum administrator with can_manage_topics')
        return me

    async def authorized(self, user):
        if not user or user.get('is_bot') or not user.get('id'):
            return False
        member = await self.bot.get_chat_member(self.settings.telegram_group_id, user['id'])
        if member.status in ('left', 'kicked'):
            return False
        return user['id'] in self.settings.manager_ids or member.status in ('creator', 'administrator')

    async def create_topic(self, name):
        result = await self.bot.create_forum_topic(self.settings.telegram_group_id, name=name[:128])
        return result.message_thread_id

    async def reopen(self, topic_id):
        try:
            await self.bot.reopen_forum_topic(self.settings.telegram_group_id, topic_id)
        except TelegramBadRequest as exc:
            if 'TOPIC_NOT_MODIFIED' not in str(exc):
                raise

    async def close_topic(self, topic_id):
        try:
            await self.bot.close_forum_topic(self.settings.telegram_group_id, topic_id)
        except TelegramBadRequest as exc:
            if 'TOPIC_NOT_MODIFIED' not in str(exc):
                raise

    async def rename(self, topic_id, name):
        await self.bot.edit_forum_topic(self.settings.telegram_group_id, topic_id, name=name[:128])

    async def text(self, topic_id, text, markup=None, reply_id=None):
        kwargs = dict(chat_id=self.settings.telegram_group_id, message_thread_id=topic_id,
                      text=text, reply_markup=markup)
        if reply_id:
            kwargs['reply_parameters'] = ReplyParameters(message_id=reply_id, allow_sending_without_reply=True)
        try:
            result = await self.bot.send_message(**kwargs)
        except TelegramBadRequest as exc:
            if 'TOPIC_CLOSED' not in str(exc):
                raise
            await self.reopen(topic_id)
            result = await self.bot.send_message(**kwargs)
        return result.message_id

    async def edit(self, message_id, text, markup=None):
        try:
            await self.bot.edit_message_text(chat_id=self.settings.telegram_group_id,
                                             message_id=message_id, text=text[:4000], reply_markup=markup)
        except TelegramBadRequest as exc:
            if 'message is not modified' not in str(exc).lower():
                raise

    async def attachment(self, topic_id, item, reply_id):
        url = media_url(item)
        kind = item.get('type', 'attachment')
        if not url:
            return await self.text(topic_id, '📎 Вложение недоступно через API. Посмотрите его в Instagram.', reply_id=reply_id)
        kwargs = dict(chat_id=self.settings.telegram_group_id, message_thread_id=topic_id,
                      reply_parameters=ReplyParameters(message_id=reply_id, allow_sending_without_reply=True))
        try:
            if kind == 'image':
                result = await self.bot.send_photo(photo=url, **kwargs)
            elif kind == 'video' and not ('instagram.com/' in url and '/reel/' in url):
                result = await self.bot.send_video(video=url, **kwargs)
            elif kind in ('audio', 'voice'):
                result = await self.bot.send_audio(audio=url, **kwargs)
            else:
                return await self.text(topic_id, f'📎 {kind}\n{url[:3000]}', reply_id=reply_id)
            return result.message_id
        except TelegramBadRequest:
            return await self.text(topic_id, f'📎 {kind}\n{url[:3000]}', reply_id=reply_id)

    async def answer(self, callback_id, text):
        try:
            await self.bot.answer_callback_query(callback_id, text=text[:180])
        except TelegramBadRequest:
            pass  # Expired spinner must never roll back a recorded manager decision.
