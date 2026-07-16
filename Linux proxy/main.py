import logging
import time
import aiohttp
import json
from aiogram.exceptions import TelegramAPIError
import asyncio
import html
import uuid
import os
import pytz
from datetime import datetime
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Message, BufferedInputFile, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, StateFilter
from aiogram.types import BusinessMessagesDeleted
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.methods.base import TelegramMethod
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))

import database

BOT_TOKEN = os.getenv("BOT_TOKEN")
INVOKE_URL = os.getenv("INVOKE_URL")
AI_MODEL = os.getenv("AI_MODEL")
OWNER_ID = int(os.getenv("OWNER_ID", 0))

class APIKeyManager:
    def __init__(self):
        self.keys = [k.strip() for k in os.getenv("AI_API_POOL", "").split(",") if k.strip()]
        if not self.keys:
            fallback = os.getenv("AI_API", "")
            self.keys = [fallback] if fallback else []
        self.active_requests = {k: 0 for k in self.keys}
        self.penalties = {k: 0 for k in self.keys}
    
    def get_least_loaded_key(self, excluded_keys=None):
        if excluded_keys is None: excluded_keys = set()
        if not self.keys: return ""
        available_keys = [k for k in self.keys if k not in excluded_keys and time.time() > self.penalties[k]]
        if not available_keys:
            available_keys = [k for k in self.keys if k not in excluded_keys]
            if not available_keys:
                available_keys = self.keys
        return min(available_keys, key=lambda k: self.active_requests[k])
    
    def acquire(self, key):
        if key in self.active_requests: self.active_requests[key] += 1
        
    def release(self, key):
        if key in self.active_requests: self.active_requests[key] = max(0, self.active_requests[key] - 1)
        
    def penalize(self, key, seconds=60):
        if key in self.penalties: self.penalties[key] = time.time() + seconds

api_manager = APIKeyManager()

user_keys = {}
active_ai_tasks = {}

from aiogram.client.session.aiohttp import AiohttpSession

logging.basicConfig(level=logging.INFO)

PROXY_URL = os.getenv("PROXY_URL", "http://127.0.0.1:10809")
session = AiohttpSession(proxy=PROXY_URL)
bot = Bot(token=BOT_TOKEN, session=session)
dp = Dispatcher()
router = Router()

user_keys = {}

async def get_business_user_id(connection_id: str):
    return await database.get_user_id_by_connection(connection_id)

async def get_user_key(business_user_id: int):
    if business_user_id in user_keys:
        return user_keys[business_user_id]
    
    connection_id = await database.get_connection(business_user_id)
    if connection_id:
        key = database.derive_key(business_user_id, connection_id)
        user_keys[business_user_id] = key
        return key
    return None

class SettingsState(StatesGroup):
    waiting_for_mute_on = State()
    waiting_for_mute_off = State()
    waiting_for_gpt_on = State()
    waiting_for_gpt_off = State()

START_MESSAGE_TEXT = """👋 <b>Привет! Я твой персональный AI-Менеджер и Бизнес-Ассистент для Автоматизации чатов.</b>

Я помогаю управлять перепиской, модерировать чаты, сохранять историю изменений и предоставляю доступ к ИИ прямо внутри диалогов.

🛠️ <b>Как меня подключить к своему аккаунту:</b>
1. Перейдите в <b>Настройки</b> своего профиля Telegram
2. Нажмите <b>Изменить</b> (или иконку карандаша)
3. Найдите раздел <b>Автоматизация чатов</b> (Telegram Business)
4. Нажмите <b>Добавить бота</b> и укажите мой юзернейм: <code>@ai_massage_manager_bot</code>

🚀 <b>Основные функции после подключения:</b>
• <b>Автоответчик:</b> Если вы не в сети, я отправлю заглушку собеседнику (работает строго 1 раз в 30 минут, спасая от флуда).
• <b>Умный Мут:</b> Позволяет временно блокировать сообщения от конкретных пользователей (они будут автоматически удаляться).
• <b>Безопасный Лог:</b> Сохраняю удаленные и измененные сообщения в зашифрованную базу данных и пересылаю вам в этот чат (сообщения и медиа хранятся на сервере 3 дня в зашифрованном виде).
• <b>Режим ИИ:</b> По команде <code>.ask</code> отвечаю на разовые вопросы, а по команде <code>.gpt</code> включаю полноценную ИИ-комнату с памятью контекста и поддержкой фото.

📖 Чтобы увидеть полный список команд, лимитов и кулдаунов, отправьте в любой чат: <code>.help</code>

🛡️ Вы можете сами убедиться в безопасности этого проекта, посмотрев его исходный код на GitHub:
https://github.com/Lonnory/ai_manager_telegram

<i>⚠️ Обратите внимание: команды управления работают только в ваших личных бизнес-чатах!</i>"""

@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚙️ Настройки кастомизации", callback_data="open_settings")]])
    await message.answer(START_MESSAGE_TEXT, parse_mode=ParseMode.HTML, reply_markup=kb)

@router.callback_query(F.data == "back_to_main")
async def cb_back_to_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⚙️ Настройки кастомизации", callback_data="open_settings")]])
    await callback.message.edit_text(START_MESSAGE_TEXT, parse_mode=ParseMode.HTML, reply_markup=kb)

@router.callback_query(F.data == "open_settings")
async def cb_open_settings(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    settings = await database.get_settings(callback.from_user.id)
    notif_status = "ВКЛ" if settings["manager_notifications"] else "ВЫКЛ"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"Уведомления (Автоответчик) [{notif_status}]", callback_data="toggle_notif")],
        [InlineKeyboardButton(text="Изменить текст '.mute'", callback_data="edit_mute_on")],
        [InlineKeyboardButton(text="Изменить текст '.mute off'", callback_data="edit_mute_off")],
        [InlineKeyboardButton(text="Изменить текст '.gpt'", callback_data="edit_gpt_on")],
        [InlineKeyboardButton(text="Изменить текст '.gpt off'", callback_data="edit_gpt_off")],
        [InlineKeyboardButton(text="🔙 Назад в меню", callback_data="back_to_main")]
    ])
    await callback.message.edit_text("⚙️ <b>Настройки бота</b>\n\nВыберите параметр для изменения:", parse_mode=ParseMode.HTML, reply_markup=kb)

@router.callback_query(F.data == "toggle_notif")
async def cb_toggle_notif(callback: CallbackQuery, state: FSMContext):
    settings = await database.get_settings(callback.from_user.id)
    new_val = 0 if settings["manager_notifications"] else 1
    await database.update_setting(callback.from_user.id, "manager_notifications", new_val)
    await cb_open_settings(callback, state)

@router.callback_query(F.data.startswith("edit_"))
async def cb_edit_texts(callback: CallbackQuery, state: FSMContext):
    action = callback.data
    if action == "edit_mute_on": await state.set_state(SettingsState.waiting_for_mute_on)
    elif action == "edit_mute_off": await state.set_state(SettingsState.waiting_for_mute_off)
    elif action == "edit_gpt_on": await state.set_state(SettingsState.waiting_for_gpt_on)
    elif action == "edit_gpt_off": await state.set_state(SettingsState.waiting_for_gpt_off)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="open_settings")]])
    await callback.message.edit_text("📝 Отправьте новый текст шаблона в ответ на это сообщение:", reply_markup=kb)
    await callback.answer()

@router.message(StateFilter(SettingsState.waiting_for_mute_on, SettingsState.waiting_for_mute_off, SettingsState.waiting_for_gpt_on, SettingsState.waiting_for_gpt_off), F.chat.type == "private")
async def process_new_template(message: Message, state: FSMContext):
    current_state = await state.get_state()
    key_map = {
        SettingsState.waiting_for_mute_on.state: "mute_on_text",
        SettingsState.waiting_for_mute_off.state: "mute_off_text",
        SettingsState.waiting_for_gpt_on.state: "gpt_on_text",
        SettingsState.waiting_for_gpt_off.state: "gpt_off_text"
    }
    key = key_map.get(current_state)
    if key:
        await database.update_setting(message.from_user.id, key, message.text)
        await message.answer("✅ Шаблон успешно изменен!")
    await state.clear()

@router.business_connection()
async def on_business_connection(connection):
    if not connection.is_enabled:
        logging.info(f"Business connection disabled by {connection.user.id}")
        if connection.user.id in user_keys:
            del user_keys[connection.user.id]
        try:
            await bot.send_message(chat_id=connection.user.id, text="⚠️ <b>Внимание!</b> Бот был отключен от вашего аккаунта Telegram Business.\n\nВсе автоматические функции приостановлены.", parse_mode=ParseMode.HTML)
        except Exception:
            pass
    else:
        logging.info(f"New business connection from {connection.user.id}")
        await database.save_connection(connection.user.id, connection.id)
        key = database.derive_key(connection.user.id, connection.id)
        user_keys[connection.user.id] = key



@router.business_message()
async def on_business_message(message: Message):
    if not message.business_connection_id:
        return
        
    business_user_id = await get_business_user_id(message.business_connection_id)
    if not business_user_id:
        return
    text = message.text or message.caption or ""
    
    if message.from_user and message.from_user.id != business_user_id:
        if not (text and text.startswith('.')):
            saved_connection_id = await database.get_mute_connection_id(message.chat.id, message.chat.id)
            if saved_connection_id:
                try:
                    mute_info = await database.get_mute_info(message.chat.id)
                    remaining = max(0, int((mute_info["expires_at"] - time.time()) / 60)) if mute_info else 0
                    uname = mute_info["username"] if mute_info else (message.from_user.username or message.from_user.first_name)
                    
                    caption = (f"🔇 <b>Сообщение от @{uname}, находящегося в муте</b>\n"
                               f"⌛ <b>Оставшееся время мута:</b> {remaining} минут(ы).\n\n"
                               f"Текст: {text}")
                    
                    if message.photo or getattr(message, 'video', None) or getattr(message, 'video_note', None) or getattr(message, 'voice', None) or getattr(message, 'audio', None) or getattr(message, 'document', None):
                        try: await bot.copy_message(chat_id=business_user_id, from_chat_id=message.chat.id, message_id=message.message_id, caption=caption, parse_mode=ParseMode.HTML)
                        except Exception: pass
                    else:
                        try: await bot.send_message(chat_id=business_user_id, text=caption, parse_mode=ParseMode.HTML)
                        except Exception: pass
                        
                    await message.bot.delete_business_messages(
                        business_connection_id=saved_connection_id,
                        message_ids=[message.message_id]
                    )
                    return
                except Exception as e:
                    return

    key = await get_user_key(business_user_id)
    if not key:
        return

    if message.from_user and message.from_user.id == business_user_id:
        if text.startswith(".ask "):
            question = text[5:].strip()
            if not question:
                return
            
            try:
                await bot.edit_message_text(
                    text="⏳ Пишу ответ...",
                    chat_id=message.chat.id,
                    message_id=message.message_id,
                    business_connection_id=message.business_connection_id
                )
            except Exception as e:
                logging.error(f"Ошибка {type(e).__name__}: {e}")
                
            
            accumulated_text = ""
            start_time = time.time()
            last_edit_time = 0
            
            payload = {
                "model": AI_MODEL,
                "messages": [
                    {"role": "system", "content": f"Ты дружелюбный ИИ ассистент. Отвечай коротко, понятно и с уместным юмором. Твой ответ строго ограничен: не пиши длиннее чем 3000 символов с учетом пробелов. Используй HTML теги. Текущее время: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}."},
                    {"role": "user", "content": question}
                ],
                "temperature": 0.7,
                "max_tokens": 1024,
                "stream": True
            }
            
            excluded_keys = set()
            for attempt in range(3):
                api_key = api_manager.get_least_loaded_key(excluded_keys)
                if not api_key: break
                
                api_manager.acquire(api_key)
                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream"
                }
                
                try:
                    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None)) as session:
                        async with session.post(INVOKE_URL, headers=headers, json=payload, proxy=PROXY_URL) as resp:
                            if resp.status == 429 or resp.status >= 400:
                                logging.warning(f"Key {api_key[:10]}... returned {resp.status}. Failover activated.")
                                api_manager.penalize(api_key)
                                excluded_keys.add(api_key)
                                continue
                            
                            buffer = ""
                            done = False
                            while not done:
                                try:
                                    chunk = await asyncio.wait_for(resp.content.readany(), timeout=60.0)
                                    if not chunk: break
                                    buffer += chunk.decode('utf-8', errors='ignore')
                                    while '\n' in buffer:
                                        line, buffer = buffer.split('\n', 1)
                                        line = line.strip()
                                        if line.startswith('data: '):
                                            data_str = line[6:].strip()
                                            if data_str == '[DONE]':
                                                done = True
                                                break
                                            try:
                                                data_json = json.loads(data_str)
                                                if 'choices' in data_json and len(data_json['choices']) > 0:
                                                    content = data_json['choices'][0].get('delta', {}).get('content', '')
                                                    if content:
                                                        accumulated_text += content
                                            except Exception: pass
                                except asyncio.TimeoutError:
                                    accumulated_text += "\n\n🤖 ИИ прервал ответ (долго отвечал)"
                                    break
                                    
                                curr = time.time()
                                if curr - start_time > 60:
                                    accumulated_text += "\n\n🤖 ИИ прервал ответ (долго отвечал)"
                                    break
                                    
                                if curr - last_edit_time > 1.0:
                                    if accumulated_text:
                                        step = (int((curr * 10) % 3) + 1)
                                        noise = "▓" if step >= 1 else ""
                                        noise += "░" if step >= 2 else ""
                                        disp = accumulated_text + noise + "\n\n🤖 ИИ пишет."
                                        try:
                                            await bot.edit_message_text(text=disp, chat_id=message.chat.id, message_id=message.message_id, business_connection_id=message.business_connection_id, parse_mode=ParseMode.HTML)
                                            last_edit_time = curr
                                        except Exception: pass
                            break
                except Exception as e:
                    logging.warning(f"Error with key {api_key[:10]}...: {e}")
                    excluded_keys.add(api_key)
                finally:
                    api_manager.release(api_key)
                    
            if accumulated_text:
                    try: await bot.edit_message_text(text=accumulated_text, chat_id=message.chat.id, message_id=message.message_id, business_connection_id=message.business_connection_id, parse_mode=ParseMode.HTML)
                    except Exception: pass
            
            return
              
        if text.strip() == ".gpt":
            is_active, _, exp_at, first_sent = await database.get_ai_session(message.chat.id)
            cd_until = await database.get_cooldown(business_user_id)
            curr_t = int(time.time())
            if not is_active and cd_until > curr_t and message.from_user.id != OWNER_ID:
                try: await bot.edit_message_text(text=f"❌ Режим ИИ на кулдауне. Подождите ещё {cd_until - curr_t} сек.", chat_id=message.chat.id, message_id=message.message_id, business_connection_id=message.business_connection_id)
                except Exception as e: logging.error(f"Ошибка {type(e).__name__}: {e}")
                return
            
            settings = await database.get_settings(business_user_id)
            if is_active:
                await database.set_ai_session(message.chat.id, 0, [], 0, 0)
                if message.from_user.id != OWNER_ID:
                    await database.set_cooldown(business_user_id, int(time.time()) + 120)
                if message.chat.id in active_ai_tasks:
                    if not active_ai_tasks[message.chat.id].done():
                        active_ai_tasks[message.chat.id].cancel()
                    del active_ai_tasks[message.chat.id]
                try: await bot.edit_message_text(text=settings["gpt_off_text"], chat_id=message.chat.id, message_id=message.message_id, business_connection_id=message.business_connection_id)
                except Exception as e: logging.error(f"Ошибка {type(e).__name__}: {e}")
            else:
                await database.set_ai_session(message.chat.id, 1, [], 0, 0)
                try: await bot.edit_message_text(text=settings["gpt_on_text"], chat_id=message.chat.id, message_id=message.message_id, business_connection_id=message.business_connection_id)
                except Exception as e: logging.error(f"Ошибка {type(e).__name__}: {e}")
            return
        elif text.strip() == ".gpt off":
            await database.set_ai_session(message.chat.id, 0, [], 0, 0)
            if message.from_user.id != OWNER_ID:
                await database.set_cooldown(business_user_id, int(time.time()) + 120)
            if message.chat.id in active_ai_tasks:
                if not active_ai_tasks[message.chat.id].done():
                    active_ai_tasks[message.chat.id].cancel()
                del active_ai_tasks[message.chat.id]
            settings = await database.get_settings(business_user_id)
            try: await bot.edit_message_text(text=settings["gpt_off_text"], chat_id=message.chat.id, message_id=message.message_id, business_connection_id=message.business_connection_id)
            except Exception as e: logging.error(f"Ошибка {type(e).__name__}: {e}")
            return
            
        if text.strip() == ".mute off":
            username = message.chat.username or message.chat.first_name or "собеседника"
            await database.remove_mute(message.chat.id)
            settings = await database.get_settings(business_user_id)
            try:
                await bot.edit_message_text(
                    text=settings["mute_off_text"],
                    chat_id=message.chat.id,
                    message_id=message.message_id,
                    business_connection_id=message.business_connection_id
                )
            except Exception as e:
                logging.error(f"Ошибка {type(e).__name__}: {e}")
            try:
                await bot.send_message(
                    chat_id=business_user_id,
                    text=f"❌ Мут отключен.\nТеперь сообщения от собеседника @{username} не будут автоматически удаляться в этом чате."
                )
            except Exception as e:
                logging.error(f"Ошибка {type(e).__name__}: {e}")
            return
        elif text.strip().startswith(".mute"):
            is_currently_muted = await database.is_muted(message.chat.id, message.chat.id)
            username = message.chat.username or message.chat.first_name or "собеседника"
            settings = await database.get_settings(business_user_id)
            
            if is_currently_muted:
                await database.remove_mute(message.chat.id)
                try:
                    await bot.edit_message_text(
                        text=settings["mute_off_text"],
                        chat_id=message.chat.id,
                        message_id=message.message_id,
                        business_connection_id=message.business_connection_id
                    )
                except Exception as e:
                    logging.error(f"Ошибка {type(e).__name__}: {e}")
                try:
                    await bot.send_message(chat_id=business_user_id, text=f"❌ Мут отключен.\nТеперь сообщения от собеседника @{username} не будут автоматически удаляться в этом чате.")
                except Exception: pass
                return

            args = text.strip().split()
            minutes = 60
            if len(args) > 1 and args[1].isdigit():
                minutes = int(args[1])
            if minutes > 60:
                minutes = 60
                
            interlocutor_id = message.chat.id
            
            await database.add_mute(message.chat.id, interlocutor_id, username, message.business_connection_id, minutes)
            
            reply_text_chat = f"✅ Мут включен.\nВас замутили на {minutes} Минут(ы)."
            reply_text_dm = f"✅ Мут включен.\nТеперь все сообщения от собеседника @{username} будут автоматически удаляться в этом чате в течение {minutes} минут(ы)."
            try:
                await bot.edit_message_text(
                    text=reply_text_chat,
                    chat_id=message.chat.id,
                    message_id=message.message_id,
                    business_connection_id=message.business_connection_id
                )
            except Exception as e:
                logging.error(f"Ошибка {type(e).__name__}: {e}")
            
            try:
                await bot.send_message(chat_id=business_user_id, text=reply_text_dm)
            except Exception as e:
                logging.error(f"Ошибка {type(e).__name__}: {e}")
            return
        elif text.strip().startswith(".notification"):
            custom_text = text.strip()[13:].strip()
            username = message.chat.username or message.chat.first_name or "собеседником"
            if custom_text:
                await database.set_custom_notification(message.chat.id, custom_text)
                try: await bot.edit_message_text(text="✅ Для этого чата установлен кастомный Авто-ответчик.", chat_id=message.chat.id, message_id=message.message_id, business_connection_id=message.business_connection_id)
                except Exception: pass
                try: await bot.send_message(chat_id=business_user_id, text=f"✅ Для этого чата установлен кастомный Авто-ответчик.\n\n👤 Чат: @{username}\n📝 Текст: {custom_text}")
                except Exception: pass
            else:
                await database.set_custom_notification(message.chat.id, None)
                try: await bot.edit_message_text(text="🔄 Текст Авто-ответчика сброшен.", chat_id=message.chat.id, message_id=message.message_id, business_connection_id=message.business_connection_id)
                except Exception: pass
                try: await bot.send_message(chat_id=business_user_id, text=f"🔄 Текст Авто-ответчика сброшен.\n\n👤 Чат: @{username}")
                except Exception: pass
            return
        elif text.strip() == ".help":
            if message.from_user.id == OWNER_ID:
                bot_username = (await bot.get_me()).username
                gpt_help = f"⚠️ Кулдаун после использования режима ИИ: 2 минуты на весь аккаунт. (Для этого Юзера не распространяется т.к для него включен иммунитет создателя бота: @{bot_username})\n\n"
            else:
                gpt_help = "⚠️ Кулдаун после использования режима ИИ: 2 минуты на весь аккаунт.\n\n"
                
            help_text = (
                "🛠️ <b>МЕНЮ КОМАНД БИЗНЕС-БОТА</b>\n\n"
                "🔹 <b>МОДЕРАЦИЯ И ЧАТ:</b>\n"
                "• <code>.mute [минуты]</code> — Замутить собеседника. Максимум: 60 мин.\n"
                "• <code>.mute off</code> — Снять мут с собеседника.\n"
                "• <code>.notification [текст]</code> — Установить локальную заглушку для чата (без текста - сбросить).\n\n"
                "🤖 <b>ИИ-АССИСТЕНТ:</b>\n"
                "• <code>.ask [вопрос]</code> — Разовый вопрос к ИИ (без сохранения контекста и без ограничений по использованию).\n"
                "• <code>.gpt</code> — Включить приватную ИИ-комнату на 10 минут (память диалога, общая беседа, поддержка фото).\n"
                "• <code>.gpt off</code> — Экстренно выключить ИИ-режим и прервать генерацию.\n"
                f"{gpt_help}"
                "⚡ <b>УТИЛИТЫ И ТЕСТЫ:</b>\n"
                "• <code>.ping</code> — Замерить задержку доставки и пинг до серверов Telegram.\n"
                "• <code>.typing [текст]</code> — Красивая анимация проявления текста.\n"
                "• <code>.help</code> — Вызвать это меню."
            )
            try:
                await bot.edit_message_text(
                    text=help_text,
                    chat_id=message.chat.id,
                    message_id=message.message_id,
                    business_connection_id=message.business_connection_id,
                    parse_mode=ParseMode.HTML
                )
            except Exception as e:
                logging.error(f"Ошибка .help: {e}")
            return
        elif text.strip() == ".ping":
            update_delivery = int((time.time() - message.date.timestamp()) * 1000)
            
            t1 = time.time()
            await bot.get_me()
            t2 = time.time()
            
            rtt = int((t2 - t1) * 1000)
            avg_tg = int(rtt / 2)
            total_ping = update_delivery + avg_tg
            
            stats_text = (
                f"🏓 <b>Ping Stats</b>\n\n"
                f"🌐 <b>Бот ⇆ TG:</b> {avg_tg} ms <i>(RTT: {rtt} ms)</i>\n"
                f"👤 <b>Юзер ⇆ Бот:</b> ~{update_delivery} ms\n\n"
                f"⚡ <b>Общий пинг (Юзер ⇆ Бот ⇆ TG):</b> {total_ping} ms"
            )
            
            await bot.edit_message_text(
                text=stats_text,
                chat_id=message.chat.id,
                message_id=message.message_id,
                business_connection_id=message.business_connection_id,
                parse_mode=ParseMode.HTML
            )
            return
        elif text.strip().startswith(".typing "):
            final_text = text.strip()[8:].strip()
            if not final_text:
                return
                
            
            text_len = len(final_text)
            step_size = 4
            
            for i in range(0, text_len + step_size, step_size):
                if i > text_len:
                    i = text_len
                    
                revealed_clean = final_text[:i]
                remaining = text_len - i
                
                if remaining > 0:
                    noise_str = ""
                    if remaining >= 1: noise_str += "▓"
                    if remaining >= 2: noise_str += "░"
                    current_text = revealed_clean + noise_str
                else:
                    current_text = final_text
                
                try:
                    await bot.edit_message_text(
                        text=current_text,
                        chat_id=message.chat.id,
                        message_id=message.message_id,
                        business_connection_id=message.business_connection_id
                    )
                    if remaining > 0:
                        await asyncio.sleep(1.0)
                except TelegramAPIError:
                    pass
                
                if remaining <= 0:
                    break
            return
            
    sender_name = message.from_user.full_name if message.from_user else "Unknown"
    
    if (text or message.photo) and not (text and text.startswith(".")):
        is_active, context, exp_at, first_sent = await database.get_ai_session(message.chat.id)
        curr_time = int(time.time())
        if is_active == 1:
            if exp_at > 0 and curr_time > exp_at:
                if message.chat.id not in active_ai_tasks or active_ai_tasks[message.chat.id].done():
                    await database.set_cooldown(business_user_id, curr_time + 120)
                    await database.set_ai_session(message.chat.id, 0, [], 0, 0)
                    try: await bot.send_message(chat_id=message.chat.id, text="❌ Режим ИИ отключён.\nЯ больше не буду отвечать на ваши вопросы(", business_connection_id=message.business_connection_id)
                    except Exception: pass
            else:
                if message.chat.id in active_ai_tasks:
                    if not active_ai_tasks[message.chat.id].done():
                        return
                    else:
                        del active_ai_tasks[message.chat.id]
                        
                msg_time = datetime.fromtimestamp(message.date.timestamp()).strftime('%d.%m.%Y %H:%M')
                
                if message.photo:
                    import io
                    import base64
                    file_info = await bot.get_file(message.photo[-1].file_id)
                    file_bytes = io.BytesIO()
                    await bot.download_file(file_info.file_path, file_bytes)
                    b64_str = await asyncio.to_thread(lambda b: base64.b64encode(b.getvalue()).decode('utf-8'), file_bytes)
                    msg_content = [
                        {"type": "text", "text": text or "Посмотри на это изображение."},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_str}"}}
                    ]
                    context.append({"role": "user", "content": msg_content})
                else:
                    context.append({"role": "user", "content": f"{sender_name} [{msg_time}]: {text}"})
                    
                await database.set_ai_session(message.chat.id, 1, context, exp_at, first_sent)
                try:
                    reply_msg = await bot.send_message(chat_id=message.chat.id, text="⏳ Пишу ответ...", reply_to_message_id=message.message_id, business_connection_id=message.business_connection_id)
                    
                    async def gpt_stream_task(chat_id, context_list, reply_id, connection_id):
                        curr_time_str = datetime.now().strftime('%d.%m.%Y %H:%M:%S')
                        sys_prompt = {"role": "system", "content": f"Ты дружелюбный ИИ ассистент. Отвечай коротко, понятно и с уместным юмором. Твой ответ строго ограничен: не пиши длиннее чем 3000 символов с учетом пробелов. Используй только безопасные HTML теги для форматирования (<b>, <i>, <code>, <pre>).\nКРИТИЧЕСКОЕ ПРАВИЛО: Текущее реальное системное время: {curr_time_str}. Если пользователь спрашивает сколько сейчас времени или какой день, отвечай строго на основе этого значения. Никогда не говори фраз в духе 'у тебя же в сообщении написано' или 'как указано в твоем логе' — отвечай так, будто ты сам смотришь на часы в этот самый момент."}
                        api_context = [sys_prompt] + context_list
                        payload = {"model": AI_MODEL, "messages": api_context, "temperature": 0.7, "max_tokens": 1024, "stream": True}
                        accumulated_text = ""
                        start_time = time.time()
                        last_edit = 0
                        try:
                            excluded_keys = set()
                            for attempt in range(3):
                                api_key = api_manager.get_least_loaded_key(excluded_keys)
                                if not api_key: break
                                
                                api_manager.acquire(api_key)
                                headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "Accept": "text/event-stream"}
                                
                                try:
                                    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None)) as session:
                                        async with session.post(INVOKE_URL, headers=headers, json=payload, proxy=PROXY_URL) as resp:
                                            if resp.status == 429 or resp.status >= 400:
                                                logging.warning(f"Key {api_key[:10]}... returned {resp.status}. Failover activated.")
                                                api_manager.penalize(api_key)
                                                excluded_keys.add(api_key)
                                                continue
                                                
                                            buffer = ""
                                            done = False
                                            while not done:
                                                try:
                                                    chunk = await asyncio.wait_for(resp.content.readany(), timeout=60.0)
                                                    if not chunk: break
                                                    buffer += chunk.decode('utf-8', errors='ignore')
                                                    while '\n' in buffer:
                                                        line, buffer = buffer.split('\n', 1)
                                                        line = line.strip()
                                                        if line.startswith('data: '):
                                                            data_str = line[6:].strip()
                                                            if data_str == '[DONE]':
                                                                done = True
                                                                break
                                                            try:
                                                                data_json = json.loads(data_str)
                                                                if 'choices' in data_json and len(data_json['choices']) > 0:
                                                                    content = data_json['choices'][0].get('delta', {}).get('content', '')
                                                                    if content:
                                                                        accumulated_text += content
                                                            except Exception: pass
                                                except asyncio.TimeoutError:
                                                    accumulated_text += "\n\n🤖 ИИ прервал ответ (долго отвечал)"
                                                    break
                                                    
                                                curr = time.time()
                                                if curr - start_time > 60:
                                                    accumulated_text += "\n\n🤖 ИИ прервал ответ (долго отвечал)"
                                                    break
                                                    
                                                if curr - last_edit > 1.0:
                                                    if accumulated_text:
                                                        step = (int((curr * 10) % 3) + 1)
                                                        noise = "▓" if step >= 1 else ""
                                                        noise += "░" if step >= 2 else ""
                                                        disp = accumulated_text + noise + "\n\n🤖 ИИ пишет."
                                                        try:
                                                            await bot.edit_message_text(text=disp, chat_id=chat_id, message_id=reply_id, business_connection_id=connection_id, parse_mode=ParseMode.HTML)
                                                            last_edit = curr
                                                        except Exception: pass
                                            break
                                except Exception as e:
                                    logging.warning(f"Error with key {api_key[:10]}...: {e}")
                                    excluded_keys.add(api_key)
                                finally:
                                    api_manager.release(api_key)
                            if accumulated_text:
                                try: await bot.edit_message_text(text=accumulated_text, chat_id=chat_id, message_id=reply_id, business_connection_id=connection_id, parse_mode=ParseMode.HTML)
                                except Exception: pass
                                is_a, ctx, c_exp, c_fs = await database.get_ai_session(chat_id)
                                if is_a:
                                    now = int(time.time())
                                    if not c_fs:
                                        c_exp = now + 300
                                        c_fs = 1
                                    ctx.append({"role": "assistant", "content": accumulated_text})
                                    if c_exp > 0 and now > c_exp:
                                        await database.set_cooldown(business_user_id, now + 120)
                                        await database.set_ai_session(chat_id, 0, [], 0, 0)
                                        try: await bot.send_message(chat_id=chat_id, text="❌ Режим ИИ отключён.\nЯ больше не буду отвечать на ваши вопросы(", business_connection_id=connection_id)
                                        except Exception: pass
                                    else:
                                        await database.set_ai_session(chat_id, 1, ctx, c_exp, c_fs)
                        except asyncio.CancelledError:
                            try: await bot.edit_message_text(text="❌ Ответ остановлен.", chat_id=chat_id, message_id=reply_id, business_connection_id=connection_id)
                            except Exception: pass
                            raise
                        except Exception as e:
                            try: await bot.edit_message_text(text="Произошла ошибка связи с API.", chat_id=chat_id, message_id=reply_id, business_connection_id=connection_id)
                            except Exception: pass
                        finally:
                            if chat_id in active_ai_tasks:
                                del active_ai_tasks[chat_id]

                    active_ai_tasks[message.chat.id] = asyncio.create_task(gpt_stream_task(message.chat.id, context, reply_msg.message_id, message.business_connection_id))
                except Exception as e:
                    logging.error(f"Ошибка {type(e).__name__}: {e}")
    
    if message.from_user and message.from_user.id != business_user_id:
        if not (text and text.startswith(".")):
            _is_ai, _, _, _ = await database.get_ai_session(message.chat.id)
            if not _is_ai:
                last_mgr = await database.get_last_manager_reply_at(message.chat.id)
                curr = int(time.time())
                if curr - last_mgr >= 1800:
                    try:
                        settings = await database.get_settings(business_user_id)
                        if settings["manager_notifications"]:
                            custom_notif = await database.get_custom_notification(message.chat.id)
                            reply_text = custom_notif if custom_notif else "Передам ваше сообщение, ждите пока ответят)"
                            await bot.send_message(
                                chat_id=message.chat.id,
                                text=reply_text,
                                business_connection_id=message.business_connection_id
                            )
                        await database.update_last_manager_reply_at(message.chat.id, curr)
                        
                        import html
                        sender_name = message.from_user.first_name
                        if message.from_user.username:
                            sender_name = f"@{message.from_user.username}"
                        user_link = f"tg://user?id={message.from_user.id}"
                        safe_text = html.escape(text) if text else "Медиа/Голосовое сообщение"
                        notify_msg = f"🔔 <b>Вам оставили сообщение!</b>\nОт: <a href='{user_link}'>{html.escape(sender_name)}</a>\n\n<i>{safe_text}</i>"
                        try:
                            await bot.send_message(chat_id=business_user_id, text=notify_msg, parse_mode="HTML")
                        except Exception as e:
                            logging.error(f"Не удалось отправить уведомление менеджеру: {e}")
                            
                    except Exception as e:
                        logging.error(f"Ошибка автоответчика: {e}")
    
    file_id = None
    if message.photo: file_id = f"photo:{message.photo[-1].file_id}"
    elif getattr(message, "video_note", None): file_id = f"video_note:{message.video_note.file_id}"
    elif message.video: file_id = f"video:{message.video.file_id}"
    elif message.document: file_id = f"document:{message.document.file_id}"
    elif message.voice: file_id = f"voice:{message.voice.file_id}"
    elif message.audio: file_id = f"audio:{message.audio.file_id}"
        
    await database.save_message(
        message_id=message.message_id,
        chat_id=message.chat.id,
        business_user_id=business_user_id,
        sender_id=message.from_user.id if message.from_user else 0,
        text=text,
        sender_name=sender_name,
        media_file_id=file_id,
        timestamp=int(message.date.timestamp()),
        key=key
    )

@router.edited_business_message()
async def on_edited_business_message(message: Message):
    if not message.business_connection_id: return
    business_user_id = await get_business_user_id(message.business_connection_id)
    if not business_user_id: return
    key = await get_user_key(business_user_id)
    if not key: return
    old_msg = await database.get_message(message.message_id, message.chat.id)
    if not old_msg: return
        
    old_text = await asyncio.to_thread(database.decrypt_text, old_msg["encrypted_text"], key)
    sender_name = await asyncio.to_thread(database.decrypt_text, old_msg["encrypted_sender_name"], key)
    new_text = message.text or message.caption or ""
    
    if old_text == new_text and not old_msg.get("media_file_id"): return
        
    escaped_sender = html.escape(sender_name)
    escaped_old = html.escape(old_text) if old_text else ""
    escaped_new = html.escape(new_text) if new_text else ""
    user_link = f"tg://user?id={message.from_user.id}" if message.from_user else ""
    
    report_text = f"✏️ Изменено сообщение пользователя <a href='{user_link}'>{escaped_sender}</a>\n\n"
    if old_text != new_text: report_text += f"Старый текст: {escaped_old}\nНовый текст: {escaped_new}"
    else: report_text += f"Текст: {escaped_old}\nМедиафайл был изменен."
        
    media_info = None
    if old_msg.get("media_file_id"):
        media_info = await asyncio.to_thread(database.decrypt_text, old_msg["media_file_id"], key)
        
    try:
        if media_info and ":" in media_info:
            m_type, old_file_id = media_info.split(":", 1)
            try:
                if m_type == "photo": await bot.send_photo(chat_id=business_user_id, photo=old_file_id, caption=report_text, parse_mode=ParseMode.HTML)
                elif m_type == "video": await bot.send_video(chat_id=business_user_id, video=old_file_id, caption=report_text, parse_mode=ParseMode.HTML)
                elif m_type == "video_note":
                    await bot.send_message(business_user_id, report_text, parse_mode=ParseMode.HTML)
                    await bot.send_video_note(chat_id=business_user_id, video_note=old_file_id)
                elif m_type == "voice": await bot.send_voice(chat_id=business_user_id, voice=old_file_id, caption=report_text, parse_mode=ParseMode.HTML)
                elif m_type == "audio": await bot.send_audio(chat_id=business_user_id, audio=old_file_id, caption=report_text, parse_mode=ParseMode.HTML)
                else: await bot.send_document(chat_id=business_user_id, document=old_file_id, caption=report_text, parse_mode=ParseMode.HTML)
            except Exception as e:
                await bot.send_document(chat_id=business_user_id, document=old_file_id, caption=report_text, parse_mode=ParseMode.HTML)
        else:
            if old_text != new_text:
                await bot.send_message(business_user_id, report_text, parse_mode=ParseMode.HTML)
    except Exception as e:
        pass
        
    _is_ai, _, _, _ = await database.get_ai_session(message.chat.id)
    if not _is_ai and message.from_user and message.from_user.id != business_user_id:
        last_mgr = await database.get_last_manager_reply_at(message.chat.id)
        curr = int(time.time())
        if curr - last_mgr >= 1800:
            try:
                settings = await database.get_settings(business_user_id)
                if settings["manager_notifications"]:
                    delay = curr - int(message.date.timestamp())
                    custom_notif = await database.get_custom_notification(message.chat.id)
                    if delay > 60 or not custom_notif:
                        reply_text = "Передам ваше сообщение, ждите пока ответят)"
                    else:
                        reply_text = custom_notif
                        
                    await bot.send_message(
                        chat_id=message.chat.id,
                        text=reply_text,
                        business_connection_id=message.business_connection_id
                    )
                await database.update_last_manager_reply_at(message.chat.id, curr)
            except Exception as e:
                logging.error(f"Ошибка автоответчика (edit): {e}")

    new_file_id = None
    if message.photo: new_file_id = f"photo:{message.photo[-1].file_id}"
    elif getattr(message, "video_note", None): new_file_id = f"video_note:{message.video_note.file_id}"
    elif message.video: new_file_id = f"video:{message.video.file_id}"
    elif message.document: new_file_id = f"document:{message.document.file_id}"
    elif message.voice: new_file_id = f"voice:{message.voice.file_id}"
    elif message.audio: new_file_id = f"audio:{message.audio.file_id}"
        
    await database.save_message(
        message_id=message.message_id,
        chat_id=message.chat.id,
        business_user_id=business_user_id,
        sender_id=message.from_user.id if message.from_user else 0,
        text=new_text,
        sender_name=sender_name,
        media_file_id=new_file_id,
        timestamp=int(message.date.timestamp()),
        key=key
    )

@router.deleted_business_messages()
async def on_business_messages_deleted(deleted: BusinessMessagesDeleted):
    business_user_id = await get_business_user_id(deleted.business_connection_id)
    if not business_user_id: return
    key = await get_user_key(business_user_id)
    if not key: return
        
    for msg_id in deleted.message_ids:
        old_msg = await database.get_message(msg_id, deleted.chat.id)
        if old_msg:
            old_text = await asyncio.to_thread(database.decrypt_text, old_msg["encrypted_text"], key)
            sender_name = await asyncio.to_thread(database.decrypt_text, old_msg["encrypted_sender_name"], key)
            report_text = f"🗑 Удалено сообщение пользователя {sender_name}\n\nТекст сообщения: {old_text}"
            
            media_info = None
            if old_msg.get("media_file_id"): media_info = await asyncio.to_thread(database.decrypt_text, old_msg["media_file_id"], key)
            
            try:
                if media_info and ":" in media_info:
                    m_type, old_file_id = media_info.split(":", 1)
                    try:
                        if m_type == "photo": await bot.send_photo(chat_id=business_user_id, photo=old_file_id, caption=report_text)
                        elif m_type == "video": await bot.send_video(chat_id=business_user_id, video=old_file_id, caption=report_text)
                        elif m_type == "video_note":
                            await bot.send_message(business_user_id, report_text)
                            await bot.send_video_note(chat_id=business_user_id, video_note=old_file_id)
                        elif m_type == "voice": await bot.send_voice(chat_id=business_user_id, voice=old_file_id, caption=report_text)
                        elif m_type == "audio": await bot.send_audio(chat_id=business_user_id, audio=old_file_id, caption=report_text)
                        else: await bot.send_document(chat_id=business_user_id, document=old_file_id, caption=report_text)
                    except Exception as e:
                        await bot.send_document(chat_id=business_user_id, document=old_file_id, caption=report_text)
                else:
                    await bot.send_message(business_user_id, report_text)
            except Exception as e:
                pass

async def clean_old_messages_loop():
    while True:
        try:
            await database.cleanup_old_messages()
        except Exception as e:
            logging.error(f"Ошибка {type(e).__name__}: {e}")
        await asyncio.sleep(3600)

async def mute_updater_loop():
    while True:
        try:
            expired = await database.get_expired_mutes()
            for mute in expired:
                chat_id = mute["chat_id"]
                username = mute["username"]
                connection_id = mute["business_connection_id"]
                
                text_chat = "❌ Мут отключен.\nможете писать)"
                text_dm = f"❌ Мут отключен.\nТеперь сообщения от собеседника @{username} не будут автоматически удаляться в этом чате."
                
                try:
                    await bot.send_message(chat_id=chat_id, text=text_chat, business_connection_id=connection_id)
                except Exception as e:
                    logging.error(f"Ошибка {type(e).__name__}: {e}")
                
                owner_id = await database.get_user_id_by_connection(connection_id)
                if owner_id:
                    try:
                        await bot.send_message(chat_id=owner_id, text=text_dm)
                    except Exception as e:
                        logging.error(f"Ошибка {type(e).__name__}: {e}")
                
                await asyncio.sleep(0.1)
        except Exception as e:
            logging.error(f"Ошибка {type(e).__name__}: {e}")
        await asyncio.sleep(60)

ADMIN_ID = os.getenv("ADMIN_ID")

async def on_startup(bot: Bot):
    if ADMIN_ID:
        try:
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
            from aiogram.enums import ParseMode
            kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🌐 В главное меню", callback_data="open_settings")]])
            await bot.send_message(
                chat_id=int(ADMIN_ID),
                text="🔌 <b>Бот успешно подключен к вашему аккаунту!</b>\n\nВсе системы работают в штатном режиме через защищенный туннель.",
                parse_mode=ParseMode.HTML,
                reply_markup=kb
            )
        except Exception as e:
            logging.error(f"Failed to send startup message: {e}")

async def main():
    await database.init_db()
    dp.include_router(router)
    dp.startup.register(on_startup)
    asyncio.create_task(clean_old_messages_loop())
    asyncio.create_task(mute_updater_loop())
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        logging.error(f"Ошибка {type(e).__name__}: {e}")
    logging.info("Starting bot polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())