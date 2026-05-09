import asyncio
import logging
import os
import uuid
from datetime import datetime, timedelta
from typing import Dict, Optional
from dotenv import load_dotenv
load_dotenv()
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery, LabeledPrice, PreCheckoutQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from supabase import create_client, Client
import aiohttp

# ---------- Конфигурация (переменные окружения) ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
ADMIN_IDS = list(map(int, os.getenv("ADMINS", "").split(","))) if os.getenv("ADMINS") else []

# Цены
SUBSCRIPTION_PRICE_STARS = 250   # 250 Stars = 250 руб (на руки ~100 руб)
CONSULT_PRICE_STARS = 500        # 500 Stars = 500 руб

# ---------- Инициализация бота и БД ----------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# Временное хранилище для оплат через KUPIKOD (подписка и консультации)
pending_kupikod: Dict[str, dict] = {}  # {payment_id: {"user_id": int, "type": "subscription"|"consult"}}

# ---------- FSM состояния ----------
class Registration(StatesGroup):
    waiting_for_pet_type = State()
    waiting_for_pet_age = State()

class ReminderCreation(StatesGroup):
    waiting_for_title = State()
    waiting_for_datetime = State()

class ConsultStates(StatesGroup):
    choosing_doctor = State()
    waiting_for_payment = State()       # ожидание оплаты
    waiting_for_question = State()
    waiting_for_photos = State()

# ---------- Вспомогательные функции БД ----------
def get_user(user_id: int) -> Optional[dict]:
    res = supabase.table("users").select("*").eq("user_id", user_id).execute()
    return res.data[0] if res.data else None

def create_user(user_id: int, username: str = None, referrer_id: int = None) -> dict:
    data = {
        "user_id": user_id,
        "username": username,
        "referrer_id": referrer_id,
        "subscription_end": None,
        "is_active": True,
        "pet_type": None,
        "pet_age": None,
    }
    supabase.table("users").insert(data).execute()
    supabase.table("questions_quota").insert({"user_id": user_id, "free_questions_used": 0}).execute()
    return data

def is_subscription_active(user_id: int) -> bool:
    user = get_user(user_id)
    if not user or not user.get("subscription_end"):
        return False
    sub_end = datetime.fromisoformat(user["subscription_end"].replace('Z', '+00:00'))
    return sub_end > datetime.now(sub_end.tzinfo)

def get_free_questions_used(user_id: int) -> int:
    res = supabase.table("questions_quota").select("free_questions_used").eq("user_id", user_id).execute()
    if not res.data:
        supabase.table("questions_quota").insert({"user_id": user_id, "free_questions_used": 0}).execute()
        return 0
    return res.data[0]["free_questions_used"]

def increment_free_questions(user_id: int):
    used = get_free_questions_used(user_id)
    supabase.table("questions_quota").update({"free_questions_used": used + 1}).eq("user_id", user_id).execute()

def activate_subscription(user_id: int, days: int = 30):
    new_end = datetime.now() + timedelta(days=days)
    supabase.table("users").update({"subscription_end": new_end.isoformat()}).eq("user_id", user_id).execute()
    # Реферальная логика
    user = get_user(user_id)
    if user and user.get("referrer_id"):
        referrer_id = user["referrer_id"]
        supabase.table("users").update({"referrer_activated": True}).eq("user_id", user_id).execute()
        activated = supabase.table("users").select("user_id").eq("referrer_id", referrer_id).eq("referrer_activated", True).execute()
        if len(activated.data) >= 3:
            referrer_user = get_user(referrer_id)
            if referrer_user and referrer_user.get("subscription_end"):
                current_end = datetime.fromisoformat(referrer_user["subscription_end"].replace('Z', '+00:00'))
                new_end_referrer = max(current_end, datetime.now(current_end.tzinfo)) + timedelta(days=90)
            else:
                new_end_referrer = datetime.now() + timedelta(days=90)
            supabase.table("users").update({"subscription_end": new_end_referrer.isoformat()}).eq("user_id", referrer_id).execute()

def create_consult_request(user_id: int, doctor_id: int, question: str, photos: list, payment_method: str) -> int:
    res = supabase.table("consult_requests").insert({
        "user_id": user_id,
        "doctor_id": doctor_id,
        "question": question,
        "photos": photos,
        "status": "paid",          # сразу paid, так как оплата прошла
        "payment_method": payment_method
    }).execute()
    return res.data[0]["id"]

# ---------- Функция вызова ИИ ----------
async def ask_ai(question: str, user_id: int, pet_info: str = "") -> str:
    system_prompt = (
        "Ты — ветеринарный помощник с уклоном на доказательную медицину (evidence-based). "
        "Запрещено: назначать дозировки рецептурных препаратов, игнорировать красные флаги (кровь, судороги, анурия, отказ от воды >12ч). "
        "При красных флагах — настоятельно рекомендовать срочный визит к ветеринару. "
        "Отвечай на русском, четко. Всегда заканчивай дисклеймером: 'Бот не заменяет врача. При ухудшении обратитесь к ветеринару очно.'"
    )
    if pet_info:
        system_prompt += f"\nИнформация о питомце пользователя: {pet_info}"
    async with aiohttp.ClientSession() as session:
        headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
        payload = {
            "model": "openrouter/free",
            "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": question}],
            "max_tokens": 800,
        }
        try:
            async with session.post("https://openrouter.ai/api/v1/chat/completions", json=payload, headers=headers) as resp:
                data = await resp.json()
                answer = data["choices"][0]["message"]["content"] if "choices" in data else f"Ошибка: {data}"
        except Exception as e:
            answer = f"Не удалось получить ответ. Ошибка: {e}"
    danger_keywords = ["кровь", "судороги", "не дышит", "отказ от воды", "рвота более", "анурия"]
    if any(kw in answer.lower() for kw in danger_keywords):
        answer += "\n\n⚠️ **ВНИМАНИЕ!** Обнаружены симптомы, требующие срочного осмотра ветеринаром. Не откладывайте визит!"
    return answer

# ---------- Клавиатуры ----------
def main_menu_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="❓ Задать вопрос", callback_data="ask_question")
    builder.button(text="⭐️ Подписка", callback_data="subscription_menu")
    builder.button(text="🔔 Напоминания", callback_data="reminders_menu")
    builder.button(text="👥 Реферальная программа", callback_data="referral_info")
    builder.button(text="🩺 Консультация с врачом", callback_data="doctor_consult")
    builder.adjust(2)
    return builder.as_markup()

def subscription_methods_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="⭐️ Оплатить Stars", callback_data="pay_stars")
    builder.button(text="📱 Оплатить с баланса телефона", callback_data="pay_phone_subscription")
    builder.button(text="◀️ Назад", callback_data="back_to_main")
    builder.adjust(1)
    return builder.as_markup()

def consult_payment_methods_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="⭐️ Оплатить Stars (500)", callback_data="consult_pay_stars")
    builder.button(text="📱 Оплатить с баланса телефона", callback_data="consult_pay_phone")
    builder.button(text="◀️ Назад", callback_data="back_to_main")
    builder.adjust(1)
    return builder.as_markup()

def reminders_menu_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Создать напоминание", callback_data="create_reminder")
    builder.button(text="📋 Мои напоминания", callback_data="list_reminders")
    builder.button(text="◀️ Назад", callback_data="back_to_main")
    builder.adjust(1)
    return builder.as_markup()

# ---------- Обработчики команд ----------
@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    username = message.from_user.username
    args = message.text.split()
    referrer_id = int(args[1]) if len(args) > 1 and args[1].isdigit() else None

    user = get_user(user_id)
    if not user:
        create_user(user_id, username, referrer_id)
        await message.answer(
            "🐾 Добро пожаловать в ветеринарного AI-помощника!\nДавайте заполним анкету питомца.\nКакой у вас вид питомца? (собака, кошка и т.д.)"
        )
        await state.set_state(Registration.waiting_for_pet_type)
    else:
        await message.answer("С возвращением! Используйте меню.", reply_markup=main_menu_keyboard())

@dp.message(Registration.waiting_for_pet_type)
async def process_pet_type(message: Message, state: FSMContext):
    await state.update_data(pet_type=message.text)
    await message.answer("Сколько лет вашему питомцу? (число или 'неизвестно')")
    await state.set_state(Registration.waiting_for_pet_age)

@dp.message(Registration.waiting_for_pet_age)
async def process_pet_age(message: Message, state: FSMContext):
    data = await state.get_data()
    supabase.table("users").update({
        "pet_type": data['pet_type'],
        "pet_age": message.text
    }).eq("user_id", message.from_user.id).execute()
    await message.answer("✅ Анкета сохранена! У вас есть 3 бесплатных вопроса.", reply_markup=main_menu_keyboard())
    await state.clear()

# ---------- Вопросы к ИИ ----------
@dp.callback_query(F.data == "ask_question")
async def ask_question_callback(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Напишите ваш вопрос о здоровье питомца (max 500 символов):")
    await state.set_state("waiting_for_question")
    await callback.answer()

@dp.message(StateFilter("waiting_for_question"))
async def handle_question(message: Message, state: FSMContext):
    user_id = message.from_user.id
    question = message.text.strip()
    if len(question) > 500:
        await message.answer("Слишком длинный вопрос, сократите.")
        return

    if not is_subscription_active(user_id):
        used = get_free_questions_used(user_id)
        if used >= 3:
            await message.answer("Закончились бесплатные вопросы. Оформите подписку.", reply_markup=subscription_methods_keyboard())
            await state.clear()
            return
        increment_free_questions(user_id)

    user_data = get_user(user_id)
    pet_info = f"Вид: {user_data.get('pet_type')}, Возраст: {user_data.get('pet_age')}" if user_data.get('pet_type') else ""
    await message.answer("🤔 Думаю...")
    answer = await ask_ai(question, user_id, pet_info)
    await message.answer(answer, parse_mode="Markdown")
    supabase.table("ai_requests").insert({"user_id": user_id, "question": question, "response": answer}).execute()
    await state.clear()

# ---------- Подписка (Stars) ----------
@dp.callback_query(F.data == "pay_stars")
async def pay_stars_callback(callback: CallbackQuery):
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="🐾 Месячная подписка",
        description="Безлимитные вопросы + напоминания",
        payload="month_subscription_stars",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="1 месяц", amount=SUBSCRIPTION_PRICE_STARS)],
        start_parameter="sub_stars",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"⭐️ Оплатить {SUBSCRIPTION_PRICE_STARS} Stars", pay=True)]
        ])
    )
    await callback.answer()

@dp.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery):
    await pre_checkout_query.answer(ok=True)

@dp.message(F.successful_payment)
async def process_successful_payment(message: Message):
    user_id = message.from_user.id
    payload = message.successful_payment.invoice_payload
    if payload == "month_subscription_stars":
        activate_subscription(user_id, days=30)
        await message.answer("✅ Подписка активирована на 30 дней!", reply_markup=main_menu_keyboard())
    elif payload.startswith("consult_"):
        # оплата консультации
        consult_id = int(payload.split("_")[1])
        # обновим статус в БД
        supabase.table("consult_requests").update({"status": "paid"}).eq("id", consult_id).execute()
        # продолжим сбор вопроса (состояние уже было)
        await message.answer("✅ Оплата получена! Теперь опишите проблему и пришлите фото.")
        # user уже в состоянии waiting_for_question? выставим состояние
        await state.set_state(ConsultStates.waiting_for_question)
        # нужно сохранить consult_id в данные состояния
        await state.update_data(consult_id=consult_id)

# ---------- Подписка (баланс телефона) ----------
@dp.callback_query(F.data == "pay_phone_subscription")
async def pay_phone_subscription(callback: CallbackQuery):
    user_id = callback.from_user.id
    payment_id = str(uuid.uuid4())
    pending_kupikod[payment_id] = {"user_id": user_id, "type": "subscription"}
    payment_url = f"https://kupikod.com/pay?amount=250&order_id={payment_id}&callback_url=https://yourbot.com/callback"  # замените
    text = (
        "📱 **Оплата подписки с баланса телефона**\n\n"
        "1. Перейдите по ссылке.\n2. Оплатите 250 руб.\n3. Нажмите 'Я оплатил'."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Перейти к оплате", url=payment_url)],
        [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"verify_kupikod_{payment_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="subscription_menu")]
    ])
    await callback.message.answer(text, parse_mode="Markdown", reply_markup=kb)
    await callback.answer()

# ---------- Консультация с врачом (с оплатой) ----------
@dp.callback_query(F.data == "doctor_consult")
async def doctor_consult_menu(callback: CallbackQuery, state: FSMContext):
    # Проверяем, есть ли врачи
    doctors = supabase.table("doctors").select("id, name, specialization").eq("is_available", True).execute()
    if not doctors.data:
        await callback.message.answer("Сейчас нет доступных ветеринаров. Попробуйте позже.")
        return
    if len(doctors.data) == 1:
        doctor_id = doctors.data[0]["id"]
        await state.update_data(selected_doctor_id=doctor_id)
        # Запрашиваем способ оплаты
        await callback.message.answer(
            "🩺 **Консультация с ветеринаром**\nСтоимость: **500 руб**.\nВыберите способ оплаты:",
            reply_markup=consult_payment_methods_keyboard()
        )
        await state.set_state(ConsultStates.choosing_doctor)
    else:
        # выбор врача (для масштабирования)
        builder = InlineKeyboardBuilder()
        for doc in doctors.data:
            builder.button(text=f"{doc['name']} ({doc['specialization']})", callback_data=f"select_doctor_{doc['id']}")
        builder.button(text="◀️ Назад", callback_data="back_to_main")
        await callback.message.answer("Выберите ветеринара:", reply_markup=builder.as_markup())
    await callback.answer()

@dp.callback_query(F.data.startswith("select_doctor_"))
async def select_doctor(callback: CallbackQuery, state: FSMContext):
    doctor_id = int(callback.data.split("_")[2])
    await state.update_data(selected_doctor_id=doctor_id)
    await callback.message.answer("Стоимость консультации 500 руб. Выберите способ оплаты:", reply_markup=consult_payment_methods_keyboard())
    await state.set_state(ConsultStates.choosing_doctor)
    await callback.answer()

# Оплата консультации Stars
@dp.callback_query(F.data == "consult_pay_stars")
async def consult_pay_stars(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    doctor_id = data.get("selected_doctor_id")
    if not doctor_id:
        await callback.message.answer("Ошибка: выберите врача заново.")
        return
    # Создаем заявку со статусом "waiting_payment"
    res = supabase.table("consult_requests").insert({
        "user_id": callback.from_user.id,
        "doctor_id": doctor_id,
        "status": "waiting_payment",
        "payment_method": "stars"
    }).execute()
    consult_id = res.data[0]["id"]
    await state.update_data(consult_id=consult_id)
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="Консультация ветеринара",
        description="Ответ врача в течение 4 часов",
        payload=f"consult_{consult_id}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="Консультация", amount=CONSULT_PRICE_STARS)],
        start_parameter="consult",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"⭐️ Оплатить {CONSULT_PRICE_STARS} Stars", pay=True)]
        ])
    )
    await callback.answer()

# Оплата консультации через телефон
@dp.callback_query(F.data == "consult_pay_phone")
async def consult_pay_phone(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    doctor_id = data.get("selected_doctor_id")
    if not doctor_id:
        await callback.message.answer("Ошибка: выберите врача заново.")
        return
    # Создаем заявку со статусом waiting_payment
    res = supabase.table("consult_requests").insert({
        "user_id": callback.from_user.id,
        "doctor_id": doctor_id,
        "status": "waiting_payment",
        "payment_method": "phone"
    }).execute()
    consult_id = res.data[0]["id"]
    await state.update_data(consult_id=consult_id)

    payment_id = str(uuid.uuid4())
    pending_kupikod[payment_id] = {"user_id": callback.from_user.id, "type": "consult", "consult_id": consult_id}
    payment_url = f"https://kupikod.com/pay?amount=500&order_id={payment_id}&callback_url=https://yourbot.com/callback"  # замените
    text = (
        "📱 **Оплата консультации с баланса телефона**\n\n"
        "1. Перейдите по ссылке.\n2. Оплатите 500 руб.\n3. Нажмите 'Я оплатил'."
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Перейти к оплате", url=payment_url)],
        [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"verify_kupikod_{payment_id}")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="doctor_consult")]
    ])
    await callback.message.answer(text, parse_mode="Markdown", reply_markup=kb)
    await callback.answer()

# Обработчик подтверждения оплаты через KUPIKOD (общий)
@dp.callback_query(F.data.startswith("verify_kupikod_"))
async def verify_kupikod(callback: CallbackQuery, state: FSMContext):
    payment_id = callback.data.split("_")[2]
    payment_data = pending_kupikod.get(payment_id)
    if not payment_data or payment_data["user_id"] != callback.from_user.id:
        await callback.answer("Неверный запрос. Начните оплату заново.", show_alert=True)
        return

    if payment_data["type"] == "subscription":
        activate_subscription(callback.from_user.id, days=30)
        await callback.message.answer("✅ Подписка активирована!", reply_markup=main_menu_keyboard())
        del pending_kupikod[payment_id]
    elif payment_data["type"] == "consult":
        consult_id = payment_data["consult_id"]
        supabase.table("consult_requests").update({"status": "paid"}).eq("id", consult_id).execute()
        await state.update_data(consult_id=consult_id)
        await callback.message.answer("✅ Оплата получена! Теперь опишите проблему и пришлите фото (до 3 фото). Для завершения отправьте /finish_consult")
        await state.set_state(ConsultStates.waiting_for_question)
        del pending_kupikod[payment_id]
    await callback.answer()

# Сбор вопроса и фото для консультации
@dp.message(ConsultStates.waiting_for_question, F.text)
async def collect_consult_question(message: Message, state: FSMContext):
    if message.text.startswith('/'):
        return  # команды пропускаем
    await state.update_data(consult_question=message.text)
    await message.answer("Вопрос сохранен. Теперь можете отправить фото (1-3). Или сразу /finish_consult")
    await state.set_state(ConsultStates.waiting_for_photos)

@dp.message(ConsultStates.waiting_for_photos, F.photo)
async def collect_consult_photos(message: Message, state: FSMContext):
    data = await state.get_data()
    photos = data.get("photos", [])
    file_id = message.photo[-1].file_id
    photos.append(file_id)
    if len(photos) >= 3:
        await state.update_data(photos=photos)
        await message.answer("Максимум 3 фото. Завершите /finish_consult")
        await state.set_state(ConsultStates.waiting_for_question)  # блокируем дальнейшие фото
    else:
        await state.update_data(photos=photos)
        await message.answer(f"Фото добавлено ({len(photos)}/3). Можно добавить еще или /finish_consult")

@dp.message(Command("finish_consult"))
async def finish_consult(message: Message, state: FSMContext):
    data = await state.get_data()
    consult_id = data.get("consult_id")
    question = data.get("consult_question")
    photos = data.get("photos", [])
    if not question:
        await message.answer("Вы не ввели вопрос. Отмените /cancel_consult и начните заново.")
        return
    # Обновляем заявку с вопросом и фото
    supabase.table("consult_requests").update({
        "question": question,
        "photos": photos
    }).eq("id", consult_id).execute()
    # Получаем данные о заявке и враче
    req = supabase.table("consult_requests").select("doctor_id, user_id").eq("id", consult_id).execute()
    if not req.data:
        await message.answer("Ошибка: заявка не найдена.")
        return
    doctor_id = req.data[0]["doctor_id"]
    doctor = supabase.table("doctors").select("tg_user_id, name").eq("id", doctor_id).execute()
    doctor_tg_id = doctor.data[0]["tg_user_id"]
    user = message.from_user
    text = f"🆕 **Консультация #{consult_id}**\nПользователь: {user.full_name} (@{user.username})\nВопрос: {question}"
    # Отправляем врачу
    if photos:
        await bot.send_photo(doctor_tg_id, photo=photos[0], caption=text, parse_mode="Markdown")
        if len(photos) > 1:
            media = [types.InputMediaPhoto(media=photo) for photo in photos[1:]]
            await bot.send_media_group(doctor_tg_id, media=media)
    else:
        await bot.send_message(doctor_tg_id, text, parse_mode="Markdown")
    await message.answer("✅ Ваш запрос отправлен врачу. Ответ придет сюда в течение 4 часов.")
    await state.clear()

@dp.message(Command("cancel_consult"))
async def cancel_consult(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Консультация отменена.", reply_markup=main_menu_keyboard())

# Команда для врача (ответ)
@dp.message(Command("answer_consult"))
async def answer_consult(message: Message):
    doctor = supabase.table("doctors").select("id").eq("tg_user_id", message.from_user.id).execute()
    if not doctor.data:
        await message.answer("У вас нет прав.")
        return
    parts = message.text.split(maxsplit=2)
    if len(parts) < 3:
        await message.answer("Формат: /answer_consult [id_заявки] [ответ]")
        return
    try:
        consult_id = int(parts[1])
        answer_text = parts[2]
    except:
        await message.answer("Неверный формат ID.")
        return
    # Обновляем заявку
    res = supabase.table("consult_requests").update({
        "status": "answered",
        "doctor_answer": answer_text,
        "answered_at": datetime.now().isoformat()
    }).eq("id", consult_id).eq("doctor_id", doctor.data[0]["id"]).execute()
    if not res.data:
        await message.answer("Заявка не найдена или доступ запрещен.")
        return
    # Отправляем пользователю
    user_id = res.data[0]["user_id"]
    await bot.send_message(
        user_id,
        f"🩺 **Ответ врача**\n\n{answer_text}\n\n⚠️ Консультация не заменяет очный осмотр."
    )
    await message.answer(f"✅ Ответ отправлен пользователю (заявка #{consult_id})")

# ---------- Реферальная программа ----------
@dp.callback_query(F.data == "referral_info")
async def referral_info(callback: CallbackQuery):
    bot_username = (await bot.get_me()).username
    link = f"https://t.me/{bot_username}?start={callback.from_user.id}"
    await callback.message.answer(
        f"👥 **Реферальная программа**\nПригласите 3 друзей — получите 3 месяца бесплатно.\nВаша ссылка: `{link}`",
        parse_mode="Markdown"
    )
    await callback.answer()

# ---------- Напоминания ----------
@dp.callback_query(F.data == "reminders_menu")
async def reminders_menu(callback: CallbackQuery):
    await callback.message.answer("Управление напоминаниями:", reply_markup=reminders_menu_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "create_reminder")
async def create_reminder_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Введите название напоминания:")
    await state.set_state(ReminderCreation.waiting_for_title)
    await callback.answer()

@dp.message(ReminderCreation.waiting_for_title)
async def reminder_title(message: Message, state: FSMContext):
    await state.update_data(title=message.text)
    await message.answer("Введите дату и время в формате ГГГГ-ММ-ДД ЧЧ:ММ (например 2025-06-15 10:00)")
    await state.set_state(ReminderCreation.waiting_for_datetime)

@dp.message(ReminderCreation.waiting_for_datetime)
async def reminder_datetime(message: Message, state: FSMContext):
    data = await state.get_data()
    title = data['title']
    try:
        remind_at = datetime.strptime(message.text, "%Y-%m-%d %H:%M")
        if remind_at < datetime.now():
            await message.answer("Дата должна быть в будущем.")
            return
        supabase.table("reminders").insert({
            "user_id": message.from_user.id,
            "title": title,
            "remind_at": remind_at.isoformat(),
            "is_completed": False
        }).execute()
        await message.answer(f"✅ Напоминание '{title}' установлено на {remind_at.strftime('%d.%m.%Y %H:%M')}")
        await state.clear()
    except ValueError:
        await message.answer("Неверный формат. Используйте ГГГГ-ММ-ДД ЧЧ:ММ")

@dp.callback_query(F.data == "list_reminders")
async def list_reminders(callback: CallbackQuery):
    res = supabase.table("reminders").select("*").eq("user_id", callback.from_user.id).eq("is_completed", False).order("remind_at").execute()
    if not res.data:
        await callback.message.answer("Нет активных напоминаний.")
    else:
        text = "📋 **Активные напоминания:**\n"
        for r in res.data:
            dt = datetime.fromisoformat(r['remind_at'].replace('Z', '+00:00'))
            text += f"• {r['title']} — {dt.strftime('%d.%m.%Y %H:%M')}\n"
        await callback.message.answer(text, parse_mode="Markdown")
    await callback.answer()

# ---------- Кнопки навигации ----------
@dp.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery):
    await callback.message.edit_text("Главное меню:", reply_markup=main_menu_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "subscription_menu")
async def subscription_menu(callback: CallbackQuery):
    await callback.message.edit_text("Выберите способ оплаты подписки (250 руб/мес):", reply_markup=subscription_methods_keyboard())
    await callback.answer()

# ---------- Фоновая задача напоминаний ----------
async def reminder_scheduler():
    while True:
        now = datetime.now()
        res = supabase.table("reminders").select("*").eq("is_completed", False).execute()
        for rem in res.data:
            remind_at = datetime.fromisoformat(rem['remind_at'].replace('Z', '+00:00'))
            if remind_at <= now:
                try:
                    await bot.send_message(rem['user_id'], f"🔔 **Напоминание:** {rem['title']}")
                    supabase.table("reminders").update({"is_completed": True}).eq("id", rem['id']).execute()
                except Exception as e:
                    logging.error(f"Reminder error: {e}")
        await asyncio.sleep(60)

# ---------- Запуск ----------
async def main():
    asyncio.create_task(reminder_scheduler())
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

# Импортируем библиотеку для создания веб-сервера
from aiohttp import web

async def health_check(request):
    return web.Response(text="Бот работает!")

async def start_web_server():
    app = web.Application()
    app.router.add_get('/health', health_check) # Создаем endpoint для проверки
    runner = web.AppRunner(app)
    await runner.setup()
    # Порт берем из переменной окружения, который мы добавили на Koyeb
    port = int(os.getenv('PORT', 8080))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"Веб-сервер для health-чеков запущен на порту {port}")

# Изменим функцию main, чтобы она запускала оба сервера
async def main():
    asyncio.create_task(start_web_server())   # Фоновый запуск веб-сервера
    asyncio.create_task(reminder_scheduler()) # Ваш планировщик напоминаний
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
