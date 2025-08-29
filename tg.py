import asyncio
import random
import logging
import os
import re
from datetime import datetime, timezone
from time import monotonic
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, RPCError
import socks  # из PySocks
from urllib.parse import urlparse, unquote
from telethon.network.connection.tcpabridged import ConnectionTcpAbridged  # (опционально, но лучше)
from telethon.tl.functions.account import GetAuthorizationsRequest

# Гейт на все исходящие для аккаунта
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
            while True:  # повторяем ту же задачу после FloodWait/RPCError
                try:
                    result = await coro_factory()
                    if not fut.done():
                        fut.set_result(result)
                    # пауза после каждого успешного запроса
                    await asyncio.sleep(self.priority_delay if priority else self.base_delay)
                    break  # выходим из внутреннего цикла, задача выполнена
                except FloodWaitError as e:
                    wait = int(getattr(e, "seconds", 3)) + random.uniform(0.2, 0.6)
                    print(f"⏳ Queue caught FloodWait {wait:.1f}s — retrying same job")
                    await asyncio.sleep(wait)
                    # иду на повтор той же задачи
                    continue
                except RPCError as e:
                    backoff = 2.0 + random.uniform(0.2, 0.8)
                    print(f"⚠️ Queue RPCError {type(e).__name__}, retry in {backoff:.1f}s")
                    await asyncio.sleep(backoff)
                    continue
                except Exception as e:
                    if not fut.done():
                        fut.set_exception(e)
                    # маленькая пауза, чтобы не молотить ошибку
                    await asyncio.sleep(0.5)
                    break
            self.queue.task_done()



# создаём per-client в run_client()


# ─────────────────────────────────────
# Общие настройки
# ─────────────────────────────────────
ACCOUNTS = [
    {
        "api_id": 28486483,
        "api_hash": "e3b71f6874229951ed5e406195cab4ad",
        "link": "https://t.me/+gOOcjDAnEIlhNGY6", 
        "proxy_url": "socks5h://customer-kaminari_7YDmg-cc-de-city-berlin-sessid-0188753575-sesstime-30:RamiBabli15062007+@pr.oxylabs.io:7777"
    },
]


SESSION_FOLDER = "session"
os.makedirs(SESSION_FOLDER, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ─────────────────────────────────────
# прокси
# 

def parse_proxy_url(url: str):
    u = urlparse(url)
    return {
        "proxy_type": socks.SOCKS5,               # <- обязательно
        "addr": u.hostname,                       # <- НЕ host, а addr
        "port": u.port,
        "rdns": (u.scheme.lower() == "socks5h"),
        "username": u.username,
        "password": unquote(u.password or ""),
    }

async def print_telegram_seen_ip(client: TelegramClient):
    auths = await client(GetAuthorizationsRequest())
    cur = next((a for a in auths.authorizations if getattr(a, "current", False)), None)
    if cur:
        print(f"🌐 Telegram видит IP: {cur.ip} | страна: {cur.country} | регион: {cur.region}")
    else:
        print("⚠️ Не удалось определить текущую авторизацию")

async def is_ready(client: TelegramClient) -> bool:
    """Онлайн + авторизация. Никаких сетевых штурмов, быстрый ранний выход."""
    if not client.is_connected():
        return False
    try:
        return await client.is_user_authorized()
    except Exception:
        return False

async def send_if_ready(client: TelegramClient, gate: OutboxGate, coro_factory, *, priority=False):
    """Не шлём ничего, если клиент не готов (разрыв/деавторизация)."""
    if not await is_ready(client):
        return None
    return await gate.send(coro_factory, priority=priority)


class PatchedAbridged(ConnectionTcpAbridged):
    async def _connect(self, timeout=None, ssl=None, **kwargs):
        """
        Патчим asyncio.open_connection только на время коннекта,
        чтобы Telethon открыл TCP через PySocks с нашим прокси.
        """
        loop = asyncio.get_running_loop()
        orig_open = asyncio.open_connection
        px = self._proxy or {}  # сюда мы передадим dict из parse_proxy_url()

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
            # поддержка как позиционных (host, port), так и именованных аргументов
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
# Настройки автоответчика
# ─────────────────────────────────────

SECOND_MESSAGE = "Привет привет, я тут"
THIRD_MESSAGE = "Ты еще не чекал мой тгк?"

OFFLINE_THRESHOLD_MINUTES = 1    # можно вернуть проверку, когда всё будет ок
FIRST_REPLY_DELAY_RANGE = (30, 200)    
SECOND_REPLY_DELAY_RANGE = (200, 400)  
THIRD_REPLY_DELAY_RANGE = (50, 150)    


IMAGE_PATH = ""  # путь до картинки для первого сообщения

# ─────────────────────────────────────
# Настройки кликера
# ─────────────────────────────────────
ASHQUA_USERNAME = 'ashqua_bot'
BIBINTO_USERNAME = 'bibinto_bot'

# ─────────────────────────────────────
# Состояние автоответчика
# ─────────────────────────────────────
answered_chats = {}  # chat_id → {"stage": 1|2|3, "second_sent_at": datetime}


# ─────────────────────────────────────
# Вспомогательные таски автоответчика
# ─────────────────────────────────────


from telethon.tl.types import InputPeerUser, InputPeerChat, InputPeerChannel
def _is_input_peer(x): return isinstance(x, (InputPeerUser, InputPeerChat, InputPeerChannel))

async def has_any_outgoing(client: TelegramClient, peer) -> bool:
    try:
        ent = peer if _is_input_peer(peer) else await client.get_input_entity(peer)
        msgs = await client.get_messages(ent, from_user='me', limit=1)
        return bool(msgs)
    except Exception as e:
        logging.warning(f"has_any_outgoing failed for {peer}: {e}")
        return True  # лучше перестраховаться, чем задублировать





async def send_second_message(client, peer, cid, gate):
    delay = random.randint(*SECOND_REPLY_DELAY_RANGE)
    print(f"⏳ Ждём {delay} сек до второго сообщения…")
    await asyncio.sleep(delay)
    try:
        await gate.send(lambda: client.send_message(peer, SECOND_MESSAGE), priority=True)
        rec = answered_chats.get(cid, {})
        rec.update({"stage": 2, "second_sent_at": datetime.now(timezone.utc)})
        answered_chats[cid] = rec
        print(f"💬 Второе сообщение отправлено в чат {cid}")
        asyncio.create_task(send_third_message(client, peer, cid, gate))
    except Exception as e:
        print(f"⚠️ Ошибка при отправке второго сообщения: {type(e).__name__}: {e}")

async def send_third_message(client, peer, cid, gate):
    delay = random.randint(*THIRD_REPLY_DELAY_RANGE)
    print(f"⏳ Ждём {delay} сек до третьего сообщения…")
    await asyncio.sleep(delay)
    try:
        await gate.send(lambda: client.send_message(peer, THIRD_MESSAGE), priority=True)
        rec = answered_chats.get(cid, {})
        rec["stage"] = 3
        answered_chats[cid] = rec
        print(f"💬 Третье сообщение отправлено в чат {cid}")
    except Exception as e:
        print(f"⚠️ Ошибка при отправке третьего сообщения: {type(e).__name__}: {e}")


async def rescan_dialogs_missing_replies(client: TelegramClient, gate: OutboxGate, first_message: str, once=False):
    while True:
        try:
            # не авторизованы/не подключены? ждём и пробуем позже (сторож №1)
            if not await is_ready(client):
                await asyncio.sleep(random.randint(20, 40))
                continue

            cutoff = datetime.now(timezone.utc).timestamp() - 2*60*60  # последние 2 часа

            # повторная проверка готовности перед длинным проходом (сторож №2)
            if not await is_ready(client):
                await asyncio.sleep(random.randint(20, 40))
                continue

            async for dlg in client.iter_dialogs():
                if not dlg.is_user:
                    continue
                if getattr(dlg.entity, "bot", False):
                    continue

                chat_id = dlg.id

                # РЕЗОЛВИМ entity для диалога
                try:
                    ent = await client.get_input_entity(chat_id)
                except Exception as e:
                    print(f"⚠️ Не удалось получить entity в рескане для {chat_id}: {e}")
                    continue

                # пропускаем, если уже есть любые наши исходящие
                try:
                    if await has_any_outgoing(client, ent):
                        continue
                except Exception as e:
                    print(f"⚠️ Ошибка проверки исходящих (rescan): {e}")
                    continue

                if chat_id in answered_chats:
                    continue

                msgs = await client.get_messages(ent, limit=5)  # читаем историю по entity
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

                async def delayed(ent=ent, cid=chat_id):
                    await asyncio.sleep(delay)
                    try:
                        if await has_any_outgoing(client, ent):
                            answered_chats.pop(cid, None)  # ← снять rescue lock
                            return
                        await gate.send(lambda: client.send_message(ent, first_message), priority=False)
                        answered_chats[cid] = {"stage": 1, "rescued": True}
                        print(f"🛟 Дослал поздний ответ в чат {cid}")
                        asyncio.create_task(send_second_message(client, ent, cid, gate))
                    except Exception as e:
                        answered_chats.pop(cid, None)  # ← и на ошибке тоже освобождаем
                        print(f"⚠️ Rescue fail {cid}: {e}")

                answered_chats[chat_id] = {"stage": 0, "rescue_pending": True}
                asyncio.create_task(delayed())

        except Exception as e:
            print(f"⚠️ Ошибка рескана: {e}")

        if once:
            break
        await asyncio.sleep(random.randint(50, 80) * 60)






# ─────────────────────────────────────
# Основной хэндлер входящих для автоответчика
# ─────────────────────────────────────
def register_auto_reply(client: TelegramClient, first_message: str, gate: OutboxGate):
    @client.on(events.NewMessage(incoming=True))
    async def auto_reply(event):

        sender = await event.get_sender()
        if getattr(sender, "bot", False):
            return  # не отвечаем ботам

        if event.is_group or event.is_channel or event.out:
            return

        chat_id = event.chat_id
        if chat_id in answered_chats:
            return
        answered_chats[chat_id] = {"stage": 0}

        # РЕЗОЛВИМ entity (важно, не используем голый chat_id)
        try:
            ent = await event.get_input_chat()
        except Exception as e:
            print(f"⚠️ Не удалось получить entity для {chat_id}: {e}")
            del answered_chats[chat_id]
            return

        if not await is_ready(client):
            return
        # Глобальная проверка исходящих: если уже что-то писали в этот чат — пропускаем
        try:
            if await has_any_outgoing(client, ent):
                print("❌ В чате уже есть наши исходящие — пропуск")
                del answered_chats[chat_id]
                return
        except Exception as e:
            print(f"⚠️ Ошибка при проверке исходящих: {e}")
            del answered_chats[chat_id]
            return

        delay = random.randint(5, 20)
        print(f"⏳ Ждём {delay} сек до первого сообщения…")
        await asyncio.sleep(delay)

        try:
            async def _send():
                # финальная проверка прямо перед отправкой
                if await has_any_outgoing(client, ent):
                    print("❌ Появились исходящие перед отправкой — отмена")
                    answered_chats.pop(chat_id, None)
                    return

                if IMAGE_PATH and os.path.isfile(IMAGE_PATH):
                    return await client.send_message(ent, first_message, file=IMAGE_PATH)
                else:
                    return await client.send_message(ent, first_message)

            result = await send_if_ready(client, gate, _send, priority=True)
            if result is None:
                # отменили из-за появившихся исходящих — выходим
                return

            answered_chats[chat_id] = {"stage": 1, "first_sent_at": datetime.now(timezone.utc)}
            print(f"💬 Первое сообщение отправлено в чат {chat_id}")
            asyncio.create_task(send_second_message(client, ent, chat_id, gate))
            
        except Exception as e:
                print(f"⚠️ Ошибка при отправке первого сообщения: {type(e).__name__}: {e}")
                del answered_chats[chat_id]





# ─────────────────────────────────────
# Функции кликера
# ─────────────────────────────────────
async def farm_ashqua_likes(client: TelegramClient, session_name: str, gate: OutboxGate):
    print(f"[{session_name}] Фармим лайки в @{ASHQUA_USERNAME}")
    last_processed_id = None
    search_attempted = False       # делали ли /search на текущей "серии мусора"
    globe_variants = {"🌎", "🌍", "🌏"}

    while True:
        try:

            if not await is_ready(client):
                await asyncio.sleep(random.uniform(5.0, 10.0))
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
                print(f"[{session_name}] ⛔ Недостаточно лайков — спим 4 часа")
                await asyncio.sleep(4 * 60 * 60)
                last_processed_id = msg.id
                search_attempted = False
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
                choose_dislike = (random.random() < 0.04) and (puke_pos is not None)
                try:
                    if choose_dislike:
                        await gate.send(lambda: msg.click(*puke_pos))
                        print(f"[{session_name}] Нажал 🤮 в сообщении {msg.id}")
                    elif heart_pos is not None:
                        await gate.send(lambda: msg.click(*heart_pos))
                        print(f"[{session_name}] Нажал ❤️ в сообщении {msg.id}")
                    else:
                        await gate.send(lambda: msg.click(*puke_pos))
                        print(f"[{session_name}] (нет ❤️) Нажал 🤮 в сообщении {msg.id}")
                except Exception as e:
                    print(f"[{session_name}] Ошибка при клике реакции: {e}")

                last_processed_id = msg.id
                search_attempted = False
                await asyncio.sleep(random.uniform(4.0, 7.0))
                continue

            # Кейс B: нет цели (скорее реклама/опрос/меню)
            if msg.buttons:
                if not search_attempted:
                    # делаем /search ОДИН раз на серию мусора
                    print(f"[{session_name}] /search (первая попытка на серию)")
                    await gate.send(lambda: client.send_message(ASHQUA_USERNAME, "/search"))
                    search_attempted = True
                    await asyncio.sleep(random.uniform(1.0, 2.0))
                    continue

                # /search уже пробовали — значит это опрос/меню, кликаем пока не исчезнут
                print(f"[{session_name}] Опрос/меню — кликаю кнопки до очистки, затем /search")
                clicks_done, max_clicks = 0, 6
                while clicks_done < max_clicks:
                    cur = await client.get_messages(ASHQUA_USERNAME, ids=msg.id)
                    if not cur or not cur.buttons:
                        break
                    try:
                        await gate.send(lambda: cur.click(0, 0))
                        clicks_done += 1
                        print(f"[{session_name}] Тыкнул кнопку {clicks_done}/{max_clicks} в опросе {msg.id}")
                    except Exception as e:
                        print(f"[{session_name}] Ошибка при клике по опросу: {e}")
                        break
                    await asyncio.sleep(random.uniform(1.0, 2.0))

                # добили опрос — снова /search и ждём новую анкету
                await gate.send(lambda: client.send_message(ASHQUA_USERNAME, "/search"))
                last_processed_id = msg.id
                search_attempted = False
                await asyncio.sleep(random.uniform(4.0, 7.0))
                continue

            # Кейс C: кнопок вообще нет — просто /search и ждём
            print(f"[{session_name}] Кнопок нет — /search")
            await gate.send(lambda: client.send_message(ASHQUA_USERNAME, "/search"))
            search_attempted = True
            await asyncio.sleep(random.uniform(4.0, 7.0))

        except Exception as e:
            print(f"[{session_name}] Ошибка в ashqua_bot: {e}")
            await asyncio.sleep(random.uniform(8.0, 20.0))






async def farm_bibinto_votes(client: TelegramClient, session_name: str, gate: OutboxGate):
    sent_messages = 0
    next_profile_ping_at = random.randint(8, 40)

    last_break_at = monotonic()
    LONG_BREAK_EVERY_SEC = random.randint(30, 60) * 60       # от 30 до 60 минут
    LONG_BREAK_DURATION_SEC = random.randint(1, 6) * 60 * 60 # от 1 до 6 часов

    city_re = re.compile(r'(?<![А-Яа-яЁё])Город(?![А-Яа-яЁё])')
    PROFILE_SNIPPET = "может скину что нибудь"

    def pick_sleep():
        r = random.random()
        if r < 0.70:
            base = random.uniform(2.5, 4.5)
        elif r < 0.90:
            base = random.uniform(8.0, 14.0)
        else:
            base = random.uniform(15.0, 20.0)
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
                LONG_BREAK_EVERY_SEC = random.randint(30, 60) * 60
                LONG_BREAK_DURATION_SEC = random.randint(1, 6) * 60 * 60

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





# ─────────────────────────────────────
# Логика одного аккаунта
# ─────────────────────────────────────
SESS_RE = re.compile(r"(sessid-)(\d+)")
def rotate_sessid(url: str) -> str:
    # меняем только цифры после sessid-
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
                device_model="iPhone 15",
                system_version="iOS 16.6",
                app_version="9.7",
                lang_code="ru",
                system_lang_code="ru-RU",
            )
            gate = OutboxGate(base_delay=6.0, priority_delay=2.0)

            first_message = (
                f"Привет) Отвечу через пару минут, щас занята, можешь пока заценить "
                f"Потом может ножки скину) {link}"
            )
            register_auto_reply(client, first_message, gate)

            await client.start()
            print(f"[{session_name}] ✅ Запущен")
            await print_telegram_seen_ip(client)

            # первичный и периодический рескан
            await rescan_dialogs_missing_replies(client, gate, first_message, once=True)
            tasks.append(asyncio.create_task(rescan_dialogs_missing_replies(client, gate, first_message)))
            tasks.append(asyncio.create_task(farm_ashqua_likes(client, session_name, gate)))
            tasks.append(asyncio.create_task(farm_bibinto_votes(client, session_name, gate)))

            # блокируемся до дисконнекта
            await client.run_until_disconnected()

            # сюда попадаем при штатном обрыве — делаем паузу перед перезапуском
            print(f"[{session_name}] ℹ️ Disconnected. Reconnect in 5 min…")
            await asyncio.sleep(300)

        except asyncio.CancelledError:
            # если когда-нибудь решишь останавливать снаружи
            raise

        except (socks.GeneralProxyError, socks.ProxyConnectionError, ConnectionError, OSError, asyncio.TimeoutError) as e:
            print(f"[{session_name}] ⚠️ Network/proxy failure: {type(e).__name__}: {e}. Retry in 5 min…")
            # опционально: крутануть sessid, чтобы уйти на другой бэкенд прокси
            # proxy_url = rotate_sessid(proxy_url)
            # погасим фоновые задачи перед паузой
            for t in tasks:
                if not t.done():
                    t.cancel()
            if tasks:
                try:
                    await asyncio.gather(*tasks, return_exceptions=True)
                except Exception:
                    pass
            # и соединение тоже закроем
            if client:
                try:
                    await client.disconnect()
                except Exception:
                    pass

            await asyncio.sleep(300)  # 5 минут

        except RPCError as e:
            print(f"[{session_name}] ⚠️ RPCError: {type(e).__name__} — retry in 5 min")
            for t in tasks:
                if not t.done():
                    t.cancel()
            if tasks:
                try:
                    await asyncio.gather(*tasks, return_exceptions=True)
                except Exception:
                    pass
            # и соединение тоже закроем
            if client:
                try:
                    await client.disconnect()
                except Exception:
                    pass
            await asyncio.sleep(300)

        except Exception as e:
            print(f"[{session_name}] ❌ Fatal: {type(e).__name__}: {e} — retry in 5 min")
            for t in tasks:
                if not t.done():
                    t.cancel()
            if tasks:
                try:
                    await asyncio.gather(*tasks, return_exceptions=True)
                except Exception:
                    pass
            # и соединение тоже закроем
            if client:
                try:
                    await client.disconnect()
                except Exception:
                    pass
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
                acc["proxy_url"]  # ← ВАЖНО: передаём прокси
            )
        ))

    await asyncio.gather(*tasks)



if __name__ == '__main__':
    asyncio.run(main())
