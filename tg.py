# -*- coding: utf-8 -*-
import asyncio
import random
import logging
import os
import re
from datetime import datetime, timezone
from time import monotonic
from typing import Any, Dict, Tuple
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, RPCError
import socks  # PySocks
from urllib.parse import urlparse, unquote
from telethon.network.connection.tcpabridged import ConnectionTcpAbridged
from telethon.tl.functions.account import GetAuthorizationsRequest
from telethon.tl.types import InputPeerUser, InputPeerChat, InputPeerChannel

# ─────────────────────────────────────
# LLM (через OpenRouter)
# ─────────────────────────────────────
from openai import OpenAI

OPENROUTER_API_KEY = "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"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
LLM_MODEL_NAME = "deepseek/deepseek-chat-v3-0324"

if not OPENROUTER_API_KEY:
    logging.warning("⚠️ OPENROUTER_API_KEY пуст.")

# ─────────────────────────────────────
# Общие настройки
# ─────────────────────────────────────
ACCOUNTS = [
    {
        "api_id": 28486483,
        "api_hash": "e3b71f6874229951ed5e406195cab4ad",
        "link": "https://t.me/+jTcOnkRy7TViYWFi", 
        "proxy_url": "socks5h://customer-kaminari_7YDmg-cc-de-city-frankfurt_am_main-sessid-0801840791-sesstime-30:RamiBabli15062007+@pr.oxylabs.io:7777"
    },
]

SESSION_FOLDER = "session"
os.makedirs(SESSION_FOLDER, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ─────────────────────────────────────
# Прокси
# ─────────────────────────────────────
def parse_proxy_url(url: str) -> Dict[str, Any]:
    u = urlparse(url)
    return {
        "proxy_type": socks.SOCKS5,
        "addr": u.hostname,
        "port": u.port,
        "rdns": (u.scheme.lower() == "socks5h"),
        "username": u.username,
        "password": unquote(u.password or ""),
    }

async def print_telegram_seen_ip(client: TelegramClient):
    try:
        auths = await client(GetAuthorizationsRequest())
        cur = next((a for a in auths.authorizations if getattr(a, "current", False)), None)
        if cur:
            print(f"🌐 Telegram видит IP: {cur.ip} | страна: {cur.country} | регион: {cur.region}")
        else:
            print("⚠️ Не удалось определить текущую авторизацию")
    except Exception as e:
        print(f"⚠️ Не удалось получить список авторизаций: {e}")

class PatchedAbridged(ConnectionTcpAbridged):
    """
    Прокидываем TCP через PySocks, не ломая Telethon.
    """
    async def _connect(self, timeout=None, ssl=None, **kwargs):
        loop = asyncio.get_running_loop()
        orig_open = asyncio.open_connection
        px = self._proxy or {}

        def _open_via_socks_blocking(host: str, port: int, timeout_val):
            s = socks.socksocket()
            s.set_proxy(
                px.get("proxy_type", socks.SOCKS5),
                px.get("addr"),
                px.get("port"),
                rdns=px.get("rdns", True),
                username=px.get("username"),
                password=px.get("password"),
            )
            s.settimeout(timeout_val or 15)
            s.connect((host, port))
            s.setblocking(False)
            return s

        async def patched_open_connection(*args, **kw):
            if kw.get("sock") is not None:
                return await orig_open(*args, **kw)
            if len(args) >= 2:
                host, port = args[0], args[1]
                ssl_val = kw.get("ssl", None)
            else:
                host = kw.get("host")
                port = kw.get("port")
                ssl_val = kw.get("ssl", None)
            sock = await loop.run_in_executor(None, _open_via_socks_blocking, host, port, timeout)
            return await orig_open(sock=sock, ssl=ssl_val)

        asyncio.open_connection = patched_open_connection
        try:
            await super()._connect(timeout=timeout, ssl=ssl, **kwargs)
        finally:
            asyncio.open_connection = orig_open

# ─────────────────────────────────────
# Очередь на все исходящие (твоя)
# ─────────────────────────────────────
class OutboxGate:
    def __init__(self, base_delay=6.0, priority_delay=2.0):
        self.queue = asyncio.Queue()
        self.base_delay = base_delay
        self.priority_delay = priority_delay
        asyncio.create_task(self._worker())

    async def send(self, coro_factory, priority=False):
        fut = asyncio.get_event_loop().create_future()
        await self.queue.put((priority, coro_factory, fut))
        return await fut

    async def _worker(self):
        while True:
            priority, coro_factory, fut = await self.queue.get()
            while True:
                try:
                    result = await coro_factory()
                    if not fut.done():
                        fut.set_result(result)
                    await asyncio.sleep(self.priority_delay if priority else self.base_delay)
                    break
                except FloodWaitError as e:
                    wait = int(getattr(e, "seconds", 3)) + random.uniform(0.2, 0.6)
                    print(f"⏳ Queue caught FloodWait {wait:.1f}s — retrying same job")
                    await asyncio.sleep(wait)
                    continue
                except RPCError as e:
                    backoff = 2.0 + random.uniform(0.2, 0.8)
                    print(f"⚠️ Queue RPCError {type(e).__name__}, retry in {backoff:.1f}s")
                    await asyncio.sleep(backoff)
                    continue
                except Exception as e:
                    if not fut.done():
                        fut.set_exception(e)
                    await asyncio.sleep(0.5)
                    break
            self.queue.task_done()

# ─────────────────────────────────────
# Хелперы
# ─────────────────────────────────────
def _is_input_peer(x): 
    return isinstance(x, (InputPeerUser, InputPeerChat, InputPeerChannel))

async def has_any_outgoing(client: TelegramClient, peer) -> bool:
    try:
        ent = peer if _is_input_peer(peer) else await client.get_input_entity(peer)
        msgs = await client.get_messages(ent, from_user='me', limit=1)
        return bool(msgs)
    except Exception as e:
        logging.warning(f"has_any_outgoing failed for {peer}: {e}")
        # лучше перестраховаться и не слать дубли, считаем что есть
        return True

async def is_ready(client: TelegramClient) -> bool:
    """
    Безопасная проверка «готовности» клиента (используется кликерами).
    """
    try:
        return bool(client.is_connected())
    except Exception:
        return False

# ─────────────────────────────────────
# LLM менеджер: история и ответы
# ─────────────────────────────────────
MAX_REPLIES = 7
FIRST_REPLY_DELAY_RANGE = (15, 400)  # задержка перед первым ответом от ИИ

SYSTEM_PROMPT_TEMPLATE = (
    "ТВОЯ РОЛЬ\n"
    "— Ты девушка 20 лет. Спокойная, дружелюбная. Ты не бот и не ассистент.\n"
    "— Пиши естественно, как в обычном чате с телефона.\n"
    "— Каждое твое сообщение — одна короткая строка (1 реплика). Никогда не склеивай несколько ответов в одно.\n"
    "— Никогда не используй смайлы/эмодзи, стикеры, «мм», междометия и канцелярит. Не ставь точку в конце. Допускаются только вопросительные знаки, запятые и краткие слова.\n"
    "— Если входящих от собеседника пришло несколько подряд, отвечай ТОЛЬКО на ПОСЛЕДНЕЕ из них. Остальные игнорируй.\n"
    "— Если не знаешь, что уместно ответить, верни пустой ответ (пустую строку), чтобы промолчать.\n"
    "— На грубость и провокации отвечай пустой строкой (игнор).\n"
    "\n"
    "ШАГИ СЦЕНАРИЯ (строго соблюдай порядок и количество)\n"
    "1) ПЕРВАЯ реплика от тебя: короткое ответное приветствие. Примеры: «привет», «хай», «приветик». Никаких дополнений.\n"
    "2) ВТОРАЯ реплика от тебя: скажи, что тебе нужно ненадолго отойти, и мягко предложи глянуть твой канал. Ссылку ставь В КОНЕЦ строки, без дополнительных слов после ссылки. Пример: «я на минутку отойду, можешь пока глянуть мой канал {channel_link}»\n"
    "3) ТРЕТЬЯ реплика: вернулась. Если есть новое сообщение собеседника — ответь на него кратко. Если новых сообщений нет — напиши «я вернулась». Одна строка.\n"
    "4) ДАЛЬШЕ (реплики 4–5): просто дружелюбно отвечай на входящие без инициативы. На комплимент отвечай взаимно. Примеры: «спасибо, ты тоже», «поняла», «окей», «да», «не совсем», «а ты откуда» — только если это прямой вопрос к тебе. Не задавай свои темы.\n"
    "5) РЕПЛИКА №5 (однократное напоминание о канале): один раз мягко напомни про канал КОРОТКО. Пример: «если что, мой канал тут {channel_link}». Больше ссылку не упоминай.\n"
    "6) РЕПЛИКА №6 (финал): спроси, заглянул ли он в канал, извинись и скажи, что снова нужно отойти. Всё в одной короткой строке. Пример: «заглянул в канал? мне нужно отойти, извини». После этого больше НЕ ОТВЕЧАЙ.\n"
    "\n"
    "СТИЛЬ И ОГРАНИЧЕНИЯ\n"
    "— Сообщения очень короткие, разговорные, без точек в конце.\n"
    "— Нулевая инициатива: не начинай новые темы, не предлагай действий, не проси денег/контактов, не давай советов.\n"
    "— Отвечай по смыслу ПОСЛЕДНЕГО входящего. Если там один смайлик/стикер/голосовое без текста — верни пустую строку (пропуск).\n"
    "— Никаких «позже допишу», «мм», «сори за молчание» и прочих филлеров — их запрещено использовать.\n"
    "— Если собеседник задаёт прямой вопрос (к тебе), отвечай кратко по делу. Если вопрос не к тебе — пропусти.\n"
    "— Ты ведёшь счёт СВОИХ реплик в этом диалоге по истории сообщений. Вторая — всегда с ссылкой, пятая — единственное напоминание, шестая — финал и тишина.\n"
)



class LLMChatManager:
    def __init__(self, channel_link: str):
        self.channel_link = channel_link
        self.histories: Dict[Tuple[str, int], list] = {}      # (session_name, chat_id) -> [messages]
        self.assistant_counts: Dict[Tuple[str, int], int] = {}# (session_name, chat_id) -> int
        self.client = OpenAI(api_key=OPENROUTER_API_KEY, base_url=OPENROUTER_BASE_URL)

    def _key(self, session_name, chat_id):
        return (session_name, chat_id)

    def build_system(self):
        return {"role": "system", "content": SYSTEM_PROMPT_TEMPLATE.format(channel_link=self.channel_link)}


    def _channel_hint(self, count: int):
        # Разрешаем упоминание канала один раз после 4-го ответа
        if count == 4:
            return f"Если уместно, мягко упомяни мой канал: {self.channel_link}"
        return "Не упоминай канал."

    def _trim(self, history):
        return history[-(MAX_REPLIES*2+6):]

    async def ask(self, session_name, chat_id, user_text: str) -> str:
        key = self._key(session_name, chat_id)
        history = self.histories.get(key, [])
        count = self.assistant_counts.get(key, 0)

        if count >= MAX_REPLIES:
            return ""  # молчим

        history.append({"role": "user", "content": user_text})
        history = self._trim(history)

        sys = self.build_system()
        channel_rule = {"role": "system", "content": self._channel_hint(count)}
        msgs = [sys, channel_rule] + history

        try:
            # Примечание: у openrouter .chat.completions.create обычно без параметра timeout.
            resp = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self.client.chat.completions.create(
                    model=LLM_MODEL_NAME,
                    messages=msgs,
                    temperature=0.7,
                    max_tokens=220,
                )
            )
            reply = (resp.choices[0].message.content or "").strip()
            if reply:
                history.append({"role": "assistant", "content": reply})
                self.assistant_counts[key] = count + 1
                self.histories[key] = history
            return reply
        except Exception as e:
            logging.error(f"LLM error: {type(e).__name__}: {e}")
            return "мм… чуть позже допишу"

    def note_assistant(self, session_name, chat_id, text: str):
        key = self._key(session_name, chat_id)
        history = self.histories.get(key, [])
        history.append({"role": "assistant", "content": text})
        self.assistant_counts[key] = self.assistant_counts.get(key, 0) + 1
        self.histories[key] = self._trim(history)

    def reset_chat(self, session_name, chat_id):
        key = self._key(session_name, chat_id)
        self.histories.pop(key, None)
        self.assistant_counts.pop(key, None)

# ─────────────────────────────────────
# Автоответчик: первый ответ теперь делает ИИ
# ─────────────────────────────────────
OFFLINE_THRESHOLD_MINUTES = 1  # флажок на будущее

answered_chats: Dict[int, Dict[str, Any]] = {}  # chat_id → {"stage": int, ...}

def register_auto_reply(client: TelegramClient, session_name: str, gate: OutboxGate, llm_mgr: LLMChatManager):
    @client.on(events.NewMessage(incoming=True))
    async def handler(event):
        # фильтры
        sender = await event.get_sender()
        if getattr(sender, "bot", False):
            return
        if event.is_group or event.is_channel or event.out:
            return

        chat_id = event.chat_id
        try:
            ent = await event.get_input_chat()
        except Exception as e:
            print(f"⚠️ Не удалось получить entity для {chat_id}: {e}")
            return

        async def process_first_message():
            try:
                delay = random.randint(*FIRST_REPLY_DELAY_RANGE)
                print(f"⏳ Ждём {delay} сек до первого LLM-ответа…")
                await asyncio.sleep(delay)

                if await has_any_outgoing(client, ent):
                    return

                user_text = (event.raw_text or "").strip()
                reply = await llm_mgr.ask(session_name, chat_id, user_text or " ")
                if not reply:
                    return

                async def _send():
                    if await has_any_outgoing(client, ent):
                        return None
                    return await client.send_message(ent, reply)

                result = await gate.send(_send, priority=True)
                if result is None:
                    return

                answered_chats[chat_id] = {"stage": 1, "first_sent_at": datetime.now(timezone.utc)}
                print(f"💬 Первый ответ LLM отправлен в чат {chat_id}")
            except Exception as e:
                print(f"⚠️ Ошибка при отправке первого LLM-ответа: {type(e).__name__}: {e}")
                answered_chats.pop(chat_id, None)

        async def process_followup_message(text: str):
            key = (session_name, chat_id)
            if llm_mgr.assistant_counts.get(key, 0) >= MAX_REPLIES:
                return

            # 👇 добавляем задержку как у первого
            delay = random.randint(*FIRST_REPLY_DELAY_RANGE)
            print(f"⏳ Ждём {delay} сек до ответа LLM…")
            await asyncio.sleep(delay)

            reply = await llm_mgr.ask(session_name, chat_id, text)
            if not reply:
                return

            try:
                await gate.send(lambda: client.send_message(ent, reply), priority=True)
                print(f"🤖 LLM → чат {chat_id}: {reply[:60]!r}")
            except Exception as e:
                print(f"⚠️ Ошибка при отправке LLM-ответа: {type(e).__name__}: {e}")


# ─────────────────────────────────────
# Рескан: теперь тоже через LLM, не через префаб
# ─────────────────────────────────────
async def rescan_dialogs_missing_replies(client: TelegramClient, gate: OutboxGate, llm_mgr: LLMChatManager, once=False):
    while True:
        try:
            cutoff = datetime.now(timezone.utc).timestamp() - 2*60*60  # 2 часа
            async for dlg in client.iter_dialogs():
                if not dlg.is_user:
                    continue
                if getattr(dlg.entity, "bot", False):
                    continue
                chat_id = dlg.id
                try:
                    ent = await client.get_input_entity(chat_id)
                except Exception as e:
                    print(f"⚠️ Не удалось получить entity в рескане для {chat_id}: {e}")
                    continue

                try:
                    if await has_any_outgoing(client, ent):
                        continue
                except Exception as e:
                    print(f"⚠️ Ошибка проверки исходящих (rescan): {e}")
                    continue

                if chat_id in answered_chats:
                    continue

                msgs = await client.get_messages(ent, limit=5)
                if not msgs:
                    continue
                last_in = next((m for m in msgs if not m.out), None)
                last_out = next((m for m in msgs if m.out), None)
                if not last_in:
                    continue
                if last_in.date.timestamp() < cutoff:
                    continue
                if last_out and last_out.date > last_in.date:
                    continue

                delay = random.randint(10, 60)

                async def delayed(ent=ent, cid=chat_id, last_in_text=(last_in.message or last_in.raw_text or "")):
                    await asyncio.sleep(delay)
                    try:
                        if await has_any_outgoing(client, ent):
                            answered_chats.pop(cid, None)
                            return

                        reply = await llm_mgr.ask(client.session.filename or "session", cid, (last_in_text or "").strip() or " ")
                        if not reply:
                            answered_chats.pop(cid, None)
                            return

                        await gate.send(lambda: client.send_message(ent, reply), priority=False)
                        answered_chats[cid] = {"stage": 1, "rescued": True}
                        print(f"🛟 Дослал первый LLM-ответ в чат {cid}")
                    except Exception as e:
                        answered_chats.pop(cid, None)
                        print(f"⚠️ Rescue fail {cid}: {e}")

                answered_chats[chat_id] = {"stage": 0, "rescue_pending": True}
                asyncio.create_task(delayed())

        except Exception as e:
            print(f"⚠️ Ошибка рескана: {e}")

        if once:
            break
        await asyncio.sleep(40 * 60)

# ─────────────────────────────────────
# Кликеры
# ─────────────────────────────────────
ASHQUA_USERNAME = 'ashqua_bot'
BIBINTO_USERNAME = 'bibinto_bot'

# ─────────────────────────────────────
# Функции кликера
# ─────────────────────────────────────
async def farm_lover_likes(client: TelegramClient, session_name: str, gate: OutboxGate):
    print(f"[{session_name}] Фармим лайки в @loverdating_bot")
    LOVER_BOT = "loverdating_bot"

    while True:
        try:
            if not await is_ready(client):
                await asyncio.sleep(random.uniform(5.0, 10.0))
                continue

            # получаем последнее сообщение от бота
            msgs = await client.get_messages(LOVER_BOT, limit=1)
            if not msgs:
                await asyncio.sleep(5)
                continue

            msg = msgs[0]
            text = (msg.message or msg.raw_text or "")

            # если все анкеты закончились → пауза 4 часа
            if "Ты уже посмотрел все анкеты на сегодня" in text:
                print(f"[{session_name}] 🛑 Анкеты закончились → пауза 4ч")
                await asyncio.sleep(4 * 60 * 60)
                continue

            # если анкета (слово "Действия" внутри текста)
            if "Действия" in text:
                choice = "❤️" if random.random() < 0.95 else "👎"
                await gate.send(lambda: client.send_message(LOVER_BOT, choice), priority=True)
                print(f"[{session_name}] 📤 Ответил на анкету {msg.id}: {choice}")
                await asyncio.sleep(random.uniform(20, 60))
                continue

            # если анкеты нет, пробуем вызвать просмотр
            if "Продолжить просмотр анкет" in text:
                await gate.send(lambda: client.send_message(LOVER_BOT, "Продолжить просмотр анкет"))
                print(f"[{session_name}] 📤 Продолжил просмотр")
                await asyncio.sleep(random.uniform(20, 60))
                continue

            # дефолт: шлем «Смотреть анкеты»
            await gate.send(lambda: client.send_message(LOVER_BOT, "Смотреть анкеты"))
            print(f"[{session_name}] 📤 Смотреть анкеты")
            await asyncio.sleep(random.uniform(20, 60))

        except Exception as e:
            print(f"[{session_name}] Ошибка в loverdating_bot: {e}")
            await asyncio.sleep(random.uniform(10, 30))


async def farm_ashqua_likes(client: TelegramClient, session_name: str, gate: OutboxGate):
    print(f"[{session_name}] Фармим лайки в @{ASHQUA_USERNAME}")
    last_processed_id = None
    search_attempted = False       # делали ли /search на текущей "серии мусора"
    globe_variants = {"🌎", "🌍", "🌏"}

    # лимитер действий
    MIN_LIKE_INTERVAL_RANGE = (50.0, 80.0)       # целевой интервал между лайками
    SOFT_SERVICE_INTERVAL_RANGE = (20.0, 30.0)   # мягкая пауза после /search/опросов
    next_like_at = 0.0                           # монотоническое время, когда можно ставить следующий лайк
    next_action_at = 0.0                         # не спамить служебкой
    empty_series = 0
    MAX_EMPTY_SERIES = 2                         # после двух «пустых» серий — короткий таймаут

    while True:
        try:
            if not await is_ready(client):
                await asyncio.sleep(random.uniform(5.0, 10.0))
                continue

            now = monotonic()
            if now < next_action_at:
                await asyncio.sleep(max(0.1, next_action_at - now))
                continue

            msgs = await client.get_messages(ASHQUA_USERNAME, limit=1)
            if not msgs:
                await asyncio.sleep(5)
                continue

            msg = msgs[0]
            text = (msg.message or msg.raw_text or "")

            # уже обработали это сообщение — ждём новое
            if msg.id == last_processed_id:
                await asyncio.sleep(5)
                continue

            # лайки закончились
            if "Недостаточно лайков" in text:
                sleep_sec = random.uniform(3.5, 4.5) * 3600  # 3.5–4.5 часа, чтобы не синхронизироваться
                print(f"[{session_name}] ⛔ Недостаточно лайков — спим {int(sleep_sec//3600)}ч")
                await asyncio.sleep(sleep_sec)
                last_processed_id = msg.id
                search_attempted = False
                empty_series = 0
                continue

            # ищем целевые кнопки
            heart_pos = puke_pos = None
            if msg.buttons:
                for i, row in enumerate(msg.buttons):
                    for j, btn in enumerate(row):
                        if btn.text == "❤️" and heart_pos is None:
                            heart_pos = (i, j)
                        elif btn.text == "🤮" and puke_pos is None:
                            puke_pos = (i, j)

            # Кейс A: есть ❤️/🤮 и это анкета (есть глобус)
            if (heart_pos or puke_pos) and any(g in text for g in globe_variants):
                # дождаться слота по лайк-лимитеру
                now = monotonic()
                wait = max(0.0, next_like_at - now)
                if wait > 0:
                    await asyncio.sleep(wait)

                choose_dislike = (random.random() < 0.04) and (puke_pos is not None)
                try:
                    if choose_dislike and puke_pos is not None:
                        await gate.send(lambda: msg.click(*puke_pos), priority=True)  # клики — приоритет
                        print(f"[{session_name}] Нажал 🤮 в сообщении {msg.id}")
                    else:
                        target = heart_pos or puke_pos
                        await gate.send(lambda: msg.click(*target), priority=True)   # клики — приоритет
                        print(f"[{session_name}] Нажал ❤️ в сообщении {msg.id}")
                except Exception as e:
                    print(f"[{session_name}] Ошибка при клике реакции: {e}")

                last_processed_id = msg.id
                search_attempted = False
                empty_series = 0

                # сдвигаем лимиты
                next_like_at = monotonic() + random.uniform(*MIN_LIKE_INTERVAL_RANGE)
                next_action_at = next_like_at  # до следующего лайка не трогаем бот лишний раз
                continue

            # Кейс B: есть кнопки, но это не анкета (меню/опрос/реклама)
            if msg.buttons:
                if not search_attempted:
                    print(f"[{session_name}] /search (первая попытка на серию)")
                    await gate.send(lambda: client.send_message(ASHQUA_USERNAME, "/search"), priority=False)  # служебка — без приоритета
                    search_attempted = True
                    empty_series += 1
                    next_action_at = monotonic() + random.uniform(*SOFT_SERVICE_INTERVAL_RANGE)
                    # last_processed_id не трогаем — ждём обновления
                    continue

                # ↓↓↓ Убрано рандомное кликанье до 6 раз. Просто шлём /search ↓↓↓
                print(f"[{session_name}] Опрос/меню — пропускаю клики, отправляю /search")
                await gate.send(lambda: client.send_message(ASHQUA_USERNAME, "/search"), priority=False)
                last_processed_id = msg.id
                search_attempted = False
                next_action_at = monotonic() + random.uniform(*SOFT_SERVICE_INTERVAL_RANGE)
                empty_series += 1

                if empty_series >= MAX_EMPTY_SERIES:
                    cooldown = random.uniform(60.0, 120.0)
                    print(f"[{session_name}] 💤 Много мусора → пауза {cooldown:.0f}с")
                    await asyncio.sleep(cooldown)
                    empty_series = 0
                continue

            # Кейс C: кнопок вообще нет — просто /search и ждём новое сообщение
            print(f"[{session_name}] Кнопок нет — /search")
            await gate.send(lambda: client.send_message(ASHQUA_USERNAME, "/search"), priority=False)
            search_attempted = True
            last_processed_id = msg.id  # фикс: чтобы не долбить один и тот же месседж
            next_action_at = monotonic() + random.uniform(*SOFT_SERVICE_INTERVAL_RANGE)
            empty_series += 1

            if empty_series >= MAX_EMPTY_SERIES:
                cooldown = random.uniform(60.0, 120.0)
                print(f"[{session_name}] 💤 Пустая серия → пауза {cooldown:.0f}с")
                await asyncio.sleep(cooldown)
                empty_series = 0

        except Exception as e:
            print(f"[{session_name}] Ошибка в ashqua_bot: {e}")
            await asyncio.sleep(random.uniform(8.0, 20.0))





async def farm_bibinto_votes(client: TelegramClient, session_name: str, gate: OutboxGate):
    sent_messages = 0
    next_profile_ping_at = random.randint(8, 40)

    last_break_at = monotonic()
    LONG_BREAK_EVERY_SEC = random.randint(25, 50) * 60       # от 5 до 15 минут
    LONG_BREAK_DURATION_SEC = random.randint(5, 16) * 60 # от 5 до 15 минут

    city_re = re.compile(r'(?<![А-Яа-яЁё])Город(?![А-Яа-яЁё])')
    PROFILE_SNIPPET = "тяночка мурчалочка"

    def pick_sleep():
        r = random.random()
        if r < 0.70:
            base = random.uniform(1.5, 3.5)
        elif r < 0.90:
            base = random.uniform(4.0, 6.0)
        else:
            base = random.uniform(5.0, 9.0)
        return max(1.0, base + random.uniform(-0.3, 0.3))

    while True:

        try:
            if not await is_ready(client):
                await asyncio.sleep(random.uniform(5.0, 10.0))
                continue
            # длинная пауза
            if monotonic() - last_break_at >= LONG_BREAK_EVERY_SEC:
                print(f"[{session_name}] ⏸ Длинная пауза bibinto_bot на {LONG_BREAK_DURATION_SEC//3600}ч…")
                await asyncio.sleep(LONG_BREAK_DURATION_SEC)
                last_break_at = monotonic()
                sent_messages = 0
                next_profile_ping_at = random.randint(8, 40)
                
                # пересчёт значений для следующего раза
                LONG_BREAK_EVERY_SEC = random.randint(25, 50) * 60
                LONG_BREAK_DURATION_SEC = random.randint(5, 16) * 60

            # хаотичный "👤Мой профиль"
            if sent_messages >= next_profile_ping_at:
                await gate.send(lambda: client.send_message(BIBINTO_USERNAME, '👤Мой профиль'))
                print(f"[{session_name}] 📤 bibinto_bot: 👤Мой профиль")
                sent_messages = 0
                next_profile_ping_at = random.randint(8, 40)
                await asyncio.sleep(random.uniform(10.0, 20.0))
                await gate.send(lambda: client.send_message(BIBINTO_USERNAME, '🚀Оценивать'))
                print(f"[{session_name}] 📤 bibinto_bot: 🚀Оценивать (после профиля)")
                await asyncio.sleep(pick_sleep())
                continue

            # проверяем последнее сообщение
            msgs = await client.get_messages(BIBINTO_USERNAME, limit=1)
            last = msgs[0] if msgs else None
            text = (last.message or last.raw_text or "") if last else ""
            tl = text.lower()

            # ХУК: если пришёл сигнал "Коала накушалась твоих оценок!"
            if "коала накушалась твоих оценок!" in tl:
                print(f"[{session_name}] 🐨 Коала сыта → пауза 3 часа")
                await asyncio.sleep(3 * 60 * 60)
                sent_messages = 0
                next_profile_ping_at = random.randint(8, 40)
                continue

            # ХУК: если это наш "Мой профиль" — не оцениваем
            if PROFILE_SNIPPET in tl:
                pause = random.uniform(5.0, 15.0)
                print(f"[{session_name}] 🛑 Обнаружен мой профиль → пауза {pause:.1f}с и 🚀Оценивать")
                await asyncio.sleep(pause)
                await gate.send(lambda: client.send_message(BIBINTO_USERNAME, '🚀Оценивать'))
                sent_messages += 1
                await asyncio.sleep(pick_sleep())
                continue

            # Анкета только если есть ровно слово "Город"
            if not city_re.search(text):
                pause = random.uniform(5.0, 15.0)
                print(f"[{session_name}] ⏸ Нет слова 'Город' → пауза {pause:.1f}с и 🚀Оценивать")
                await asyncio.sleep(pause)
                await gate.send(lambda: client.send_message(BIBINTO_USERNAME, '🚀Оценивать'))
                sent_messages += 1
                await asyncio.sleep(pick_sleep())
                continue

            # ставим оценку
            score = str(random.choice([9, 10]))
            await gate.send(lambda: client.send_message(BIBINTO_USERNAME, score))
            sent_messages += 1
            print(f"[{session_name}] 📤 bibinto_bot: Оценка {score}")

            # задержка между действиями
            await asyncio.sleep(pick_sleep())

            # редкая “заминка”
            if random.random() < 0.08:
                extra = random.uniform(12.0, 40.0)
                print(f"[{session_name}] 🤔 Небольшая заминка {extra:.1f} c")
                await asyncio.sleep(extra)

        except Exception as e:
            print(f"[{session_name}] Ошибка в bibinto_bot:", e)
            await asyncio.sleep(random.uniform(8.0, 20.0))
# ===============================================

# ─────────────────────────────────────
# Логика одного аккаунта
# ─────────────────────────────────────
SESS_RE = re.compile(r"(sessid-)(\d+)")
def rotate_sessid(url: str) -> str:
    return SESS_RE.sub(lambda m: f"{m.group(1)}{random.randint(10**9, 10**10-1)}", url, count=1)

async def run_client(session_name, api_id, api_hash, link, proxy_url):
    session_path = os.path.join(SESSION_FOLDER, session_name)

    while True:
        client = None
        tasks = []
        try:
            px = parse_proxy_url(proxy_url)
            client = TelegramClient(
                session_path,
                api_id,
                api_hash,
                connection=PatchedAbridged,
                proxy=px,
                use_ipv6=False,
                connection_retries=3,
            )
            gate = OutboxGate(base_delay=6.0, priority_delay=2.0)

            llm_mgr = LLMChatManager(channel_link=link)
            register_auto_reply(client, session_name, gate, llm_mgr)

            await client.start()
            print(f"[{session_name}] ✅ Запущен")
            await print_telegram_seen_ip(client)

            # кликеры стартуют сразу
            tasks.append(asyncio.create_task(farm_ashqua_likes(client, session_name, gate)))
            tasks.append(asyncio.create_task(farm_bibinto_votes(client, session_name, gate)))
            tasks.append(asyncio.create_task(farm_lover_likes(client, session_name, gate))) 

            # отложенный рескан — через 30 минут после запуска
            async def delayed_rescan():
                await asyncio.sleep(30 * 60)
                await rescan_dialogs_missing_replies(client, gate, llm_mgr)

            tasks.append(asyncio.create_task(delayed_rescan()))

            
            await client.run_until_disconnected()
            print(f"[{session_name}] ℹ️ Disconnected. Reconnect in 5 min…")
            await asyncio.sleep(300)

        except asyncio.CancelledError:
            raise
        except (socks.GeneralProxyError, socks.ProxyConnectionError, ConnectionError, OSError, asyncio.TimeoutError) as e:
            print(f"[{session_name}] ⚠️ Network/proxy failure: {type(e).__name__}: {e}. Retry in 5 min…")
            # proxy_url = rotate_sessid(proxy_url)  # при желании можно включить ротацию
            await asyncio.sleep(300)
        except RPCError as e:
            print(f"[{session_name}] ⚠️ RPCError: {type(e).__name__} — retry in 5 min")
            await asyncio.sleep(300)
        except Exception as e:
            print(f"[{session_name}] ❌ Fatal: {type(e).__name__}: {e} — retry in 5 min")
            await asyncio.sleep(300)
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
            if tasks:
                try:
                    await asyncio.gather(*tasks, return_exceptions=True)
                except Exception:
                    pass
            if client:
                try:
                    await client.disconnect()
                except Exception:
                    pass

# ─────────────────────────────────────
# Главное
# ─────────────────────────────────────
async def main():
    session_files = sorted([
        os.path.splitext(f)[0]
        for f in os.listdir(SESSION_FOLDER)
        if f.endswith(".session")
    ])

    if not session_files:
        print("❌ Нет сессий для запуска.")
        return

    if len(session_files) < len(ACCOUNTS):
        print("⚠️ Сессий меньше, чем аккаунтов. Запустим первые", len(session_files))
    elif len(session_files) > len(ACCOUNTS):
        print("⚠️ Сессий больше, чем аккаунтов. Лишние сессии проигнорируем.")

    tasks = []
    for session_name, acc in zip(session_files, ACCOUNTS):
        tasks.append(asyncio.create_task(
            run_client(
                session_name,
                acc["api_id"],
                acc["api_hash"],
                acc["link"],
                acc["proxy_url"]
            )
        ))

    await asyncio.gather(*tasks)

if __name__ == '__main__':
    asyncio.run(main())
