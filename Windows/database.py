import aiosqlite
import hashlib
import base64
import time
import os
from Crypto.Cipher import AES
import json
import asyncio

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_NAME = os.path.join(BASE_DIR, "messages.db")
_db_conn = None

def derive_key(user_id: int, connection_id: str) -> bytes:
    data = f"{user_id}:{connection_id}".encode('utf-8')
    return hashlib.sha256(data).digest()

def encrypt_text(text: str, key: bytes) -> str:
    if not text:
        return ""
    cipher = AES.new(key, AES.MODE_GCM)
    ciphertext, tag = cipher.encrypt_and_digest(text.encode('utf-8'))
    data = cipher.nonce + tag + ciphertext
    return base64.b64encode(data).decode('utf-8')

def decrypt_text(encrypted_text: str, key: bytes) -> str:
    if not encrypted_text:
        return ""
    try:
        data = base64.b64decode(encrypted_text.encode('utf-8'))
        nonce = data[:16]
        tag = data[16:32]
        ciphertext = data[32:]
        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        plaintext = cipher.decrypt_and_verify(ciphertext, tag)
        return plaintext.decode('utf-8')
    except Exception:
        return "<Не удалось расшифровать>"

def encrypt_bytes(data: bytes, key: bytes) -> bytes:
    cipher = AES.new(key, AES.MODE_GCM)
    ciphertext, tag = cipher.encrypt_and_digest(data)
    return cipher.nonce + tag + ciphertext

def decrypt_bytes(encrypted_data: bytes, key: bytes) -> bytes:
    nonce = encrypted_data[:16]
    tag = encrypted_data[16:32]
    ciphertext = encrypted_data[32:]
    cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
    return cipher.decrypt_and_verify(ciphertext, tag)

async def init_db():
    global _db_conn
    _db_conn = await aiosqlite.connect(DB_NAME)
    
    await _db_conn.execute('''
        CREATE TABLE IF NOT EXISTS business_users (
            user_id INTEGER PRIMARY KEY,
            connection_id TEXT,
            cooldown_until INTEGER DEFAULT 0
        )
    ''')
    try: await _db_conn.execute('ALTER TABLE business_users ADD COLUMN cooldown_until INTEGER DEFAULT 0')
    except Exception: pass
    await _db_conn.execute('''
        CREATE TABLE IF NOT EXISTS messages (
            message_id INTEGER,
            chat_id INTEGER,
            business_user_id INTEGER,
            sender_id INTEGER,
            encrypted_text TEXT,
            encrypted_sender_name TEXT,
            media_file_id TEXT,
            timestamp INTEGER,
            PRIMARY KEY (message_id, chat_id)
        )
    ''')
    try:
        await _db_conn.execute('ALTER TABLE messages ADD COLUMN sender_id INTEGER')
    except Exception:
        pass
        
    await _db_conn.execute('''
        CREATE TABLE IF NOT EXISTS muted_chats (
            chat_id INTEGER,
            user_id INTEGER,
            username TEXT,
            business_connection_id TEXT,
            expires_at INTEGER,
            PRIMARY KEY (chat_id, user_id)
        )
    ''')
    await _db_conn.execute('''
        CREATE TABLE IF NOT EXISTS ai_sessions (
            chat_id INTEGER PRIMARY KEY,
            is_active INTEGER,
            context_json TEXT,
            session_expires_at INTEGER,
            cooldown_until INTEGER,
            is_first_reply_sent INTEGER
        )
    ''')
    try: await _db_conn.execute('ALTER TABLE ai_sessions ADD COLUMN session_expires_at INTEGER')
    except Exception: pass
    try: await _db_conn.execute('ALTER TABLE ai_sessions ADD COLUMN cooldown_until INTEGER')
    except Exception: pass
    try: await _db_conn.execute('ALTER TABLE ai_sessions ADD COLUMN is_first_reply_sent INTEGER')
    except Exception: pass
    
    await _db_conn.execute('''
        CREATE TABLE IF NOT EXISTS manager_states (
            chat_id INTEGER PRIMARY KEY,
            last_manager_reply_at INTEGER DEFAULT 0
        )
    ''')
    try: await _db_conn.execute('ALTER TABLE manager_states ADD COLUMN custom_notification TEXT DEFAULT NULL')
    except Exception: pass
    
    await _db_conn.execute('''
        CREATE TABLE IF NOT EXISTS bot_settings (
            user_id INTEGER PRIMARY KEY,
            mute_on_text TEXT,
            mute_off_text TEXT,
            gpt_on_text TEXT,
            gpt_off_text TEXT,
            manager_notifications INTEGER DEFAULT 1
        )
    ''')
    
    await _db_conn.commit()

async def get_last_manager_reply_at(chat_id: int) -> int:
    if not _db_conn: return 0
    async with _db_conn.execute('SELECT last_manager_reply_at FROM manager_states WHERE chat_id = ?', (chat_id,)) as cursor:
        row = await cursor.fetchone()
        if row and row[0]:
            return row[0]
        return 0

async def update_last_manager_reply_at(chat_id: int, timestamp: int):
    if not _db_conn: return
    await _db_conn.execute(
        'INSERT INTO manager_states (chat_id, last_manager_reply_at) VALUES (?, ?) '
        'ON CONFLICT(chat_id) DO UPDATE SET last_manager_reply_at=excluded.last_manager_reply_at',
        (chat_id, timestamp)
    )
    await _db_conn.commit()

async def get_custom_notification(chat_id: int):
    if not _db_conn: return None
    async with _db_conn.execute('SELECT custom_notification FROM manager_states WHERE chat_id = ?', (chat_id,)) as cursor:
        row = await cursor.fetchone()
        if row: return row[0]
        return None

async def set_custom_notification(chat_id: int, text):
    if not _db_conn: return
    await _db_conn.execute(
        'INSERT INTO manager_states (chat_id, custom_notification) VALUES (?, ?) '
        'ON CONFLICT(chat_id) DO UPDATE SET custom_notification=excluded.custom_notification',
        (chat_id, text)
    )
    await _db_conn.commit()

async def save_connection(user_id: int, connection_id: str):
    await _db_conn.execute('''
        INSERT OR REPLACE INTO business_users (user_id, connection_id)
        VALUES (?, ?)
    ''', (user_id, connection_id))
    await _db_conn.commit()

async def get_connection(user_id: int):
    async with _db_conn.execute('SELECT connection_id FROM business_users WHERE user_id = ?', (user_id,)) as cursor:
        row = await cursor.fetchone()
        if row:
            return row[0]
        return None

async def get_user_id_by_connection(connection_id: str):
    async with _db_conn.execute('SELECT user_id FROM business_users WHERE connection_id = ?', (connection_id,)) as cursor:
        row = await cursor.fetchone()
        if row:
            return row[0]
        return None





async def save_message(message_id: int, chat_id: int, business_user_id: int, sender_id: int,
                       text: str, sender_name: str, media_file_id: str, 
                       timestamp: int, key: bytes):
    enc_text = await asyncio.to_thread(encrypt_text, text, key)
    enc_sender = await asyncio.to_thread(encrypt_text, sender_name, key)
    enc_media = await asyncio.to_thread(encrypt_text, media_file_id, key) if media_file_id else None
    
    await _db_conn.execute('''
        INSERT OR REPLACE INTO messages 
        (message_id, chat_id, business_user_id, sender_id, encrypted_text, encrypted_sender_name, media_file_id, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', (message_id, chat_id, business_user_id, sender_id, enc_text, enc_sender, enc_media, timestamp))
    await _db_conn.commit()

async def get_recent_interlocutor_messages(chat_id: int, business_user_id: int, limit: int = 100):
    msgs = []
    try:
        async with _db_conn.execute('SELECT message_id FROM messages WHERE chat_id = ? AND sender_id != ? ORDER BY timestamp DESC LIMIT ?', (chat_id, business_user_id, limit)) as cursor:
            rows = await cursor.fetchall()
            for r in rows:
                msgs.append(r[0])
    except Exception:
        pass
    return msgs

async def get_message(message_id: int, chat_id: int):
    _db_conn.row_factory = aiosqlite.Row
    async with _db_conn.execute('SELECT * FROM messages WHERE message_id = ? AND chat_id = ?', (message_id, chat_id)) as cursor:
        row = await cursor.fetchone()
        if row:
            return dict(row)
        return None

async def cleanup_old_messages(days: int = 3):
    cutoff_time = int(time.time()) - (days * 24 * 60 * 60)
    cursor = await _db_conn.execute('''
        SELECT m.media_file_id, m.business_user_id, b.connection_id 
        FROM messages m
        JOIN business_users b ON m.business_user_id = b.user_id
        WHERE m.timestamp < ? AND m.media_file_id IS NOT NULL
    ''', (cutoff_time,))
    
    rows = await cursor.fetchall()
    for row in rows:
        enc_path, user_id, connection_id = row
        key = derive_key(user_id, connection_id)
        try:
            decrypted_info = await asyncio.to_thread(decrypt_text, enc_path, key)
            if ":" in decrypted_info:
                _, file_path = decrypted_info.split(":", 1)
                if os.path.exists(file_path):
                    os.remove(file_path)
        except Exception:
            pass
            
    await _db_conn.execute('DELETE FROM messages WHERE timestamp < ?', (cutoff_time,))
    await _db_conn.commit()

async def add_mute(chat_id: int, user_id: int, username: str, connection_id: str, minutes: int):
    expires_at = int(time.time()) + (minutes * 60)
    await _db_conn.execute('''
        INSERT OR REPLACE INTO muted_chats (chat_id, user_id, username, business_connection_id, expires_at)
        VALUES (?, ?, ?, ?, ?)
    ''', (chat_id, user_id, username, connection_id, expires_at))
    await _db_conn.commit()

async def remove_mute(chat_id: int):
    await _db_conn.execute('DELETE FROM muted_chats WHERE chat_id = ?', (chat_id,))
    await _db_conn.commit()

async def is_muted(chat_id: int, user_id: int) -> bool:
    async with _db_conn.execute('SELECT expires_at FROM muted_chats WHERE chat_id = ? AND user_id = ?', (chat_id, user_id)) as cursor:
        row = await cursor.fetchone()
        if row:
            if int(time.time()) < row[0]:
                return True
            else:
                await _db_conn.execute('DELETE FROM muted_chats WHERE chat_id = ? AND user_id = ?', (chat_id, user_id))
                await _db_conn.commit()
        return False

async def get_mute_connection_id(chat_id: int, user_id: int):
    async with _db_conn.execute('SELECT business_connection_id, expires_at FROM muted_chats WHERE chat_id = ? AND user_id = ?', (chat_id, user_id)) as cursor:
        row = await cursor.fetchone()
        if row:
            if int(time.time()) < row[1]:
                return row[0]
        return None

async def get_mute_info(chat_id: int):
    async with _db_conn.execute("SELECT expires_at, username FROM muted_chats WHERE chat_id = ?", (chat_id,)) as cursor:
        row = await cursor.fetchone()
        if row:
            return {"expires_at": row[0], "username": row[1]}
        return None

async def get_expired_mutes():
    current_time = int(time.time())
    expired = []
    async with _db_conn.execute('SELECT chat_id, user_id, username, business_connection_id FROM muted_chats WHERE expires_at <= ?', (current_time,)) as cursor:
        rows = await cursor.fetchall()
        for row in rows:
            expired.append({
                "chat_id": row[0],
                "user_id": row[1],
                "username": row[2],
                "business_connection_id": row[3]
            })
    if expired:
        await _db_conn.execute('DELETE FROM muted_chats WHERE expires_at <= ?', (current_time,))
        await _db_conn.commit()
    return expired

async def set_ai_session(chat_id: int, is_active: int, context_list: list, session_expires_at: int = 0, is_first_reply_sent: int = 0):
    context_json = json.dumps(context_list, ensure_ascii=False)
    await _db_conn.execute('''
        INSERT OR REPLACE INTO ai_sessions (chat_id, is_active, context_json, session_expires_at, is_first_reply_sent)
        VALUES (?, ?, ?, ?, ?)
    ''', (chat_id, is_active, context_json, session_expires_at, is_first_reply_sent))
    await _db_conn.commit()

async def get_ai_session(chat_id: int):
    async with _db_conn.execute('SELECT is_active, context_json, session_expires_at, is_first_reply_sent FROM ai_sessions WHERE chat_id = ?', (chat_id,)) as cursor:
        row = await cursor.fetchone()
        if row:
            is_active = row[0]
            context_list = []
            if row[1]:
                try: context_list = json.loads(row[1])
                except Exception: pass
            return is_active, context_list, row[2] or 0, row[3] or 0
        return 0, [], 0, 0

async def get_cooldown(business_user_id: int) -> int:
    async with _db_conn.execute('SELECT cooldown_until FROM business_users WHERE user_id = ?', (business_user_id,)) as cursor:
        row = await cursor.fetchone()
        if row and row[0]:
            return row[0]
        return 0

async def set_cooldown(business_user_id: int, cooldown_until: int):
    await _db_conn.execute('UPDATE business_users SET cooldown_until = ? WHERE user_id = ?', (cooldown_until, business_user_id))
    await _db_conn.commit()

async def get_settings(user_id: int):
    default_settings = {
        "user_id": user_id,
        "mute_on_text": "❌ Мут включен.\nМолча удаляю все сообщения)",
        "mute_off_text": "❌ Мут отключен.\nможете писать)",
        "gpt_on_text": "✅ Режим ИИ включен.\nНапишите в чат и я отвечу.",
        "gpt_off_text": "❌ Режим ИИ отключён.\nЯ больше не буду отвечать на ваши вопросы(",
        "manager_notifications": 1
    }
    async with _db_conn.execute("SELECT * FROM bot_settings WHERE user_id = ?", (user_id,)) as cursor:
        row = await cursor.fetchone()
        if row:
            keys = ["user_id", "mute_on_text", "mute_off_text", "gpt_on_text", "gpt_off_text", "manager_notifications"]
            result = dict(zip(keys, row))
            for k, v in default_settings.items():
                if result.get(k) is None:
                    result[k] = v
            return result
        else:
            return default_settings

async def update_setting(user_id: int, key: str, value):
    valid_keys = ["mute_on_text", "mute_off_text", "gpt_on_text", "gpt_off_text", "manager_notifications"]
    if key not in valid_keys: return
    
    await _db_conn.execute("INSERT OR IGNORE INTO bot_settings (user_id) VALUES (?)", (user_id,))
    await _db_conn.execute(f"UPDATE bot_settings SET {key} = ? WHERE user_id = ?", (value, user_id))
    await _db_conn.commit()
