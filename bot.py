import asyncio
import os
import re
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ChatPermissions
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage
from dotenv import load_dotenv
import sqlite3
from datetime import datetime
import threading

# Загружаем настройки
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(id.strip()) for id in os.getenv("ADMIN_IDS", "").split(",") if id.strip()]

# Создаём бота
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# База данных
DB_NAME = "masters_stats.db"

# Создаём блокировку для базы данных
db_lock = threading.Lock()

# Хранилище активных заказов и уведомлений
active_orders = {}
pending_notifications = {}

# Состояния для админских команд
class AdminStates(StatesGroup):
    waiting_for_master_id_for_commission = State()
    waiting_for_commission = State()

# ========== ФУНКЦИИ БАЗЫ ДАННЫХ ==========
def init_db():
    """Инициализация базы данных с блокировкой"""
    with db_lock:
        with sqlite3.connect(DB_NAME, timeout=10) as conn:
            # Создаём основную таблицу
            conn.execute("""
                CREATE TABLE IF NOT EXISTS masters (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    full_name TEXT,
                    orders_taken INTEGER DEFAULT 0,
                    total_commission REAL DEFAULT 0,
                    rating REAL DEFAULT 0,
                    last_active TIMESTAMP
                )
            """)
            
            # Создаём таблицу заказов
            conn.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    master_id INTEGER,
                    order_text TEXT,
                    commission REAL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    completed BOOLEAN DEFAULT 0
                )
            """)
            
            # Создаём таблицу уведомлений
            conn.execute("""
                CREATE TABLE IF NOT EXISTS notification_queue (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER,
                    master_id INTEGER,
                    sent BOOLEAN DEFAULT 0,
                    sent_at TIMESTAMP
                )
            """)
            
            # Добавляем недостающие колонки (для обратной совместимости)
            try:
                conn.execute("ALTER TABLE masters ADD COLUMN last_active TIMESTAMP")
                print("✅ Добавлена колонка last_active")
            except sqlite3.OperationalError:
                pass
            
            try:
                conn.execute("ALTER TABLE masters ADD COLUMN rating REAL DEFAULT 0")
                print("✅ Добавлена колонка rating")
            except sqlite3.OperationalError:
                pass
            
            try:
                conn.execute("ALTER TABLE masters ADD COLUMN total_commission REAL DEFAULT 0")
                print("✅ Добавлена колонка total_commission")
            except sqlite3.OperationalError:
                pass
            
            # Обновляем существующие записи
            conn.execute("""
                UPDATE masters SET last_active = CURRENT_TIMESTAMP 
                WHERE last_active IS NULL
            """)
                
    print("✅ База данных готова")

def get_top_masters(limit=2):
    """Возвращает топ N мастеров по рейтингу (топ-2)"""
    with db_lock:
        with sqlite3.connect(DB_NAME, timeout=10) as conn:
            cur = conn.execute("""
                SELECT user_id, username, full_name, orders_taken, total_commission, rating 
                FROM masters 
                WHERE orders_taken > 0 
                ORDER BY rating DESC 
                LIMIT ?
            """, (limit,))
            return cur.fetchall()

def update_rating(user_id):
    """Обновляет рейтинг мастера с блокировкой"""
    with db_lock:
        with sqlite3.connect(DB_NAME, timeout=10) as conn:
            cur = conn.execute(
                "SELECT total_commission, orders_taken FROM masters WHERE user_id = ?",
                (user_id,)
            )
            result = cur.fetchone()
            if result and result[1] > 0:
                rating = result[0] / result[1]
                conn.execute(
                    "UPDATE masters SET rating = ? WHERE user_id = ?",
                    (rating, user_id)
                )
                return rating
    return 0

def add_commission_to_master(user_id, commission):
    """Добавляет комиссию мастеру с блокировкой"""
    with db_lock:
        with sqlite3.connect(DB_NAME, timeout=10) as conn:
            conn.execute(
                "UPDATE masters SET total_commission = total_commission + ? WHERE user_id = ?",
                (commission, user_id)
            )
    update_rating(user_id)

def get_all_masters():
    """Получает всех мастеров с блокировкой"""
    with db_lock:
        with sqlite3.connect(DB_NAME, timeout=10) as conn:
            cur = conn.execute("""
                SELECT user_id, username, full_name, orders_taken, total_commission, rating 
                FROM masters 
                ORDER BY rating DESC
            """)
            return cur.fetchall()

def get_or_create_master(user_id, username, full_name):
    """Создаёт мастера если его нет с блокировкой"""
    with db_lock:
        with sqlite3.connect(DB_NAME, timeout=10) as conn:
            cur = conn.execute("SELECT user_id FROM masters WHERE user_id = ?", (user_id,))
            if not cur.fetchone():
                conn.execute("""
                    INSERT INTO masters (user_id, username, full_name, last_active) 
                    VALUES (?, ?, ?, ?)
                """, (user_id, username, full_name, datetime.now()))
                print(f"➕ Новый мастер: {full_name}")

def increment_orders(user_id, commission=0):
    """Увеличивает счётчик заказов с блокировкой"""
    with db_lock:
        with sqlite3.connect(DB_NAME, timeout=10) as conn:
            conn.execute("""
                UPDATE masters 
                SET orders_taken = orders_taken + 1, 
                    last_active = CURRENT_TIMESTAMP
                WHERE user_id = ?
            """, (user_id,))
            
            if commission > 0:
                conn.execute("""
                    UPDATE masters 
                    SET total_commission = total_commission + ?
                    WHERE user_id = ?
                """, (commission, user_id))
    
    update_rating(user_id)

# ========== ФУНКЦИИ ДЛЯ РАБОТЫ С ЗАКАЗАМИ ==========
async def send_notification_to_master(master_id, order_text, contacts, order_id):
    """Отправляет уведомление конкретному мастеру"""
    try:
        message = format_master_notification(order_text, contacts)
        await bot.send_message(master_id, message, parse_mode="Markdown")
        
        with db_lock:
            with sqlite3.connect(DB_NAME, timeout=10) as conn:
                conn.execute("""
                    INSERT INTO notification_queue (order_id, master_id, sent, sent_at)
                    VALUES (?, ?, 1, ?)
                """, (order_id, master_id, datetime.now()))
        
        return True
    except Exception as e:
        print(f"Не удалось отправить уведомление мастеру {master_id}: {e}")
        return False

async def send_delayed_notifications(order_id, excluded_masters, order_text, contacts):
    """Отправляет уведомления всем мастерам через 1 минуту (кроме топ-2)"""
    await asyncio.sleep(60)  # 1 минута
    
    if order_id not in active_orders or active_orders[order_id]["taken"]:
        return
    
    with db_lock:
        with sqlite3.connect(DB_NAME, timeout=10) as conn:
            cur = conn.execute("SELECT user_id FROM masters")
            all_masters = cur.fetchall()
    
    for (master_id,) in all_masters:
        if master_id not in excluded_masters:
            await send_notification_to_master(master_id, order_text, contacts, order_id)
    
    if order_id in pending_notifications:
        del pending_notifications[order_id]

def extract_contacts(text):
    contacts = {
        "phone": None,
        "address": None,
        "name": None,
        "full_text": text
    }
    
    phone_pattern = r'(\+?\d[\d\s\-\(\)]{8,}\d)'
    phone_match = re.search(phone_pattern, text)
    if phone_match:
        contacts["phone"] = phone_match.group(1).strip()
    
    address_pattern = r'(?:адрес|по адресу|ул\.|улица|пр\.|проспект|локация)[:\s]*([^\n]+)'
    address_match = re.search(address_pattern, text, re.IGNORECASE)
    if address_match:
        contacts["address"] = address_match.group(1).strip()
    
    name_pattern = r'(?:имя|клиент|заказчик|контактное лицо)[:\s]*([А-Яа-яA-Za-z\s]+?)(?:\n|,|$)'
    name_match = re.search(name_pattern, text, re.IGNORECASE)
    if name_match:
        contacts["name"] = name_match.group(1).strip()
    
    return contacts

def hide_phone_in_text(text, phone):
    if phone:
        hidden_text = text.replace(phone, "[📞 ТЕЛЕФОН СКРЫТ]")
        phone_clean = re.sub(r'[\s\-\(\)]', '', phone)
        if phone_clean != phone:
            hidden_text = hidden_text.replace(phone_clean, "[📞 ТЕЛЕФОН СКРЫТ]")
    else:
        hidden_text = text
    return hidden_text

def format_public_order(text, contacts):
    public_text = hide_phone_in_text(text, contacts["phone"])
    
    if contacts["phone"]:
        phone_notice = "\n\n🔒 *Телефон скрыт! Нажмите «ВЗЯТЬ ЗАКАЗ» чтобы увидеть контакты.*"
    else:
        phone_notice = "\n\n⚠️ *В заказе не указан телефон!*"
    
    return f"📦 **НОВЫЙ ЗАКАЗ**{phone_notice}\n\n{public_text}"

def format_master_message(order_text, contacts):
    message = "✅ **ВЫ ВЗЯЛИ ЗАКАЗ!**\n\n"
    message += "📋 **ДЕТАЛИ ЗАКАЗА:**\n"
    message += "─" * 20 + "\n"
    message += f"{order_text}\n\n"
    
    message += "📞 **КОНТАКТЫ КЛИЕНТА:**\n"
    message += "─" * 20 + "\n"
    
    if contacts["name"]:
        message += f"👤 *Имя:* {contacts['name']}\n"
    if contacts["phone"]:
        message += f"📱 *Телефон:* `{contacts['phone']}`\n"
    if contacts["address"]:
        message += f"📍 *Адрес:* {contacts['address']}\n"
    
    if not any([contacts["name"], contacts["phone"], contacts["address"]]):
        message += "⚠️ В заказе не указаны контакты!\n"
        message += "Свяжитесь с администратором.\n"
    
    message += "\n" + "─" * 20 + "\n"
    message += "📌 Свяжитесь с клиентом и выполните заказ!"
    
    return message

def format_master_notification(order_text, contacts):
    """Форматирует сообщение для уведомления мастера о новом заказе"""
    message = "🔔 **НОВЫЙ ЗАКАЗ ДОСТУПЕН!**\n\n"
    message += "📋 **ДЕТАЛИ ЗАКАЗА:**\n"
    message += "─" * 20 + "\n"
    message += f"{order_text}\n\n"
    
    message += "📞 **КОНТАКТЫ КЛИЕНТА:**\n"
    message += "─" * 20 + "\n"
    
    if contacts["name"]:
        message += f"👤 *Имя:* {contacts['name']}\n"
    if contacts["phone"]:
        message += f"📱 *Телефон:* `{contacts['phone']}`\n"
    if contacts["address"]:
        message += f"📍 *Адрес:* {contacts['address']}\n"
    
    message += "\n" + "─" * 20 + "\n"
    message += "👉 *Перейдите в группу и нажмите «ВЗЯТЬ ЗАКАЗ»!*"
    
    return message

# ========== КОМАНДЫ ==========
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    get_or_create_master(message.from_user.id, message.from_user.username, message.from_user.full_name)
    
    await message.answer(
        "🤖 **KyrgyzMasterBot**\n\n"
        "✅ **Вы успешно зарегистрированы!**\n\n"
        "🔒 **Система скрытых контактов:**\n"
        "• Номера телефонов НЕ видны в группе\n"
        "• Только мастер, взявший заказ, получает телефон в ЛС\n\n"
        "🏆 **Система рейтинга:**\n"
        "• Рейтинг = Общая комиссия / Количество заказов\n"
        "• 🥇🥈 *Топ-2 мастера* получают заказы МГНОВЕННО\n"  # ⬅️ ИСПРАВЛЕНО
        "• 👤 Остальные мастера — через 1 минуту\n\n"  # ⬅️ ИСПРАВЛЕНО
        "📌 **Команды:**\n"
        "• `/my_rating` — узнать свой рейтинг\n"
        "• `/top` — топ-10 мастеров\n\n"
        "👑 **Админ-команды:**\n"
        "• `/add_commission` — добавить комиссию\n"
        "• `/stats` — полная статистика\n\n"
        "🚀 Готово!",
        parse_mode="Markdown"
    )

@dp.message(Command("my_rating"))
async def cmd_my_rating(message: types.Message):
    user_id = message.from_user.id
    
    with db_lock:
        with sqlite3.connect(DB_NAME, timeout=10) as conn:
            cur = conn.execute("""
                SELECT user_id, username, full_name, orders_taken, total_commission, rating 
                FROM masters 
                WHERE user_id = ?
            """, (user_id,))
            my_data = cur.fetchone()
            
            cur = conn.execute("""
                SELECT user_id, rating, orders_taken, total_commission
                FROM masters 
                WHERE orders_taken > 0 
                ORDER BY rating DESC
            """)
            all_masters = cur.fetchall()
    
    if not my_data or my_data[3] == 0:
        await message.answer(
            "📊 **У вас пока нет рейтинга**\n\n"
            "Выполните первый заказ, чтобы появился рейтинг!\n\n"
            "🏆 Топ-2 мастера получают заказы мгновенно!\n"  # ⬅️ ИСПРАВЛЕНО
            "Команда `/top` — посмотреть топ мастеров",
            parse_mode="Markdown"
        )
        return
    
    my_user_id, my_username, my_full_name, my_orders, my_commission, my_rating = my_data
    
    my_position = None
    for i, master in enumerate(all_masters, 1):
        if master[0] == my_user_id:
            my_position = i
            break
    
    text = "📊 **ВАШ РЕЙТИНГ**\n\n"
    text += f"👤 *Мастер:* {f'@{my_username}' if my_username else my_full_name}\n"
    text += f"📦 *Заказов:* `{my_orders}`\n"
    text += f"💰 *Комиссия:* `{my_commission:.0f}` сом\n"
    text += f"⭐ *Рейтинг:* `{my_rating:.2f}`\n"
    
    if my_position:
        text += f"📊 *Место:* `{my_position}` из `{len(all_masters)}`\n"
        
        if my_position <= 2:
            text += "\n🎉 *Вы в топ-2!* Получаете заказы мгновенно!\n"
        else:
            text += "\n⏰ Заказы приходят через 1 минуту\n"
    
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("top"))
async def cmd_top(message: types.Message):
    with db_lock:
        with sqlite3.connect(DB_NAME, timeout=10) as conn:
            cur = conn.execute("""
                SELECT user_id, username, full_name, orders_taken, total_commission, rating 
                FROM masters 
                WHERE orders_taken > 0 
                ORDER BY rating DESC 
                LIMIT 10
            """)
            top_masters = cur.fetchall()
    
    if not top_masters:
        await message.answer("📊 Пока нет мастеров с рейтингом")
        return
    
    text = "🏆 **ТОП-10 МАСТЕРОВ**\n\n"
    
    for i, (user_id, username, full_name, orders, commission, rating) in enumerate(top_masters, 1):
        medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
        name = f"@{username}" if username else full_name
        text += f"{medal} *{name}*\n"
        text += f"   ⭐ Рейтинг: `{rating:.2f}` | 📦 {orders} зак. | 💰 {commission:.0f} сом\n\n"
    
    await message.answer(text, parse_mode="Markdown")

# ========== АДМИН-КОМАНДЫ ==========
@dp.message(Command("add_commission"))
async def cmd_add_commission(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ Только для администраторов!")
        return
    
    stats = get_all_masters()
    if not stats:
        await message.answer("📊 Нет зарегистрированных мастеров")
        return
    
    text = "📋 **Список мастеров:**\n\n"
    for user_id, username, full_name, orders, commission, rating in stats:
        mention = f"@{username}" if username else full_name
        text += f"🆔 ID: `{user_id}` - {mention}\n"
        text += f"   📦 Заказов: {orders} | 💰 Комиссия: {commission:.0f} сом | ⭐ Рейтинг: {rating:.2f}\n\n"
    
    text += "\n✍️ *Отправьте ID мастера, которому хотите добавить комиссию*"
    await message.answer(text, parse_mode="Markdown")
    await state.set_state(AdminStates.waiting_for_master_id_for_commission)

@dp.message(AdminStates.waiting_for_master_id_for_commission)
async def process_master_id_for_commission(message: types.Message, state: FSMContext):
    try:
        master_id = int(message.text.strip())
        await state.update_data(master_id=master_id)
        await message.answer("💰 Введите сумму комиссии в сомах (например: 500)")
        await state.set_state(AdminStates.waiting_for_commission)
    except:
        await message.answer("❌ Неверный ID. Отправьте число.")
        await state.clear()

@dp.message(AdminStates.waiting_for_commission)
async def process_commission_amount(message: types.Message, state: FSMContext):
    text = message.text.strip()
    text = text.replace(" ", "").replace(",", ".").replace("сом", "").strip()
    
    try:
        commission = float(text)
        
        if commission <= 0:
            await message.answer("❌ Сумма должна быть больше 0!")
            return
        
        data = await state.get_data()
        master_id = data["master_id"]
        
        add_commission_to_master(master_id, commission)
        
        with db_lock:
            with sqlite3.connect(DB_NAME, timeout=10) as conn:
                cur = conn.execute("SELECT username, full_name, total_commission, orders_taken, rating FROM masters WHERE user_id = ?", (master_id,))
                master = cur.fetchone()
        
        await message.answer(
            f"✅ **Комиссия добавлена!**\n\n"
            f"👤 Мастер: @{master[0] if master[0] else master[1]}\n"
            f"💰 Добавлено: {commission:.0f} сом\n"
            f"📊 Общая комиссия: {master[2]:.0f} сом\n"
            f"📦 Заказов: {master[3]}\n"
            f"⭐ Новый рейтинг: {master[4]:.2f}"
        )
        await state.clear()
        
    except ValueError:
        await message.answer("❌ Неверный формат! Введите число (например: 500)")

@dp.message(Command("stats"))
async def cmd_stats(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ Только для администраторов!")
        return
    
    stats = get_all_masters()
    if not stats:
        await message.answer("📊 Нет данных")
        return
    
    text = "📈 **ПОЛНАЯ СТАТИСТИКА:**\n\n"
    for user_id, username, full_name, orders, commission, rating in stats:
        mention = f"@{username}" if username else full_name
        text += f"👤 {mention}\n"
        text += f"   📦 Заказов: {orders}\n"
        text += f"   💰 Комиссия: {commission:.0f} сом\n"
        text += f"   ⭐ Рейтинг: {rating:.2f}\n\n"
    
    await message.answer(text, parse_mode="Markdown")

@dp.message(Command("set_group"))
async def cmd_set_group(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ Только для администраторов!")
        return
    
    if message.chat.type not in ["group", "supergroup"]:
        await message.answer("❌ Эту команду нужно использовать в группе!")
        return
    
    try:
        await bot.set_chat_permissions(
            chat_id=message.chat.id,
            permissions=ChatPermissions(
                can_send_messages=False,
                can_send_media=False,
                can_send_polls=False,
                can_send_other_messages=False,
                can_add_web_page_previews=False,
                can_change_info=False,
                can_invite_users=True,
                can_pin_messages=False
            )
        )
        
        for admin_id in ADMIN_IDS:
            try:
                await bot.promote_chat_member(
                    chat_id=message.chat.id,
                    user_id=admin_id,
                    can_send_messages=True,
                    can_send_media=True,
                    can_send_polls=True,
                    can_send_other_messages=True,
                    can_add_web_page_previews=True,
                    can_change_info=False,
                    can_invite_users=True,
                    can_pin_messages=True
                )
            except:
                pass
        
        await message.answer("✅ Группа настроена!")
    except Exception as e:
        await message.answer(f"❌ Ошибка: {e}")

# ========== ОБРАБОТКА ЗАКАЗОВ В ГРУППЕ ==========
@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def handle_order(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.delete()
        return
    
    if message.text and message.text.startswith("/"):
        return
    
    contacts = extract_contacts(message.text)
    public_text = format_public_order(message.text, contacts)
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ ВЗЯТЬ ЗАКАЗ", callback_data="take_order")]
    ])
    
    new_msg = await message.answer(public_text, reply_markup=keyboard, parse_mode="Markdown")
    await message.delete()
    
    # Получаем топ-2 мастеров
    top_masters = get_top_masters(2)
    top_master_ids = [m[0] for m in top_masters]
    
    active_orders[new_msg.message_id] = {
        "chat_id": message.chat.id,
        "full_text": message.text,
        "contacts": contacts,
        "taken": False,
        "top_masters": top_master_ids
    }
    
    # Отправляем уведомления топ-2 мастерам
    for master in top_masters:
        master_id, username, full_name, _, _, _ = master
        await send_notification_to_master(master_id, message.text, contacts, new_msg.message_id)
    
    # Запускаем отложенную рассылку для остальных (через 1 минуту)
    if len(top_master_ids) < len(get_all_masters()):
        task = asyncio.create_task(
            send_delayed_notifications(new_msg.message_id, top_master_ids, message.text, contacts)
        )
        pending_notifications[new_msg.message_id] = task
    
    # Уведомление админу
    await bot.send_message(
        message.from_user.id,
        f"✅ Заказ опубликован!\n\n"
        f"🔔 Уведомления отправлены топ-{len(top_masters)} мастерам\n"
        f"⏰ Остальным мастерам придет через 1 минуту\n"  # ⬅️ ИСПРАВЛЕНО
    )

@dp.callback_query(F.data == "take_order")
async def take_order(callback: types.CallbackQuery):
    user = callback.from_user
    msg_id = callback.message.message_id
    
    if msg_id not in active_orders:
        await callback.answer("❌ Этот заказ уже кто-то взял!", show_alert=True)
        return
    
    order_data = active_orders[msg_id]
    if order_data["taken"]:
        await callback.answer("❌ Заказ уже взят!", show_alert=True)
        return
    
    order_data["taken"] = True
    
    # Отменяем отложенную рассылку
    if msg_id in pending_notifications:
        pending_notifications[msg_id].cancel()
        del pending_notifications[msg_id]
    
    get_or_create_master(user.id, user.username, user.full_name or user.first_name)
    
    try:
        master_message = format_master_message(order_data["full_text"], order_data["contacts"])
        await bot.send_message(user.id, master_message, parse_mode="Markdown")
        
        increment_orders(user.id, 0)
        
        # Удаляем сообщение из группы
        try:
            await bot.delete_message(
                chat_id=callback.message.chat.id,
                message_id=callback.message.message_id
            )
        except Exception as e:
            print(f"❌ Не удалось удалить: {e}")
        
        del active_orders[msg_id]
        
        await callback.answer("✅ Вы взяли заказ! Контакты в ЛС.", show_alert=True)
        
        await bot.send_message(
            callback.message.chat.id, 
            f"✅ Заказ взят мастером @{user.username if user.username else user.first_name}"
        )
        
    except Exception as e:
        if "can't initiate conversation" in str(e):
            await callback.answer("❌ Напишите боту /start в ЛС!", show_alert=True)
            order_data["taken"] = False
        else:
            await callback.answer(f"❌ Ошибка: {str(e)[:50]}", show_alert=True)

# ========== ЗАПУСК ==========
async def main():
    init_db()
    bot_info = await bot.get_me()
    print("="*50)
    print(f"🤖 Бот @{bot_info.username} запущен!")
    print(f"👑 Админы: {ADMIN_IDS}")
    print("="*50)
    print("\n🏆 НАСТРОЙКИ РЕЙТИНГА:")
    print("   • Топ-2 мастера — уведомления МГНОВЕННО")
    print("   • Остальные мастера — через 1 МИНУТУ")
    print("="*50)
    print("\n✅ Доступные команды:")
    print("   /add_commission - добавить комиссию мастеру")
    print("   /stats - статистика")
    print("   /my_rating - свой рейтинг")
    print("   /top - топ мастеров")
    print("="*50)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())