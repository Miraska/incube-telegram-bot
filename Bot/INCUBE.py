import os
import asyncio
import openai
import html
import logging
from dotenv import load_dotenv
from typing import Any, Awaitable, Callable, Dict, Optional
from datetime import date
from bs4 import BeautifulSoup as bs
from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import CommandStart
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.dispatcher.middlewares.base import BaseMiddleware
from aiogram.utils.media_group import MediaGroupBuilder
from aiogram.enums.parse_mode import ParseMode
from aiogram.enums.content_type import ContentType
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State

# ========================================================================== #
load_dotenv()

NL = '\n'

TG_BOT_TOKEN = os.getenv('TG_BOT_TOKEN')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
CHANNEL_ID = os.getenv('CHANNEL_ID')
CHANNEL_URL = os.getenv('CHANNEL_URL')

GPT_PROMPT = '''Измени немного слова в посте, не меняя разметку.
Удаляй следующие элементы:
- Упоминания аккаунтов (начинающиеся с @)
- Ссылки, начинающиеся на t.me/
Не трогай другие ссылки (например, https:// или http://).
Сократи текст, без изменения смысла текста, чтобы текст влезал в 1000 сиволов.'''


GPT_MAX_TOKENS = 500
GPT_TEMPERATURE = 0.7
LINK_CAPTION = 'SUBSCRIBE'
LINK_APPEND = f'{NL * 2}<a href="{CHANNEL_URL}">{LINK_CAPTION}</a>'
MAX_DAILY_REPOSTS = 555
MAX_SYMBOLS_MESSAGE = 4096

PROXY_URL = None
# PROXY_URL = 'http://178.218.44.79:3128'

ALLOWED_USERS = [416064234, 1498695786, 6799175057, 949078033]

# ========================================================================== #

# логирование
logging.basicConfig(level=logging.INFO, format='[{asctime}] [{levelname}] {message}', style='{')

# Инициализация бота и диспетчера
storage = MemoryStorage()
session = AiohttpSession(proxy=PROXY_URL)
bot = Bot(token=TG_BOT_TOKEN, session=session, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=storage)
openai.api_key = OPENAI_API_KEY
default_state = State()

# ========================================================================== #

class AlbumMiddleware(BaseMiddleware):
    def __init__(self, latency: float = 0.01):
        self.latency = latency
        self.album_data = {}
        super().__init__()

    async def __call__(self, handler: Callable[[types.TelegramObject, Dict[str, Any]], Awaitable[Any]],
                       event: types.Message, data: Dict[str, Any]) -> Any:
        id_ = event.media_group_id
        if not id_:
            return await handler(event, data)

        if id_ not in self.album_data:
            self.album_data[id_] = []
        self.album_data[id_].append(event)
        await asyncio.sleep(self.latency)

        data['album'] = self.album_data[event.media_group_id]
        return await handler(event, data)

dp.message.middleware(AlbumMiddleware())

# ========================================================================== #

def escape_html(text: str) -> str:
    return html.unescape(text)

def truncate_html(text: str, trunc: int) -> str:
    soup = bs(text, 'html.parser')
    if len(soup.get_text()) <= trunc:
        return text
    return str(bs(text[:trunc], 'html.parser'))

async def chat_completion(prompt: str, custom_prompt: Optional[str] = None) -> str:
    try:
        response = openai.ChatCompletion.create(
            model='gpt-4',
            messages=[
                {'role': 'system', 'content': custom_prompt or GPT_PROMPT},
                {'role': 'user', 'content': prompt or ''}
            ],
            max_tokens=GPT_MAX_TOKENS,
            temperature=GPT_TEMPERATURE
        )
        msg = response.choices[0].message['content']
        return msg.strip() if msg else ''
    except Exception as e:
        logging.error(f'Ошибка при обращении к OpenAI: {str(e)}')
        return 'Произошла ошибка при обработке запроса.'

async def rewrite(text_to_send: str, trunc: Optional[int] = None) -> str:
    if not text_to_send:
        return ''
    
    rewritten_text = await chat_completion(text_to_send)

    shortened_text_prompt = '''Сократи текст до 1000 символов, сохранив смысл, не меняя сам текст и не меняя разметку.'''
    shortened_text = await chat_completion(rewritten_text, custom_prompt=shortened_text_prompt)

    final_text = f'{shortened_text} {LINK_APPEND}'
    
    if trunc:
        trunc -= (len(LINK_APPEND) + 2)
        shortened_text = truncate_html(shortened_text, trunc)
        final_text = f'{shortened_text} {LINK_APPEND}'
    
    return final_text


@dp.message(CommandStart())
async def start_handler(message: types.Message, state: FSMContext):
    await state.set_state(default_state)
    await message.answer(f'Приветствую, {message.from_user.full_name}!')



@dp.message(~F.media_group_id)
async def message_handler(message: types.Message, state: FSMContext):
    if message.from_user.id not in ALLOWED_USERS:
        logging.warning(f'Доступ запрещен для пользователя с ID: {message.from_user.id}')
        return
    text_to_send = message.html_text or message.caption
    if not text_to_send:
        return

    await state.set_state(default_state)
    data = await state.get_data() or {}
    today = date.today().isoformat()
    cnt = data.get(today, 0)
    if cnt >= MAX_DAILY_REPOSTS:
        await message.answer(f'🛑 Превышен суточный лимит в {MAX_DAILY_REPOSTS} сообщений.')
        return

    final_text = await rewrite(text_to_send)

    chat_id = CHANNEL_ID

    cnt += 1
    await state.update_data({today: cnt})

    try:
        if message.photo:
            await bot.send_photo(chat_id=chat_id, photo=message.photo[-1].file_id, caption=final_text)
        elif message.video:
            await bot.send_video(chat_id=chat_id, video=message.video.file_id, caption=final_text)
        elif message.document:
            await bot.send_document(chat_id=chat_id, document=message.document.file_id, caption=final_text)
        else:
            await bot.send_message(chat_id=chat_id, text=final_text)
    except Exception as e:
        logging.error(f'Ошибка при отправке сообщения: {str(e)}')



@dp.message(F.media_group_id)
async def album_handler(message: types.Message, album: list[types.Message], state: FSMContext):
    if message.from_user.id not in ALLOWED_USERS:
        logging.warning(f'Доступ запрещен для пользователя с ID: {message.from_user.id}')
        return

    await state.set_state(default_state)
    data = await state.get_data() or {}
    today = date.today().isoformat()
    cnt = data.get(today, 0)
    if cnt >= MAX_DAILY_REPOSTS:
        await message.answer(f'🛑 Превышен суточный лимит в {MAX_DAILY_REPOSTS} сообщений.')
        return

    chat_id = CHANNEL_ID
    media_group = MediaGroupBuilder()

    for obj in album:
        try:
            media = getattr(obj, obj.content_type, None)
            if not media:
                continue
            if obj.content_type == ContentType.PHOTO:
                media = media[-1]
            file_id = getattr(media, 'file_id', None)
            if not file_id:
                continue
            cap = obj.html_text or obj.caption or None
            if cap:
                cap = await rewrite(cap)
            media_group.add(type=obj.content_type, media=file_id, caption=cap)
        except Exception as e:
            logging.error(f'Ошибка при обработке альбома: {str(e)}')

    cnt += 1
    await state.update_data({today: cnt})

    try:
        await bot.send_media_group(chat_id=chat_id, media=media_group.build())
    except Exception as e:
        logging.error(f'Ошибка при отправке медийной группы: {str(e)}')

# ========================================================================== #

async def main():
    await dp.start_polling(bot)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except:
        print('Бот отключен')
