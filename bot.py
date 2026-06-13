import asyncio
import os
import re
import uuid
import sqlite3
import threading
from datetime import datetime
from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import (
    InlineKeyboardMarkup, 
    InlineKeyboardButton, 
    ChatPermissions,
    ReplyKeyboardMarkup,
    KeyboardButton,
    BotCommand
)
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage

# Загружаем настройки
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(id.strip()) for id in os.getenv("ADMIN_IDS", "").split(",") if id.strip()]

# Создаём бота
bot = Bot(token=BOT_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

DB_NAME = "masters_stats.db"
db_lock = threading.Lock()

# Глобальное хранилище активных заказов
active_orders = {}

class AdminStates(StatesGroup):
    waiting_for_commission = State()

# ==================== КНОПКИ ДЛЯ ИНТЕРФЕЙСА (ZERO TYPING) ====================

def get_menu_keyboard(user_id: int):
    """Возвращает разные кнопки в зависимости от того, Админ перед нами или Мастер"""
    if user_id in ADMIN_IDS:
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="➕ Начислить Рейтинг")],
                [KeyboardButton(text="📊 Общая Статистика"), KeyboardButton(text="🏆 Топ-10 Мастеров")]
            ],
            resize_keyboard=True,
            placeholder="Панель Управления"
        )
    else:
        return ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="⭐ Мой Рейтинг"), KeyboardButton(text="🏆 Топ-10 Мастеров")]
            ],
            resize_keyboard=True,
            placeholder="Выберите действие"
        )

# ==================== DATABASE LAYER ====================

def init_db():
    with db_lock:
        with sqlite3.connect(DB_NAME, timeout=10) as conn:
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
    print("✅ База данных готова")

def update_rating(user_id):
    with db_lock:
        with sqlite3.connect(DB_NAME, timeout=10) as conn:
            cur = conn.execute("SELECT total_commission, orders_taken FROM masters WHERE user_id = ?", (user_id,))
            result = cur.fetchone()
            if result and result[1] > 0:
                rating = result[0] / result[1]
                conn.execute("UPDATE masters SET rating = ? WHERE user_id = ?", (rating, user_id))
                return rating
    return 0

def add_commission_to_master(user_id, commission):
    with db_lock:
        with sqlite3.connect(DB_NAME, timeout=10) as conn:
            conn.execute("UPDATE masters SET total_commission = total_commission + ? WHERE user_id = ?", (commission, user_id))
    update_rating(user_id)

def get_all_masters():
    with db_lock:
        with sqlite3.connect(DB_NAME, timeout=10) as conn:
            cur = conn.execute("SELECT user_id, username, full_name, orders_taken, total_commission, rating FROM masters ORDER BY rating DESC")
            return cur.fetchall()

def get_or_create_master(user_id, username, full_name):
    with db_lock:
        with sqlite3.connect(DB_NAME, timeout=10) as conn:
            cur = conn.execute("SELECT user_id FROM masters WHERE user_id = ?", (user_id,))
            if not cur.fetchone():
                conn.execute("INSERT INTO masters (user_id, username, full_name, last_active) VALUES (?, ?, ?, ?)", (user_id, username, full_name, datetime.now()))

def increment_orders(user_id):
    with db_lock:
        with sqlite3.connect(DB_NAME, timeout=10) as conn:
            conn.execute("UPDATE masters SET orders_taken = orders_taken + 1, last_active = CURRENT_TIMESTAMP WHERE user_id = ?", (user_id,))
    update_rating(user_id)

def get_top_masters_ids(limit=2):
    with db_lock:
        with sqlite3.connect(DB_NAME, timeout=10) as conn:
            cur = conn.execute("SELECT user_id FROM masters WHERE orders_taken > 0 ORDER BY rating DESC LIMIT ?", (limit,))
            return [row[0] for row in cur.fetchall()]

# ==================== PARSING & FORMATTING ====================

def extract_contacts(text):
    contacts = {"phone": None, "address": None, "name": None, "full_text": text}
    phone_match = re.search(r'(\+?\d[\d\s\-\(\)]{8,}\d)', text)
    if phone_match: contacts["phone"] = phone_match.group(1).strip()
    
    address_match = re.search(r'(?:адрес|по адресу|ул\.|улица|пр\.|проспект|локация)[:\s]*([^\n]+)', text, re.IGNORECASE)
    if address_match: contacts["address"] = address_match.group(1).strip()
    
    name_match = re.search(r'(?:имя|клиент|заказчик|контактное лицо)[:\s]*([А-Яа-яA-Za-z\s]+?)(?:\n|,|$)', text, re.IGNORECASE)
    if name_match: contacts["name"] = name_match.group(1).strip()
    return contacts

def hide_phone_in_text(text, phone):
    if not phone: return text
    hidden_text = text.replace(phone, "[📞 ТЕЛЕФОН СКРЫТ]")
    phone_clean = re.sub(r'[\s\-\(\)]', '', phone)
    if phone_clean != phone:
        hidden_text = hidden_text.replace(phone_clean, "[📞 ТЕЛЕФОН СКРЫТ]")
    return hidden_text

# ==================== CORE ORDER STREAM LOGIC ====================

async def post_to_public_group(order_id: str):
    await asyncio.sleep(30)
    if order_id not in active_orders or active_orders[order_id]["taken"]: return
        
    order_data = active_orders[order_id]
    public_text = f"📦 **НОВЫЙ ЗАКАЗ ДЛЯ ВСЕХ**\n\n{hide_phone_in_text(order_data['full_text'], order_data['contacts']['phone'])}"
    keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ ВЗЯТЬ ЗАКАЗ", callback_data=f"take_{order_id}")]])
    
    try:
        new_msg = await bot.send_message(chat_id=order_data["chat_id"], text=public_text, reply_markup=keyboard, parse_mode="Markdown")
        order_data["public_message_id"] = new_msg.message_id
    except Exception as e:
        print(f"❌ Ошибка отправки в общую группу: {e}")

@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def handle_order(message: types.Message):
    if message.from_user.id not in ADMIN_IDS or (message.text and message.text.startswith("/")):
        return
    
    contacts = extract_contacts(message.text)
    order_id = str(uuid.uuid4())[:8]
    
    active_orders[order_id] = {
        "chat_id": message.chat.id,
        "public_message_id": None,
        "vip_messages": {},  
        "full_text": message.text,
        "contacts": contacts,
        "taken": False
    }
    
    await message.delete()
    
    top_masters = get_top_masters_ids(limit=2)
    vip_text = f"🔥 **ПРИОРИТЕТНЫЙ ЗАКАЗ (У вас есть 30 сек!)**\n\n{hide_phone_in_text(message.text, contacts['phone'])}"
    vip_keyboard = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⭐ ВЗЯТЬ ПРИОРИТЕТНО", callback_data=f"take_{order_id}")]])
    
    for master_id in top_masters:
        try:
            vip_msg = await bot.send_message(chat_id=master_id, text=vip_text, reply_markup=vip_keyboard, parse_mode="Markdown")
            active_orders[order_id]["vip_messages"][master_id] = vip_msg.message_id
        except Exception:
            pass
            
    asyncio.create_task(post_to_public_group(order_id))

@dp.callback_query(F.data.startswith("take_"))
async def take_order(callback: types.CallbackQuery):
    user = callback.from_user
    order_id = callback.data.split("_")[1]
    
    if order_id not in active_orders:
        await callback.answer("❌ Заказ уже взят или ссылка устарела!", show_alert=True)
        return
    
    order_data = active_orders[order_id]
    if order_data["taken"]:
        await callback.answer("❌ Этот заказ уже ушёл!", show_alert=True)
        return
    
    order_data["taken"] = True
    get_or_create_master(user.id, user.username, user.full_name or user.first_name)
    
    try:
        details = f"✅ **ВЫ ВЗЯЛИ ЗАКАЗ!**\n\n📋 **ДЕТАЛИ:**\n{order_data['full_text']}\n\n📱 **НОМЕР КЛИЕНТА:** `{order_data['contacts']['phone']}`"
        await bot.send_message(user.id, details, parse_mode="Markdown", reply_markup=get_menu_keyboard(user.id))
        
        increment_orders(user.id)
        
        for vip_id, msg_id in order_data["vip_messages"].items():
            try: await bot.delete_message(chat_id=vip_id, message_id=msg_id)
            except Exception: pass
                
        if order_data["public_message_id"]:
            try: await bot.delete_message(chat_id=order_data["chat_id"], message_id=order_data["public_message_id"])
            except Exception: pass
                
        await bot.send_message(order_data["chat_id"], f"🔒 Заказ взят мастером @{user.username if user.username else user.first_name}")
        del active_orders[order_id]
        await callback.answer("✅ Контакты отправлены в ваш личный чат с ботом!", show_alert=True)
        
    except Exception as e:
        order_data["taken"] = False
        await callback.answer("❌ Сначала запустите бота в ЛС (/start)", show_alert=True)

# ==================== CONTROLLERS (ZERO TYPING) ====================

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    uid = message.from_user.id
    if uid not in ADMIN_IDS:
        get_or_create_master(uid, message.from_user.username, message.from_user.full_name)
    
    welcome_text = (
        "👑 **Добро пожаловать в админ-панель!**\nНиже доступны все инструменты управления." 
        if uid in ADMIN_IDS else 
        "🤖 **KyrgyzMasterBot**\nИспользуйте меню ниже для проверки своей статистики."
    )
    await message.answer(welcome_text, reply_markup=get_menu_keyboard(uid), parse_mode="Markdown")

@dp.message(F.text == "⭐ Мой Рейтинг")
async def handle_rating_request(message: types.Message):
    user_id = message.from_user.id
    with db_lock:
        with sqlite3.connect(DB_NAME, timeout=10) as conn:
            cur = conn.execute("SELECT orders_taken, total_commission, rating FROM masters WHERE user_id = ?", (user_id,))
            my_data = cur.fetchone()
            cur = conn.execute("SELECT user_id FROM masters WHERE orders_taken > 0 ORDER BY rating DESC")
            all_masters = [r[0] for r in cur.fetchall()]

    if not my_data or my_data[0] == 0:
        await message.answer("📊 **У вас пока нет выполненных заказов.**", reply_markup=get_menu_keyboard(user_id))
        return

    pos = all_masters.index(user_id) + 1 if user_id in all_masters else "Топ-0"
    text = f"📊 **ВАШ ПРОФИЛЬ:**\n\n📦 Заказов сдано: `{my_data[0]}`\n💰 Комиссии оплачено: `{my_data[1]:.0f}` сом\n⭐ Текущий Рейтинг: `{my_data[2]:.2f}`\n🏆 Место в системе: `{pos}`"
    await message.answer(text, parse_mode="Markdown", reply_markup=get_menu_keyboard(user_id))

@dp.message(F.text == "🏆 Топ-10 Мастеров")
async def handle_top_request(message: types.Message):
    with db_lock:
        with sqlite3.connect(DB_NAME, timeout=10) as conn:
            cur = conn.execute("SELECT username, full_name, rating, total_commission FROM masters WHERE orders_taken > 0 ORDER BY rating DESC LIMIT 10")
            top = cur.fetchall()
            
    if not top:
        await message.answer("📊 В системе пока нет оценок.")
        return
        
    text = "🏆 **ТОП-10 КОНТРАКТОРОВ:**\n\n"
    for i, (username, full_name, rating, total_comm) in enumerate(top, 1):
        name = username if username else full_name
        text += f"{'🥇' if i==1 else '🥈' if i==2 else '🥉' if i==3 else f'{i}.'} *{name}*\n   ⭐ Рейтинг: `{rating:.2f}` | 💰 Всего принес: `{total_comm:.0f}` сом\n\n"
    await message.answer(text, parse_mode="Markdown", reply_markup=get_menu_keyboard(message.from_user.id))

# ==================== SMART ADMIN FEATURES ====================

@dp.message(F.text == "➕ Начислить Рейтинг")
async def admin_choose_master(message: types.Message):
    if message.from_user.id not in ADMIN_IDS: return
    
    stats = get_all_masters()
    if not stats:
        await message.answer("📊 В базе данных пока нет зарегистрированных мастеров.")
        return
        
    # Формируем inline-кнопки прямо под сообщением для выбора мастера в один клик
    inline_buttons = []
    for uid, uname, fname, _, _, rat in stats:
        display_name = f"@{uname}" if uname else fname
        btn_text = f"👤 {display_name} (⭐ {rat:.2f})"
        inline_buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"adm_pay_{uid}")])
        
    keyboard = InlineKeyboardMarkup(inline_keyboard=inline_buttons)
    await message.answer("👇 **Выберите мастера из списка ниже для начисления комиссии:**", reply_markup=keyboard, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("adm_pay_"))
async def admin_process_selection(callback: types.CallbackQuery, state: FSMContext):
    if callback.from_user.id not in ADMIN_IDS: return
    
    target_master_id = int(callback.data.split("_")[2])
    
    with db_lock:
        with sqlite3.connect(DB_NAME, timeout=10) as conn:
            cur = conn.execute("SELECT username, full_name FROM masters WHERE user_id = ?", (target_master_id,))
            res = cur.fetchone()
            
    if not res:
        await callback.answer("❌ Мастер не найден.")
        return
        
    master_name = f"@{res[0]}" if res[0] else res[1]
    await state.update_data(target_id=target_master_id)
    
    await callback.message.edit_text(f"💰 **Мастер выбран:** {master_name}\n\n✍️ *Введите сумму принятой комиссии (в сомах):*")
    await state.set_state(AdminStates.waiting_for_commission)
    await callback.answer()

@dp.message(AdminStates.waiting_for_commission)
async def admin_save_commission(message: types.Message, state: FSMContext):
    if message.from_user.id not in ADMIN_IDS: return
    
    raw_val = message.text.strip().replace(" ", "").replace(",", ".")
    try:
        commission = float(raw_val)
        if commission <= 0: raise ValueError
        
        data = await state.get_data()
        target_id = data["target_id"]
        
        add_commission_to_master(target_id, commission)
        
        await message.answer(f"✅ **Успешно!** Начислено `{commission:.0f}` сом. Рейтинг обновлен.", reply_markup=get_menu_keyboard(message.from_user.id), parse_mode="Markdown")
        await state.clear()
    except:
        await message.answer("❌ Ошибка формата! Введите корректное положительное число.")

@dp.message(F.text == "📊 Общая Статистика")
async def admin_view_full_analytics(message: types.Message):
    if message.from_user.id not in ADMIN_IDS: return
    
    stats = get_all_masters()
    if not stats:
        await message.answer("📈 Статистика пуста.")
        return
        
    text = "📊 **ОТЧЕТ ПО ВЫРУЧКЕ И МАСТЕРАМ:**\n" + "─"*25 + "\n\n"
    grand_total_commission = 0
    
    for uid, uname, fname, ords, comm, rat in stats:
        display_name = f"@{uname}" if uname else fname
        grand_total_commission += comm
        text += (
            f"👤 **{display_name}**\n"
            f"   ├ 🆔 ID: `{uid}`\n"
            f"   ├ ⭐ Рейтинг: `{rat:.2f}`\n"
            f"   ├ 📦 Выполнено заказов: `{ords}`\n"
            f"   └ 💰 Принес комиссии: `{comm:.0f}` сом\n\n"
        )
        
    text += "─"*25 + f"\n💵 **ИТОГО СОБРАНО КОМИССИИ:** `{grand_total_commission:.0f}` сом"
    await message.answer(text, parse_mode="Markdown", reply_markup=get_menu_keyboard(message.from_user.id))

# ==================== ENGINE START ====================

async def main():
    init_db()
    await bot.set_my_commands([BotCommand(command="start", description="Перезапустить меню")])
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())