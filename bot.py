import asyncio
import logging
import os
import re
import uuid
from datetime import datetime, timedelta, date
from typing import Dict, Optional
from yookassa import Configuration, Payment

from dotenv import load_dotenv
load_dotenv()

from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from supabase import create_client, Client
import aiohttp

# ---------- Конфигурация ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")
ADMIN_IDS = list(map(int, os.getenv("ADMINS", "").split(","))) if os.getenv("ADMINS") else []

YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY")
if YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY:
    Configuration.account_id = YOOKASSA_SHOP_ID
    Configuration.secret_key = YOOKASSA_SECRET_KEY

# ---------- Инициализация ----------
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

pending_payments: Dict[str, dict] = {}

# ---------- FSM состояния ----------
class Registration(StatesGroup):
    waiting_for_pet_type = State()
    waiting_for_pet_age = State()

class ReminderCreation(StatesGroup):
    waiting_for_title = State()
    waiting_for_datetime = State()

class ConsultStates(StatesGroup):
    choosing_doctor = State()
    waiting_for_question = State()
    waiting_for_photos = State()

class AskState(StatesGroup):
    waiting_for_question = State()

# ---------- Работа с БД ----------
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


def check_and_add_monthly_free_consult(user_id: int):
    """Проверяет, нужно ли начислить бесплатную консультацию за новый месяц подписки"""
    user = get_user(user_id)
    if not user or not user.get("subscription_end"):
        return False

    subscription_end = datetime.fromisoformat(user["subscription_end"].replace('Z', '+00:00'))
    if subscription_end < datetime.now():
        return False  # Подписка истекла

    last_date_str = user.get("last_free_consult_date")
    today = date.today()

    # Преобразуем строку из БД в объект date (если есть)
    last_date = None
    if last_date_str:
        try:
            # Если строка в формате ISO (YYYY-MM-DD)
            last_date = datetime.fromisoformat(last_date_str).date()
        except:
            # Если другой формат или ошибка
            last_date = None

    # Если никогда не начисляли ИЛИ прошло 25+ дней
    if not last_date or (today - last_date).days >= 25:
        current = user.get("free_consultations", 0)
        supabase.table("users").update({
            "free_consultations": current + 1,
            "last_free_consult_date": today.isoformat()
        }).eq("user_id", user_id).execute()
        print(f"✅ Начислена бесплатная консультация пользователю {user_id} (всего: {current + 1})")
        return True
    return False

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

def get_remaining_free_questions(user_id: int) -> int:
    """Возвращает количество оставшихся бесплатных вопросов"""
    used = get_free_questions_used(user_id)
    remaining = max(0, 3 - used)
    return remaining

def increment_free_questions(user_id: int):
    used = get_free_questions_used(user_id)
    supabase.table("questions_quota").update({"free_questions_used": used + 1}).eq("user_id", user_id).execute()


def get_referral_stats(user_id: int) -> dict:
    """Возвращает статистику рефералов пользователя"""
    # Количество активированных рефералов (тех, кто оформил подписку)
    activated = supabase.table("users").select("user_id").eq("referrer_id", user_id).eq("referrer_activated",
                                                                                        True).execute()
    activated_count = len(activated.data)

    # Количество всех приглашённых (даже без подписки)
    total = supabase.table("users").select("user_id").eq("referrer_id", user_id).execute()
    total_count = len(total.data)

    return {
        "total": total_count,
        "activated": activated_count,
        "needed": max(0, 3 - activated_count)  # сколько осталось до бонуса
    }


def activate_subscription(user_id: int, days: int = 30):
    new_end = datetime.now() + timedelta(days=days)
    supabase.table("users").update({"subscription_end": new_end.isoformat()}).eq("user_id", user_id).execute()

    # Даём 1 бесплатную консультацию при активации подписки
    user = get_user(user_id)
    current_free = user.get("free_consultations", 0)
    supabase.table("users").update({"free_consultations": current_free + 1}).eq("user_id", user_id).execute()

    # Реферальная логика (оставляем как было)
    if user and user.get("referrer_id"):
        referrer_id = user["referrer_id"]
        supabase.table("users").update({"referrer_activated": True}).eq("user_id", user_id).execute()
        activated = supabase.table("users").select("user_id").eq("referrer_id", referrer_id).eq("referrer_activated",
                                                                                                True).execute()
        if len(activated.data) >= 3:
            referrer_user = get_user(referrer_id)
            if referrer_user and referrer_user.get("subscription_end"):
                current_end = datetime.fromisoformat(referrer_user["subscription_end"].replace('Z', '+00:00'))
                new_end_referrer = max(current_end, datetime.now(current_end.tzinfo)) + timedelta(days=90)
            else:
                new_end_referrer = datetime.now() + timedelta(days=90)
            supabase.table("users").update({"subscription_end": new_end_referrer.isoformat()}).eq("user_id", referrer_id).execute()

def create_yookassa_payment(user_id: int, amount: float, description: str, payment_type: str = "subscription") -> str:
    """Создаёт платёж в ЮKassa и возвращает ссылку для оплаты"""
    payment = Payment.create({
        "amount": {
            "value": str(amount),
            "currency": "RUB"
        },
        "confirmation": {
            "type": "redirect",
            "return_url": "https://t.me/Sovetaibot"  # замените на ссылку вашего бота
        },
        "capture": True,
        "description": description,
        "metadata": {
            "user_id": str(user_id),
            "payment_type": payment_type
        }
    })
    return payment.confirmation.confirmation_url, payment.id

def clean_answer(text: str) -> str:
    """Постобработка ответа ИИ: удаление мусора, нормализация пробелов."""
    text = re.sub(r'```[\s\S]*?```', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = text.strip()
    if len(text) < 10:
        text = "Не удалось сформулировать ответ. Пожалуйста, перефразируйте вопрос."
    return text

# ---------- Функция запроса к ИИ (улучшенная) ----------
async def ask_ai(question: str, user_id: int, pet_info: str = "") -> str:
    system_prompt = (
        "Ты — помощник владельца домашних животных. Твоя задача — предоставлять полезные и безопасные рекомендации, "
        "НО НЕ ЗАМЕНЯТЬ СОБОЙ ВЕТЕРИНАРНОГО ВРАЧА. Все твои ответы должны основываться на принципах доказательной "
        "медицины и авторитетных источников, таких как WSAVA, AVMA, AAHA и PubMed. "
        "Следуй строго этой структуре:\n"
        "1. Оценка срочности: если есть тревожные симптомы (сильная вялость, отказ от воды/еды >12ч, судороги, "
        "затруднённое дыхание, многократная рвота, потеря координации, кровь) — единственный ответ: "
        "'Ситуация выглядит критической. Немедленно покажите животное ветеринару. Я не могу давать другие советы.' "
        "2. Иначе: вежливо поприветствуй, уточни вид/возраст (если не указаны), дай ответ на основе доказательной медицины. "
        "Объясни, что владелец может сделать дома, а что — только врач. Запрещено давать конкретные дозировки препаратов. "
        "3. Завершай напоминанием: 'Бот не заменяет врача. При ухудшении обратитесь к ветеринару очно.'\n"
        "Пиши только на русском языке, грамотно, без иностранных слов (кроме научных терминов с пояснением). "
        "Не используй эмодзи, сленг, повторы. Отвечай кратко, по делу."
    )
    if pet_info:
        system_prompt += f"\nИнформация о питомце: {pet_info}"

    # Список моделей в порядке приоритета (от лучших к резервным)
    models_priority = [
        "nvidia/nemotron-3-super-120b-a12b:free",  # ✅ работает (ваш лог)
        "openrouter/free",  # автоматический выбор (финальный резерв)
    ]

    last_error = None

    for model in models_priority:
        try:
            async with aiohttp.ClientSession() as session:
                headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
                payload = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": question}
                    ],
                    "max_tokens": 1000,
                    "temperature": 0.3,
                    "frequency_penalty": 0.5,
                }

                async with session.post(
                        "https://openrouter.ai/api/v1/chat/completions",
                        json=payload,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=55)
                ) as resp:
                    data = await resp.json()

                    # Если успешный ответ (200)
                    if resp.status == 200 and "choices" in data and data["choices"]:
                        answer = data["choices"][0]["message"]["content"]
                        answer = clean_answer(answer)
                        # Логируем, какая модель сработала (для отладки)
                        print(f"✅ Использована модель: {model}")
                        return answer

                    # Если модель вернула ошибку, пробуем следующую
                    error_msg = data.get('error', {}).get('message', str(data))
                    print(f"⚠️ Модель {model} не работает: {error_msg}")
                    last_error = error_msg
                    continue  # переходим к следующей модели

        except asyncio.TimeoutError:
            print(f"⏰ Таймаут модели {model}, пробуем следующую...")
            last_error = "Timeout"
            continue
        except Exception as e:
            print(f"❌ Ошибка модели {model}: {str(e)}")
            last_error = str(e)
            continue

    # Если все модели не сработали
    return f"❌ Извините, все нейросети временно недоступны. Последняя ошибка: {last_error}\nПожалуйста, попробуйте позже или обратитесь к врачу напрямую через /doctor_consult"
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
    builder.button(text="💳 Оплатить картой (250 ₽)", callback_data="pay_rubles")  # новая кнопка
    builder.button(text="◀️ Назад", callback_data="back_to_main")
    builder.adjust(1)
    return builder.as_markup()

def consult_payment_methods_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="💳 Оплатить 500 ₽", callback_data="consult_pay_rubles")
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

# ---------- Обработчики ----------
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
            "🐾 Добро пожаловать! Давайте заполним анкету питомца.\nКакой у вас вид питомца? (собака, кошка и т.д.)"
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

# ----- Вопросы -----
@dp.callback_query(F.data == "ask_question")
async def ask_question_callback(callback: CallbackQuery, state: FSMContext):
    await callback.message.answer("Напишите ваш вопрос о здоровье питомца (max 500 символов):")
    await state.set_state(AskState.waiting_for_question)
    await callback.answer()

@dp.message(Command("ask"))
async def cmd_ask(message: Message, state: FSMContext):
    await message.answer("Напишите ваш вопрос о здоровье питомца (max 500 символов):")
    await state.set_state(AskState.waiting_for_question)

@dp.message(AskState.waiting_for_question)
async def handle_question(message: Message, state: FSMContext):
    user_id = message.from_user.id
    question = message.text.strip()
    if len(question) > 500:
        await message.answer("Слишком длинный вопрос, сократите.")
        return

    if not is_subscription_active(user_id):
        used = get_free_questions_used(user_id)
        remaining = 3 - used
        if used >= 3:
            await message.answer(
                "❌ **У вас закончились бесплатные вопросы.**\n\n"
                "Оформите подписку за 250 ₽/мес:\n"
                "• Безлимитные вопросы к ИИ\n"
                "• 1 бесплатная консультация с врачом в месяц\n"
                "• Напоминания о вакцинациях\n\n"
                "Используйте /subscribe",
                parse_mode="Markdown",
                reply_markup=subscription_methods_keyboard()
            )
            await state.clear()
            return

        # Показываем остаток перед вопросом
        await message.answer(
            f"🤔 Думаю...\n\n💡 У вас осталось **{remaining}** бесплатных вопросов. После этого потребуется подписка.")
        increment_free_questions(user_id)
    else:
        await message.answer("🤔 Думаю... (безлимит по подписке)")

    user_data = get_user(user_id)
    pet_info = f"Вид: {user_data.get('pet_type')}, Возраст: {user_data.get('pet_age')}" if user_data.get(
        'pet_type') else ""
    answer = await ask_ai(question, user_id, pet_info)
    await message.answer(answer)
    supabase.table("ai_requests").insert({"user_id": user_id, "question": question, "response": answer}).execute()
    await state.clear()

# ----- Подписка rubles -----
@dp.callback_query(F.data == "pay_rubles")
async def pay_rubles_callback(callback: CallbackQuery):
    """Оплата подписки рублями через ЮKassa"""
    user_id = callback.from_user.id
    amount = 250.00

    payment_url, payment_id = create_yookassa_payment(
        user_id=user_id,
        amount=amount,
        description=f"Подписка для пользователя {user_id}",
        payment_type="subscription"
    )

    pending_payments[payment_id] = {"user_id": user_id, "type": "subscription"}

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить 250 ₽", url=payment_url)],
        [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"check_payment_{payment_id}")]  # ← изменил
    ])

    await callback.message.edit_text(
        "💰 **Оплата подписки**\n\n"
        "Стоимость: 250 ₽\n\n"
        "Нажмите на кнопку, чтобы оплатить картой или через СБП.\n\n"
        "После оплаты нажмите 'Я оплатил' для проверки.",
        reply_markup=kb
    )
    await callback.answer()

@dp.message(Command("subscribe"))
async def cmd_subscribe(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить 250 ₽", callback_data="pay_rubles")]
    ])
    await message.answer("Оплата подписки (250 ₽/мес):", reply_markup=kb)


# ----- Консультация с врачом -----
@dp.message(Command("doctor_consult"))
async def cmd_doctor_consult(message: Message, state: FSMContext):
    """Обработчик команды /doctor_consult из меню"""
    user_id = message.from_user.id

    # Проверяем, нужно ли начислить бесплатную консультацию
    check_and_add_monthly_free_consult(user_id)

    user = get_user(user_id)
    free_consults = user.get("free_consultations", 0)

    # Получаем список врачей
    doctors = supabase.table("doctors").select("id, name, specialization").eq("is_available", True).execute()
    if not doctors.data:
        await message.answer("Сейчас нет доступных ветеринаров. Попробуйте позже.")
        return

    # Сохраняем ID врача
    doctor_id = doctors.data[0]["id"] if len(doctors.data) == 1 else None
    await state.update_data(selected_doctor_id=doctor_id)

    # Показываем меню выбора оплаты
    if free_consults > 0:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"🎁 Бесплатная консультация (осталось: {free_consults})",
                                  callback_data="use_free_consult")],
            [InlineKeyboardButton(text="💳 Оплатить 500 ₽", callback_data="consult_pay_rubles")],
            [InlineKeyboardButton(text="◀️ Назад в меню", callback_data="back_to_main")]
        ])
        await message.answer(
            f"🩺 **Консультация с ветеринаром**\n\n"
            f"У вас есть **{free_consults}** бесплатная(ых) консультация(ий) по подписке!\n"
            f"Каждый месяц подписки даёт +1 бесплатную консультацию.\n\n"
            f"💰 Платная консультация: 500 ₽\n\n"
            f"Выберите способ оплаты:",
            reply_markup=kb,
            parse_mode="Markdown"
        )
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить 500 ₽", callback_data="consult_pay_rubles")],
            [InlineKeyboardButton(text="◀️ Назад в меню", callback_data="back_to_main")]
        ])
        await message.answer(
            "🩺 **Консультация с ветеринаром**\n\n"
            "💰 Стоимость: 500 ₽\n\n"
            "💡 **Совет:** Оформите подписку за 250 ₽/мес и получайте 1 бесплатную консультацию каждый месяц!\n\n"
            "Нажмите на кнопку для оплаты:",
            reply_markup=kb,
            parse_mode="Markdown"
        )

    await state.set_state(ConsultStates.choosing_doctor)

@dp.callback_query(F.data == "doctor_consult")
async def doctor_consult_callback(callback: CallbackQuery, state: FSMContext):
    """Обработчик кнопки консультации на экране"""
    user_id = callback.from_user.id

    # Проверяем, нужно ли начислить бесплатную консультацию
    check_and_add_monthly_free_consult(user_id)

    user = get_user(user_id)
    free_consults = user.get("free_consultations", 0)

    # Получаем список врачей
    doctors = supabase.table("doctors").select("id, name, specialization").eq("is_available", True).execute()
    if not doctors.data:
        await callback.message.answer("Сейчас нет доступных ветеринаров. Попробуйте позже.")
        await callback.answer()
        return

    # Сохраняем ID врача
    doctor_id = doctors.data[0]["id"] if len(doctors.data) == 1 else None
    await state.update_data(selected_doctor_id=doctor_id)

    # Показываем меню выбора оплаты
    if free_consults > 0:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"🎁 Бесплатная консультация (осталось: {free_consults})",
                                  callback_data="use_free_consult")],
            [InlineKeyboardButton(text="💳 Оплатить 500 ₽", callback_data="consult_pay_rubles")],
            [InlineKeyboardButton(text="◀️ Назад в меню", callback_data="back_to_main")]
        ])
        await callback.message.edit_text(
            f"🩺 **Консультация с ветеринаром**\n\n"
            f"У вас есть **{free_consults}** бесплатная(ых) консультация(ий) по подписке!\n"
            f"Каждый месяц подписки даёт +1 бесплатную консультацию.\n\n"
            f"💰 Платная консультация: 500 ₽\n\n"
            f"Выберите способ оплаты:",
            reply_markup=kb,
            parse_mode="Markdown"
        )
    else:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить 500 ₽", callback_data="consult_pay_rubles")],
            [InlineKeyboardButton(text="◀️ Назад в меню", callback_data="back_to_main")]
        ])
        await callback.message.edit_text(
            "🩺 **Консультация с ветеринаром**\n\n"
            "💰 Стоимость: 500 ₽\n\n"
            "💡 **Совет:** Оформите подписку за 250 ₽/мес и получайте 1 бесплатную консультацию каждый месяц!\n\n"
            "Нажмите на кнопку для оплаты:",
            reply_markup=kb,
            parse_mode="Markdown"
        )

    await state.set_state(ConsultStates.choosing_doctor)
    await callback.answer()

@dp.callback_query(F.data == "use_free_consult")
async def use_free_consult(callback: CallbackQuery, state: FSMContext):
    """Обработчик использования бесплатной консультации"""
    user_id = callback.from_user.id

    # Повторно проверяем начисление
    check_and_add_monthly_free_consult(user_id)

    user = get_user(user_id)
    free_consults = user.get("free_consultations", 0)

    if free_consults <= 0:
        await callback.message.answer("❌ У вас нет бесплатных консультаций. Выберите платный вариант.")
        await callback.answer()
        return

    # Уменьшаем счётчик
    supabase.table("users").update({"free_consultations": free_consults - 1}).eq("user_id", user_id).execute()

    data = await state.get_data()
    doctor_id = data.get("selected_doctor_id")

    if not doctor_id:
        doctors = supabase.table("doctors").select("id").eq("is_available", True).execute()
        if not doctors.data:
            await callback.message.answer("Нет доступных врачей. Попробуйте позже.")
            return
        doctor_id = doctors.data[0]["id"]
        await state.update_data(selected_doctor_id=doctor_id)

    # Создаём заявку
    res = supabase.table("consult_requests").insert({
        "user_id": user_id,
        "doctor_id": doctor_id,
        "status": "paid",
        "payment_method": "free_subscription"
    }).execute()

    consult_id = res.data[0]["id"]
    await state.update_data(consult_id=consult_id)

    # Исправленное сообщение (без незакрытых форматирований)
    await callback.message.answer(
        "✅ БЕСПЛАТНАЯ КОНСУЛЬТАЦИЯ АКТИВИРОВАНА!\n\n"
        "Теперь опишите проблему и пришлите фото (1-3).\n"
        "Для завершения отправьте /finish_consult"
    )
    await state.set_state(ConsultStates.waiting_for_question)
    await callback.answer()


@dp.callback_query(F.data == "consult_pay_rubles")
async def consult_pay_rubles(callback: CallbackQuery, state: FSMContext):
    """Оплата консультации рублями через ЮKassa"""
    user_id = callback.from_user.id
    data = await state.get_data()
    doctor_id = data.get("selected_doctor_id")

    if not doctor_id:
        await callback.message.answer("❌ Ошибка: выберите врача заново.")
        return

    # Создаём заявку со статусом waiting_payment
    res = supabase.table("consult_requests").insert({
        "user_id": user_id,
        "doctor_id": doctor_id,
        "status": "waiting_payment",
        "payment_method": "yookassa"
    }).execute()
    consult_id = res.data[0]["id"]
    await state.update_data(consult_id=consult_id)

    amount = 500.00
    payment_url, payment_id = create_yookassa_payment(
        user_id=user_id,
        amount=amount,
        description=f"Консультация #{consult_id} для пользователя {user_id}",
        payment_type="consult"
    )

    pending_payments[payment_id] = {"user_id": user_id, "type": "consult", "consult_id": consult_id}

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Перейти к оплате 500 ₽", url=payment_url)],
        [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"check_payment_{payment_id}")]
    ])

    await callback.message.edit_text(
        "💰 **Оплата консультации**\n\n"
        "Стоимость: 500 ₽\n\n"
        "Нажмите на кнопку, чтобы оплатить картой или через СБП.\n"
        "После оплаты нажмите 'Я оплатил'.",
        reply_markup=kb
    )
    await callback.answer()


@dp.callback_query(F.data.startswith("check_payment_"))
async def check_payment(callback: CallbackQuery, state: FSMContext):
    payment_id = callback.data.split("_")[2]
    payment_data = pending_payments.get(payment_id)

    if not payment_data:
        await callback.answer("Платёж не найден.", show_alert=True)
        return

    try:
        payment = Payment.find_one(payment_id)

        if payment.status == "succeeded":
            user_id = payment_data["user_id"]

            if payment_data["type"] == "subscription":
                activate_subscription(user_id, days=30)
                await callback.message.answer("✅ Подписка активирована! Спасибо за оплату.")
                await callback.message.answer("Главное меню:", reply_markup=main_menu_keyboard())

            elif payment_data["type"] == "consult":
                consult_id = payment_data["consult_id"]
                supabase.table("consult_requests").update({"status": "paid"}).eq("id", consult_id).execute()
                await callback.message.answer(
                    "✅ **Оплата получена!**\n\n"
                    "Теперь опишите проблему и пришлите фото (1-3).\n"
                    "Для завершения отправьте /finish_consult"
                )
                await state.set_state(ConsultStates.waiting_for_question)
                await state.update_data(consult_id=consult_id)

            del pending_payments[payment_id]
        else:
            await callback.answer(f"❌ Платёж ещё не завершён. Статус: {payment.status}", show_alert=True)
    except Exception as e:
        await callback.answer(f"Ошибка проверки: {str(e)}", show_alert=True)

    await callback.answer()


@dp.message(Command("profile"))
async def cmd_profile(message: Message):
    user_id = message.from_user.id
    user = get_user(user_id)

    # Проверяем подписку и начисление консультаций
    check_and_add_monthly_free_consult(user_id)
    user = get_user(user_id)

    # Получаем информацию о бесплатных вопросах
    free_questions_remaining = get_remaining_free_questions(user_id)
    free_questions_used = get_free_questions_used(user_id)

    free_consults = user.get("free_consultations", 0)
    subscription_end = user.get("subscription_end")

    is_sub_active = subscription_end and datetime.fromisoformat(
        subscription_end.replace('Z', '+00:00')) > datetime.now()
    status = "✅ Активна" if is_sub_active else "❌ Неактивна"

    if subscription_end:
        end_date = datetime.fromisoformat(subscription_end.replace('Z', '+00:00'))
        end_str = end_date.strftime("%d.%m.%Y")
    else:
        end_str = "—"

    last_date_str = user.get("last_free_consult_date")
    last_str = last_date_str if last_date_str else "—"

    # Формируем текст
    text = f"👤 **Ваш профиль**\n\n"
    text += f"📅 Подписка: {status}\n"
    if is_sub_active:
        text += f"🗓️ Действует до: {end_str}\n"

    text += f"\n🎓 **Бесплатные вопросы ИИ:**\n"
    text += f"• Использовано: {free_questions_used}/3\n"
    if not is_sub_active:
        text += f"• Осталось: {free_questions_remaining}\n"
        text += f"💡 После 3 вопросов нужно оформить подписку\n"
    else:
        text += f"• Безлимит (активна подписка)\n"

    text += f"\n🩺 **Бесплатные консультации с врачом:**\n"
    text += f"• Доступно: {free_consults}\n"
    text += f"• Последнее начисление: {last_str}\n\n"
    text += f"💡 Каждый месяц подписки даёт +1 бесплатную консультацию!"

    await message.answer(text, parse_mode="Markdown")

@dp.callback_query(F.data.startswith("select_doctor_"))
async def select_doctor(callback: CallbackQuery, state: FSMContext):
    doctor_id = int(callback.data.split("_")[2])
    await state.update_data(selected_doctor_id=doctor_id)
    await callback.message.answer("Стоимость 500 руб. Выберите способ оплаты:", reply_markup=consult_payment_methods_keyboard())
    await callback.answer()

@dp.message(Command("questions"))
async def cmd_questions(message: Message):
    """Показать остаток бесплатных вопросов"""
    user_id = message.from_user.id

    if is_subscription_active(user_id):
        await message.answer("✅ У вас активна подписка! Вопросы к ИИ — **безлимит**.")
        return

    used = get_free_questions_used(user_id)
    remaining = max(0, 3 - used)

    text = f"🎓 **Бесплатные вопросы к ИИ:**\n"
    text += f"• Использовано: {used}/3\n"
    text += f"• Осталось: {remaining}\n\n"

    if remaining == 0:
        text += "❌ Бесплатные вопросы закончились.\n"
        text += "Оформите подписку: /subscribe"
    else:
        text += "💡 Задайте вопрос командой /ask"

    await message.answer(text, parse_mode="Markdown")

@dp.message(ConsultStates.waiting_for_question, F.text)
async def collect_consult_question(message: Message, state: FSMContext):
    if message.text.startswith('/'):
        return
    if len(message.text) < 10:
        await message.answer("Пожалуйста, опишите проблему подробнее (минимум 10 символов).")
        return
    await state.update_data(consult_question=message.text)
    await message.answer(
        "✅ Вопрос сохранён.\n\n"
        "Теперь можете отправить фото (1-3).\n"
        "Или сразу нажмите /finish_consult"
    )
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
        await state.set_state(ConsultStates.waiting_for_question)
    else:
        await state.update_data(photos=photos)
        await message.answer(f"Фото добавлено ({len(photos)}/3). Можно ещё или /finish_consult")


@dp.message(Command("finish_consult"))
async def finish_consult(message: Message, state: FSMContext):
    user_id = message.from_user.id
    data = await state.get_data()
    consult_id = data.get("consult_id")
    question = data.get("consult_question")
    photos = data.get("photos", [])

    # Проверяем, есть ли вопрос
    if not question:
        await message.answer(
            "❌ Вы не ввели вопрос.\n\n"
            "Чтобы начать консультацию:\n"
            "1. Нажмите /doctor_consult\n"
            "2. Выберите способ оплаты\n"
            "3. Напишите вопрос\n"
            "4. Отправьте фото (если нужно)\n"
            "5. Нажмите /finish_consult"
        )
        await state.clear()
        return

    # Проверяем, есть ли заявка
    if not consult_id:
        await message.answer(
            "❌ Не найдена активная консультация.\n\n"
            "Пожалуйста, начните заново: /doctor_consult"
        )
        await state.clear()
        return

    # Обновляем заявку с вопросом и фото
    try:
        supabase.table("consult_requests").update({
            "question": question,
            "photos": photos
        }).eq("id", consult_id).execute()

        # Получаем данные о заявке и враче
        req = supabase.table("consult_requests").select("doctor_id, user_id").eq("id", consult_id).execute()
        if not req.data:
            await message.answer("❌ Ошибка: заявка не найдена.")
            await state.clear()
            return

        doctor_id = req.data[0]["doctor_id"]
        doctor = supabase.table("doctors").select("tg_user_id, name").eq("id", doctor_id).execute()

        if not doctor.data:
            await message.answer("❌ Ошибка: врач не найден.")
            await state.clear()
            return

        doctor_tg_id = doctor.data[0]["tg_user_id"]
        doctor_name = doctor.data[0].get("name", "Ветеринар")

        # Формируем сообщение врачу
        user = message.from_user
        pet_info = get_user(user_id)
        pet_text = f"Вид: {pet_info.get('pet_type', 'не указан')}, Возраст: {pet_info.get('pet_age', 'не указан')}" if pet_info else "не указана"

        text = (
            f"🆕 НОВАЯ КОНСУЛЬТАЦИЯ #{consult_id}\n\n"
            f"👤 Пользователь: {user.full_name}\n"
            f"🆔 ID: {user_id}\n"
            f"🐾 Питомец: {pet_text}\n\n"
            f"📝 Вопрос:\n{question}\n"
        )

        # Отправляем врачу
        if photos:
            await bot.send_photo(doctor_tg_id, photo=photos[0], caption=text)
            if len(photos) > 1:
                media = [types.InputMediaPhoto(media=photo) for photo in photos[1:]]
                await bot.send_media_group(doctor_tg_id, media=media)
        else:
            await bot.send_message(doctor_tg_id, text)

        # Подтверждение пользователю
        await message.answer(
            f"✅ **Запрос отправлен врачу!**\n\n"
            f"📋 Ваш вопрос: {question[:100]}...\n"
            f"📸 Фото: {len(photos)} шт.\n\n"
            f"👨‍⚕️ Врач {doctor_name} ответит вам в течение 4 часов.\n\n"
            f"Ответ придёт в этот чат.",
            parse_mode="Markdown"
        )

    except Exception as e:
        await message.answer(f"❌ Ошибка при отправке: {str(e)}")

    finally:
        await state.clear()
@dp.message(Command("cancel_consult"))
async def cancel_consult(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Консультация отменена.", reply_markup=main_menu_keyboard())

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
    res = supabase.table("consult_requests").update({
        "status": "answered",
        "doctor_answer": answer_text,
        "answered_at": datetime.now().isoformat()
    }).eq("id", consult_id).eq("doctor_id", doctor.data[0]["id"]).execute()
    if not res.data:
        await message.answer("Заявка не найдена или доступ запрещён.")
        return
    user_id = res.data[0]["user_id"]
    await bot.send_message(user_id, f"🩺 Ответ врача:\n\n{answer_text}\n\n⚠️ Консультация не заменяет очный осмотр.")
    await message.answer(f"✅ Ответ отправлен пользователю (заявка #{consult_id})")

# ----- Реферальная программа -----
@dp.callback_query(F.data == "referral_info")
async def referral_info_callback(callback: CallbackQuery):
    user_id = callback.from_user.id
    stats = get_referral_stats(user_id)
    bot_username = (await bot.get_me()).username
    link = f"https://t.me/{bot_username}?start={user_id}"

    text = (
        f"👥 **Реферальная программа**\n\n"
        f"Ваша ссылка: `{link}`\n\n"
        f"📊 **Статистика:**\n"
        f"• Приглашено друзей: {stats['total']}\n"
        f"• Оформили подписку: {stats['activated']}/3\n\n"
    )

    if stats['activated'] >= 3:
        text += "✅ **Вы уже получили 3 месяца подписки бесплатно!**\n"
    else:
        text += f"🎁 Осталось пригласить **{stats['needed']}** друга(ей) с подпиской, чтобы получить **3 месяца бесплатно**!"

    await callback.message.answer(text, parse_mode="Markdown")
    await callback.answer()


@dp.message(Command("referral"))
async def cmd_referral(message: Message):
    user_id = message.from_user.id
    stats = get_referral_stats(user_id)
    bot_username = (await bot.get_me()).username
    link = f"https://t.me/{bot_username}?start={user_id}"

    text = (
        f"👥 **Реферальная программа**\n\n"
        f"Ваша ссылка: `{link}`\n\n"
        f"📊 **Статистика:**\n"
        f"• Приглашено друзей: {stats['total']}\n"
        f"• Оформили подписку: {stats['activated']}/3\n\n"
    )

    if stats['activated'] >= 3:
        text += "✅ **Вы уже получили 3 месяца подписки бесплатно!**\n"
    else:
        text += f"🎁 Осталось пригласить **{stats['needed']}** друга(ей) с подпиской, чтобы получить **3 месяца бесплатно**!"

    await message.answer(text, parse_mode="Markdown")
# ----- Напоминания -----
@dp.callback_query(F.data == "reminders_menu")
async def reminders_menu(callback: CallbackQuery):
    await callback.message.answer("Управление напоминаниями:", reply_markup=reminders_menu_keyboard())
    await callback.answer()

@dp.message(Command("reminders"))
async def cmd_reminders(message: Message):
    """Обработчик текстовой команды /reminders из меню"""
    kb = reminders_menu_keyboard()
    await message.answer("📅 Управление напоминаниями:", reply_markup=kb)
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
        text = "📋 Активные напоминания:\n"
        for r in res.data:
            dt = datetime.fromisoformat(r['remind_at'].replace('Z', '+00:00'))
            text += f"• {r['title']} — {dt.strftime('%d.%m.%Y %H:%M')}\n"
        await callback.message.answer(text)
    await callback.answer()

# ----- Навигация -----
@dp.callback_query(F.data == "back_to_main")
async def back_to_main(callback: CallbackQuery):
    await callback.message.edit_text("Главное меню:", reply_markup=main_menu_keyboard())
    await callback.answer()

@dp.callback_query(F.data == "subscription_menu")
async def subscription_menu(callback: CallbackQuery):
    await callback.message.edit_text("Выберите способ оплаты подписки (250 ₽/мес):", reply_markup=subscription_methods_keyboard())
    await callback.answer()


# ----- Планировщик напоминаний -----
async def reminder_scheduler():
    while True:
        now = datetime.now()
        res = supabase.table("reminders").select("*").eq("is_completed", False).execute()
        for rem in res.data:
            remind_at = datetime.fromisoformat(rem['remind_at'].replace('Z', '+00:00'))
            if remind_at <= now:
                try:
                    await bot.send_message(rem['user_id'], f"🔔 Напоминание: {rem['title']}")
                    supabase.table("reminders").update({"is_completed": True}).eq("id", rem['id']).execute()
                except Exception as e:
                    logging.error(f"Reminder error: {e}")
        await asyncio.sleep(60)

# ----- Запуск -----
async def main():
    asyncio.create_task(reminder_scheduler())
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())