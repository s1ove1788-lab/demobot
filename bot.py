"""
Демо-бот «Автосервис» для MAX.

Что умеет:
  • запись клиента: услуга → авто → дата → свободное время → телефон → подтверждение;
  • прайс, адрес и часы работы, вопрос мастеру, «Мои записи» с отменой;
  • владелец получает заявку и одной кнопкой подтверждает / отменяет / жмёт «Машина готова»;
  • напоминание клиенту за 2 часа до визита, сбор оценки после визита;
  • ДЕМО-РЕЖИМ: клиент сам видит карточку заявки «глазами владельца» и может понажимать кнопки;
  • вам (ADMIN_IDS) приходит уведомление о каждом, кто открыл демо, — это ваши потенциальные покупатели.

Запуск: заполните .env (см. .env.example) и выполните `python3 bot.py`.
"""
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

VERSION = "01.11-demo-v1"

TOKEN = os.environ.get("MAX_TOKEN", "")
ADMIN_IDS = {int(x) for x in re.split(r"[,\s]+", os.getenv("ADMIN_IDS", "")) if x.strip().isdigit()}
DEMO_MODE = os.getenv("DEMO_MODE", "1") == "1"
DB_PATH = os.getenv("DB_PATH", "autoservice.db")
TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Moscow"))
API = "https://platform-api.max.ru"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("autodemo")

# ─────────────────────────── Настройки бизнеса ───────────────────────────
# Под реального клиента меняете только этот блок.

BUSINESS = {
    "name": "ТурбоГараж",
    "address": "ул. Моторная, 12 (въезд со двора)",
    "hours": "Пн–Сб 9:00–20:00, Вс — выходной",
    "phone": "+7 900 000-00-00",
}

# (код, название категории, [(код услуги, название, цена)])
CATEGORIES = [
    ("to", "🔧 ТО и ремонт", [
        ("oil", "Замена масла и фильтров", "от 1 500 ₽"),
        ("service", "Комплексное ТО", "от 4 500 ₽"),
        ("diag", "Компьютерная диагностика", "1 000 ₽"),
        ("susp", "Диагностика ходовой", "800 ₽"),
    ]),
    ("tires", "🛞 Шиномонтаж", [
        ("t16", "Переобувка R13–R16 (4 колеса)", "2 400 ₽"),
        ("t20", "Переобувка R17–R21 (4 колеса)", "3 600 ₽"),
        ("bal", "Балансировка 4 колёс", "1 200 ₽"),
    ]),
    ("wash", "✨ Мойка и детейлинг", [
        ("wash", "Комплексная мойка", "1 200 ₽"),
        ("chem", "Химчистка салона", "от 8 000 ₽"),
        ("polish", "Полировка кузова", "от 12 000 ₽"),
        ("ceramic", "Керамическое покрытие", "от 25 000 ₽"),
    ]),
]
SERVICES = {s[0]: {"name": s[1], "price": s[2], "cat": c[0]} for c in CATEGORIES for s in c[2]}

WORK_DAYS = {0, 1, 2, 3, 4, 5}          # Пн–Сб
SLOT_HOURS = list(range(9, 20))          # начало слотов: 9:00 … 19:00
SLOT_CAPACITY = 2                        # сколько машин принимаем на один час (боксы)
DAYS_AHEAD = 7
REMIND_BEFORE = timedelta(hours=2)

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
STATUS_RU = {"new": "🟡 ждёт подтверждения", "confirmed": "🟢 подтверждена",
             "done": "✅ выполнена", "cancelled": "⚪️ отменена"}

# ─────────────────────────── API MAX ───────────────────────────


class Max:
    def __init__(self, token):
        self.s = requests.Session()
        self.s.headers["Authorization"] = token

    def call(self, method, path, params=None, body=None, timeout=40):
        r = self.s.request(method, API + path, params=params, json=body, timeout=timeout)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} → {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    def send(self, uid, text, kb=None):
        body = {"text": text, "format": "markdown"}
        if kb:
            body["attachments"] = [kb]
        return self.call("POST", "/messages", params={"user_id": uid}, body=body)

    def answer(self, callback_id, text=None, kb=None, notification=None):
        """Ответ на нажатие кнопки: заменить сообщение и/или показать всплывашку."""
        body = {}
        if text is not None:
            body["message"] = {"text": text, "format": "markdown", "attachments": [kb] if kb else []}
        if notification:
            body["notification"] = notification
        return self.call("POST", "/answers", params={"callback_id": callback_id}, body=body)


mx = Max(TOKEN)


def keyboard(rows):
    """rows: [[(текст, payload), ...], ...]; payload со ссылкой → кнопка-ссылка."""
    def btn(t, p):
        if p.startswith("http"):
            return {"type": "link", "text": t, "url": p}
        return {"type": "callback", "text": t, "payload": p}
    return {"type": "inline_keyboard", "payload": {"buttons": [[btn(t, p) for t, p in r] for r in rows]}}


def safe_send(uid, text, kb=None):
    try:
        mx.send(uid, text, kb)
    except Exception as e:  # noqa: BLE001
        log.warning("Не удалось отправить %s: %s", uid, e)


def show(uid, callback_id, text, kb=None):
    """Заменить текущее сообщение (если пришли с кнопки) или отправить новое."""
    if callback_id:
        try:
            mx.answer(callback_id, text, kb)
            return
        except Exception as e:  # noqa: BLE001
            log.warning("answer не прошёл, шлю новым сообщением: %s", e)
    safe_send(uid, text, kb)


def notify(callback_id, text):
    if callback_id:
        try:
            mx.answer(callback_id, notification=text)
        except Exception:  # noqa: BLE001
            pass

# ─────────────────────────── База ───────────────────────────


_db = sqlite3.connect(DB_PATH, check_same_thread=False)
_db.row_factory = sqlite3.Row
_db_lock = threading.Lock()


def q(sql, args=(), one=False):
    with _db_lock:
        cur = _db.execute(sql, args)
        _db.commit()
        rows = cur.fetchall()
        return cur.lastrowid if sql.lstrip().upper().startswith("INSERT") else (rows[0] if one and rows else (None if one else rows))


def init_db():
    with _db_lock:
        _db.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY, name TEXT, source TEXT, first_at TEXT);
        CREATE TABLE IF NOT EXISTS bookings(
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, service TEXT, price TEXT,
            car TEXT, phone TEXT, start TEXT, status TEXT, reminded INTEGER DEFAULT 0,
            rating INTEGER, created_at TEXT);
        """)
        _db.commit()


def now():
    return datetime.now(TZ)


def fmt_dt(iso):
    d = datetime.fromisoformat(iso)
    return f"{WEEKDAYS[d.weekday()]} {d:%d.%m} в {d:%H:%M}"


def booking_text(b):
    return (f"🔧 {b['service']} — {b['price']}\n"
            f"🚗 {b['car']}\n"
            f"📅 {fmt_dt(b['start'])}\n"
            f"📞 {b['phone']}")

# ─────────────────────────── Состояние диалога ───────────────────────────

STATE = {}  # uid → {"step": ..., ...}


def is_owner(uid, booking=None):
    """Владелец — из ADMIN_IDS. В демо-режиме клиент управляет своей заявкой «как владелец»."""
    return uid in ADMIN_IDS or (DEMO_MODE and booking is not None and booking["user_id"] == uid)

# ─────────────────────────── Экраны клиента ───────────────────────────


def main_menu_kb():
    return keyboard([
        [("📅 Записаться", "book")],
        [("💰 Цены", "prices"), ("📍 Адрес и часы", "info")],
        [("❓ Вопрос мастеру", "ask"), ("🗂 Мои записи", "my")],
    ])


def welcome_text():
    t = (f"👋 Здравствуйте! Это автосервис **{BUSINESS['name']}**.\n\n"
         "Здесь можно за минуту записаться на ремонт, шиномонтаж или мойку — без звонков и ожидания на линии.")
    if DEMO_MODE:
        t += ("\n\n_Это демонстрационный бот: запишитесь как клиент, а потом посмотрите, "
              "как заявку видит владелец сервиса._")
    return t


def screen_menu(uid, cb=None):
    STATE.pop(uid, None)
    show(uid, cb, welcome_text(), main_menu_kb())


def screen_prices(uid, cb):
    lines = [f"💰 **Цены {BUSINESS['name']}**"]
    for _, title, items in CATEGORIES:
        lines.append(f"\n**{title}**")
        lines += [f"• {name} — {price}" for _, name, price in items]
    lines.append("\nТочную стоимость мастер назовёт после осмотра.")
    show(uid, cb, "\n".join(lines), keyboard([[("📅 Записаться", "book")], [("◀️ Меню", "menu")]]))


def screen_info(uid, cb):
    show(uid, cb, (f"📍 **{BUSINESS['address']}**\n"
                   f"🕘 {BUSINESS['hours']}\n"
                   f"📞 {BUSINESS['phone']}"),
         keyboard([[("📅 Записаться", "book")], [("◀️ Меню", "menu")]]))


def screen_categories(uid, cb):
    STATE[uid] = {"step": "choose"}
    rows = [[(title, f"cat:{code}")] for code, title, _ in CATEGORIES]
    rows.append([("◀️ Меню", "menu")])
    show(uid, cb, "Что нужно сделать с автомобилем?", keyboard(rows))


def screen_services(uid, cb, cat):
    c = next((c for c in CATEGORIES if c[0] == cat), None)
    if not c:
        return screen_categories(uid, cb)
    rows = [[(f"{name} · {price}", f"srv:{code}")] for code, name, price in c[2]]
    rows.append([("◀️ Назад", "book")])
    show(uid, cb, f"**{c[1]}**\nВыберите услугу:", keyboard(rows))


def screen_ask_car(uid, cb, srv):
    if srv not in SERVICES:
        return screen_categories(uid, cb)
    STATE[uid] = {"step": "car", "srv": srv}
    show(uid, cb, (f"✅ {SERVICES[srv]['name']}\n\n"
                   "🚗 Напишите марку и модель автомобиля, например: **Kia Rio 2019**"),
         keyboard([[("◀️ Назад", f"cat:{SERVICES[srv]['cat']}")]]))


def free_slots(day):
    slots = []
    for h in SLOT_HOURS:
        start = datetime(day.year, day.month, day.day, h, tzinfo=TZ)
        if start <= now() + timedelta(minutes=30):
            continue
        busy = q("SELECT COUNT(*) n FROM bookings WHERE start=? AND status IN ('new','confirmed')",
                 (start.isoformat(),), one=True)["n"]
        if busy < SLOT_CAPACITY:
            slots.append(start)
    return slots


def available_days():
    days, d = [], now().date()
    while len(days) < DAYS_AHEAD:
        if d.weekday() in WORK_DAYS and free_slots(d):
            days.append(d)
        d += timedelta(days=1)
        if (d - now().date()).days > 30:
            break
    return days


def screen_days(uid, cb=None):
    st = STATE.get(uid, {})
    days = available_days()
    labels = []
    for d in days:
        lbl = "Сегодня" if d == now().date() else ("Завтра" if d == now().date() + timedelta(days=1)
                                                   else f"{WEEKDAYS[d.weekday()]} {d:%d.%m}")
        labels.append((lbl, f"day:{d.isoformat()}"))
    rows = [labels[i:i + 2] for i in range(0, len(labels), 2)]
    rows.append([("◀️ Изменить авто", f"srv:{st.get('srv', '')}")])
    show(uid, cb, f"🚗 {st.get('car', '')}\n\n📅 Выберите день:", keyboard(rows))


def screen_times(uid, cb, day_iso):
    st = STATE.get(uid)
    if not st or "car" not in st:
        return screen_categories(uid, cb)
    day = date.fromisoformat(day_iso)
    slots = free_slots(day)
    if not slots:
        notify(cb, "На этот день всё занято")
        return screen_days(uid, cb)
    btns = [(f"{s:%H:%M}", f"time:{s.isoformat()}") for s in slots]
    rows = [btns[i:i + 3] for i in range(0, len(btns), 3)]
    rows.append([("◀️ Другой день", "days")])
    show(uid, cb, f"📅 {WEEKDAYS[day.weekday()]} {day:%d.%m}\n🕘 Свободное время:", keyboard(rows))


def screen_ask_phone(uid, cb, start_iso):
    st = STATE.get(uid)
    if not st or "car" not in st:
        return screen_categories(uid, cb)
    st.update(step="phone", start=start_iso)
    show(uid, cb, (f"🕘 {fmt_dt(start_iso)}\n\n"
                   "📞 Напишите номер телефона — мастер позвонит, если что-то уточнить."),
         keyboard([[("◀️ Другое время", "days")]]))


def screen_summary(uid):
    st = STATE[uid]
    s = SERVICES[st["srv"]]
    st["step"] = "summary"
    text = ("Проверьте запись:\n\n"
            f"🔧 {s['name']} — {s['price']}\n🚗 {st['car']}\n📅 {fmt_dt(st['start'])}\n📞 {st['phone']}")
    safe_send(uid, text, keyboard([
        [("✅ Записаться", "confirm")],
        [("✏️ Изменить", "book"), ("❌ Отмена", "menu")],
    ]))


def owner_card(b, demo_copy=False):
    head = ("👀 **ДЕМО: так заявку видит владелец сервиса.**\nПопробуйте нажать кнопки 👇\n\n"
            if demo_copy else "")
    text = f"{head}🆕 **Новая запись №{b['id']}**\n\n{booking_text(b)}\n\nСтатус: {STATUS_RU[b['status']]}"
    return text, owner_kb(b)


def owner_kb(b):
    i = b["id"]
    if b["status"] == "new":
        rows = [[("✅ Подтвердить", f"adm:ok:{i}"), ("❌ Отклонить", f"adm:no:{i}")]]
    elif b["status"] == "confirmed":
        rows = [[("🚗 Машина готова", f"adm:ready:{i}")], [("❌ Отменить", f"adm:no:{i}")]]
    else:
        rows = []
    if DEMO_MODE and b["status"] in ("new", "confirmed"):
        rows.append([("⏰ Пример напоминания", f"adm:remind:{i}")])
    return keyboard(rows) if rows else None


def do_confirm(uid, cb, user_name):
    st = STATE.get(uid)
    if not st or st.get("step") != "summary":
        return screen_menu(uid, cb)
    start = st["start"]
    busy = q("SELECT COUNT(*) n FROM bookings WHERE start=? AND status IN ('new','confirmed')",
             (start,), one=True)["n"]
    if busy >= SLOT_CAPACITY:
        notify(cb, "Это время только что заняли")
        st["step"] = "car"
        return screen_days(uid, cb)
    s = SERVICES[st["srv"]]
    bid = q("INSERT INTO bookings(user_id, service, price, car, phone, start, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (uid, s["name"], s["price"], st["car"], st["phone"], start, "new", now().isoformat()))
    STATE.pop(uid, None)
    b = q("SELECT * FROM bookings WHERE id=?", (bid,), one=True)
    show(uid, cb, (f"🎉 **Заявка №{bid} принята!**\n\n{booking_text(b)}\n\n"
                   "Мастер подтвердит запись, и вам придёт сообщение. "
                   f"За {int(REMIND_BEFORE.total_seconds() // 3600)} ч до визита напомним."),
         main_menu_kb())
    text, kb = owner_card(b)
    who = f"\n👤 {user_name}" if user_name else ""
    for a in ADMIN_IDS - {uid}:
        safe_send(a, text + who, kb)
    if DEMO_MODE:
        text, kb = owner_card(b, demo_copy=True)
        safe_send(uid, text, kb)
    log.info("Запись №%s от %s", bid, uid)


def screen_my(uid, cb):
    rows_db = q("SELECT * FROM bookings WHERE user_id=? AND status IN ('new','confirmed') "
                "AND start>=? ORDER BY start", (uid, now().isoformat()))
    if not rows_db:
        return show(uid, cb, "У вас пока нет активных записей.",
                    keyboard([[("📅 Записаться", "book")], [("◀️ Меню", "menu")]]))
    parts, rows = ["🗂 **Ваши записи:**"], []
    for b in rows_db:
        parts.append(f"\n**№{b['id']}** · {STATUS_RU[b['status']]}\n{booking_text(b)}")
        rows.append([(f"❌ Отменить №{b['id']}", f"cancel:{b['id']}")])
    rows.append([("◀️ Меню", "menu")])
    show(uid, cb, "\n".join(parts), keyboard(rows))


def do_cancel(uid, cb, bid):
    b = q("SELECT * FROM bookings WHERE id=? AND user_id=?", (bid, uid), one=True)
    if not b or b["status"] not in ("new", "confirmed"):
        notify(cb, "Запись не найдена")
        return screen_my(uid, None)
    q("UPDATE bookings SET status='cancelled' WHERE id=?", (bid,))
    notify(cb, "Запись отменена")
    for a in ADMIN_IDS - {uid}:
        safe_send(a, f"⚪️ Клиент отменил запись №{bid}\n\n{booking_text(b)}")
    screen_my(uid, cb)

# ─────────────────────────── Действия владельца ───────────────────────────


def owner_action(uid, cb, action, bid):
    b = q("SELECT * FROM bookings WHERE id=?", (bid,), one=True)
    if not b:
        return notify(cb, "Заявка не найдена")
    if not is_owner(uid, b):
        return notify(cb, "Эта кнопка только для владельца")
    client = b["user_id"]
    demo_self = DEMO_MODE and client == uid and uid not in ADMIN_IDS
    tag = "\n\n_👆 Это сообщение получает клиент._" if demo_self else ""

    if action == "ok" and b["status"] == "new":
        q("UPDATE bookings SET status='confirmed' WHERE id=?", (bid,))
        safe_send(client, f"✅ **Запись №{bid} подтверждена!**\n\n{booking_text(b)}\n\n"
                          f"Ждём вас: {BUSINESS['address']}{tag}")
    elif action == "no" and b["status"] in ("new", "confirmed"):
        q("UPDATE bookings SET status='cancelled' WHERE id=?", (bid,))
        safe_send(client, f"😔 К сожалению, запись №{bid} отменена мастером. "
                          f"Выберите другое время или позвоните: {BUSINESS['phone']}{tag}",
                  keyboard([[("📅 Записаться снова", "book")]]))
    elif action == "ready" and b["status"] == "confirmed":
        q("UPDATE bookings SET status='done' WHERE id=?", (bid,))
        safe_send(client, f"🚗 **Ваш автомобиль готов!** Можно забирать.\n\n"
                          f"Оцените, пожалуйста, нашу работу:{tag}",
                  keyboard([[(f"{n}⭐", f"rate:{bid}:{n}") for n in range(1, 6)]]))
    elif action == "remind":
        safe_send(client, f"⏰ **Напоминаем о записи** в {BUSINESS['name']}\n\n{booking_text(b)}\n\n"
                          f"Адрес: {BUSINESS['address']}{tag}",
                  keyboard([[("🗂 Мои записи", "my")]]))
        return notify(cb, "Напоминание отправлено")
    else:
        return notify(cb, "Уже обработано")

    b = q("SELECT * FROM bookings WHERE id=?", (bid,), one=True)
    text, kb = owner_card(b, demo_copy=demo_self)
    show(uid, cb, text, kb)


def do_rate(uid, cb, bid, stars):
    b = q("SELECT * FROM bookings WHERE id=? AND user_id=?", (bid, uid), one=True)
    if not b:
        return notify(cb, "Запись не найдена")
    q("UPDATE bookings SET rating=? WHERE id=?", (stars, bid))
    for a in ADMIN_IDS - {uid}:
        safe_send(a, f"{'⭐' * stars} Оценка по записи №{bid} ({b['service']})")
    if stars >= 4:
        show(uid, cb, "Спасибо за высокую оценку! 🙏 Будем рады видеть вас снова.", main_menu_kb())
    else:
        STATE[uid] = {"step": "feedback", "bid": bid}
        show(uid, cb, "Спасибо за честность. Напишите, что нам улучшить, — владелец прочитает лично.")

# ─────────────────────────── Обработчики событий ───────────────────────────


def user_name(u):
    u = u or {}
    return u.get("name") or " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x)


def on_started(update):
    u = update.get("user") or {}
    uid = u.get("user_id")
    if not uid:
        return
    name, source = user_name(u), update.get("payload")
    new = q("SELECT 1 FROM users WHERE id=?", (uid,), one=True) is None
    if new:
        q("INSERT OR IGNORE INTO users(id, name, source, first_at) VALUES (?,?,?,?)",
          (uid, name, source, now().isoformat()))
        if uid not in ADMIN_IDS:
            src = f"\nИсточник: {source}" if source else ""
            for a in ADMIN_IDS:
                safe_send(a, f"👤 Новый человек открыл демо: **{name or 'без имени'}** (id {uid}){src}\n"
                             "Хороший момент написать ему 😉")
    if not ADMIN_IDS:
        safe_send(uid, f"Ваш ID в MAX: `{uid}`\nВпишите его в ADMIN_IDS в файле .env и перезапустите бота.")
    screen_menu(uid)


def on_callback(update):
    cbk = update.get("callback") or {}
    uid = (cbk.get("user") or {}).get("user_id")
    cb, p = cbk.get("callback_id"), cbk.get("payload") or ""
    if not uid:
        return
    parts = p.split(":")
    try:
        if p == "menu":
            screen_menu(uid, cb)
        elif p == "prices":
            screen_prices(uid, cb)
        elif p == "info":
            screen_info(uid, cb)
        elif p == "book":
            screen_categories(uid, cb)
        elif p == "my":
            screen_my(uid, cb)
        elif p == "days":
            screen_days(uid, cb)
        elif p == "ask":
            STATE[uid] = {"step": "question"}
            show(uid, cb, "✍️ Напишите вопрос одним сообщением — мастер ответит здесь же.",
                 keyboard([[("◀️ Меню", "menu")]]))
        elif p == "confirm":
            do_confirm(uid, cb, user_name(cbk.get("user")))
        elif parts[0] == "cat":
            screen_services(uid, cb, parts[1])
        elif parts[0] == "srv":
            screen_ask_car(uid, cb, parts[1])
        elif parts[0] == "day":
            screen_times(uid, cb, parts[1])
        elif parts[0] == "time":
            screen_ask_phone(uid, cb, p[len("time:"):])
        elif parts[0] == "cancel":
            do_cancel(uid, cb, int(parts[1]))
        elif parts[0] == "rate":
            do_rate(uid, cb, int(parts[1]), int(parts[2]))
        elif parts[0] == "adm" and parts[1] == "reply":
            target = int(parts[2])
            if uid in ADMIN_IDS or (DEMO_MODE and target == uid):
                STATE[uid] = {"step": "reply", "to": target}
                show(uid, cb, "✍️ Напишите ответ клиенту одним сообщением.")
            else:
                notify(cb, "Эта кнопка только для владельца")
        elif parts[0] == "adm":
            owner_action(uid, cb, parts[1], int(parts[2]))
        else:
            screen_menu(uid, cb)
    except (ValueError, IndexError):
        screen_menu(uid, cb)


def on_message(update):
    m = update.get("message") or {}
    sender = m.get("sender") or {}
    if sender.get("is_bot") or (m.get("recipient") or {}).get("chat_type") not in (None, "dialog"):
        return
    uid = sender.get("user_id")
    text = ((m.get("body") or {}).get("text") or "").strip()
    if not uid or not text:
        return

    if uid in ADMIN_IDS and text.startswith("/"):
        return admin_command(uid, text)
    if text.lower() in ("/start", "start", "меню", "начать"):
        return screen_menu(uid)

    st = STATE.get(uid) or {}
    step = st.get("step")
    if step == "car":
        st.update(car=text[:60], step="day")
        return screen_days(uid)
    if step == "phone":
        digits = re.sub(r"\D", "", text)
        if len(digits) < 10:
            return safe_send(uid, "Похоже, в номере ошибка. Напишите, пожалуйста, например: +7 912 345-67-89")
        st["phone"] = text[:20]
        return screen_summary(uid)
    if step == "question":
        STATE.pop(uid, None)
        name = user_name(sender) or "Клиент"
        card = f"❓ **Вопрос от клиента** {name}:\n\n{text}"
        kb = keyboard([[("↩️ Ответить", f"adm:reply:{uid}")]])
        for a in ADMIN_IDS - {uid}:
            safe_send(a, card, kb)
        safe_send(uid, "Спасибо! Мастер ответит в ближайшее время 👌", main_menu_kb())
        if DEMO_MODE:
            safe_send(uid, "👀 **ДЕМО: так вопрос видит владелец.**\n\n" + card, kb)
        return
    if step == "reply":
        STATE.pop(uid, None)
        tag = "\n\n_👆 Это сообщение получает клиент._" if DEMO_MODE and st["to"] == uid else ""
        safe_send(st["to"], f"💬 **Ответ мастера {BUSINESS['name']}:**\n\n{text}{tag}", main_menu_kb())
        if st["to"] != uid:
            safe_send(uid, "Ответ отправлен ✅")
        return
    if step == "feedback":
        STATE.pop(uid, None)
        for a in ADMIN_IDS - {uid}:
            safe_send(a, f"📝 Отзыв по записи №{st['bid']}:\n\n{text}")
        return safe_send(uid, "Спасибо, передали владельцу 🙏", main_menu_kb())

    screen_menu(uid)


HELP = ("Команды владельца:\n"
        "/today — записи на сегодня\n"
        "/tomorrow — записи на завтра\n"
        "/stats — сколько людей открыло демо и записалось")


def admin_command(uid, text):
    cmd = text.split()[0].lower()
    if cmd in ("/today", "/tomorrow"):
        d = now().date() + timedelta(days=1 if cmd == "/tomorrow" else 0)
        start = datetime(d.year, d.month, d.day, tzinfo=TZ)
        rows = q("SELECT * FROM bookings WHERE start>=? AND start<? AND status IN ('new','confirmed','done') "
                 "ORDER BY start", (start.isoformat(), (start + timedelta(days=1)).isoformat()))
        if not rows:
            return safe_send(uid, f"На {d:%d.%m} записей нет.")
        for b in rows:
            text, kb = owner_card(b)
            safe_send(uid, text.replace("🆕 **Новая запись", "**Запись"), kb)
    elif cmd == "/stats":
        users = q("SELECT COUNT(*) n FROM users", one=True)["n"]
        week = q("SELECT COUNT(*) n FROM users WHERE first_at>=?",
                 ((now() - timedelta(days=7)).isoformat(),), one=True)["n"]
        books = q("SELECT COUNT(*) n FROM bookings", one=True)["n"]
        safe_send(uid, f"📊 Открыли бота: {users} (за неделю {week})\nЗаписей создано: {books}\nВерсия: {VERSION}")
    else:
        safe_send(uid, HELP)

# ─────────────────────────── Напоминания ───────────────────────────


def reminder_loop():
    while True:
        try:
            limit = (now() + REMIND_BEFORE).isoformat()
            for b in q("SELECT * FROM bookings WHERE status='confirmed' AND reminded=0 AND start<=? AND start>?",
                       (limit, now().isoformat())):
                safe_send(b["user_id"], f"⏰ **Напоминаем о записи** в {BUSINESS['name']}\n\n{booking_text(b)}\n\n"
                                        f"Адрес: {BUSINESS['address']}", keyboard([[("🗂 Мои записи", "my")]]))
                q("UPDATE bookings SET reminded=1 WHERE id=?", (b["id"],))
        except Exception as e:  # noqa: BLE001
            log.exception("Ошибка напоминаний: %s", e)
        time.sleep(60)

# ─────────────────────────── Главный цикл ───────────────────────────


HANDLERS = {"bot_started": on_started, "message_callback": on_callback, "message_created": on_message}


def main():
    if not TOKEN:
        raise SystemExit("Не задан MAX_TOKEN в .env")
    init_db()
    threading.Thread(target=reminder_loop, daemon=True).start()
    log.info("Демо-бот %s запущен (версия %s, демо-режим: %s)", BUSINESS["name"], VERSION, DEMO_MODE)
    marker = None
    while True:
        try:
            params = {"timeout": 30, "types": ",".join(HANDLERS)}
            if marker is not None:
                params["marker"] = marker
            data = mx.call("GET", "/updates", params=params, timeout=45)
            marker = data.get("marker", marker)
            for u in data.get("updates") or []:
                h = HANDLERS.get(u.get("update_type"))
                if h:
                    try:
                        h(u)
                    except Exception as e:  # noqa: BLE001
                        log.exception("Ошибка обработки %s: %s", u.get("update_type"), e)
        except requests.RequestException as e:
            log.warning("Сеть: %s", e)
            time.sleep(3)
        except Exception as e:  # noqa: BLE001
            log.warning("Ошибка API: %s", e)
            time.sleep(5)


if __name__ == "__main__":
    main()
