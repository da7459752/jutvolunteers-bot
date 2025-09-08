import asyncio
import logging
import os
import asyncpg
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiohttp import web


# --- Логирование ---
logging.basicConfig(level=logging.INFO)

# --- Переменные окружения ---
TOKEN = os.getenv("TOKEN")
DB_DSN = os.getenv("DATABASE_URL")

bot = Bot(token=TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

db_pool = None  # глобальная переменная для подключения к БД

# --- Подключение к БД ---
async def create_db_pool():
    return await asyncpg.create_pool(dsn=DB_DSN)

# --- Создание таблиц ---
async def init_db():
    async with db_pool.acquire() as conn:
        # volunteers
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS volunteers (
            id SERIAL PRIMARY KEY,
            full_name TEXT NOT NULL,
            contacts TEXT NOT NULL,
            status TEXT DEFAULT 'Active',
            lateness_count INT DEFAULT 0,
            warnings_count INT DEFAULT 0
        );
        """)
        # blacklist
        await conn.execute("""
        CREATE TABLE IF NOT EXISTS blacklist (
            id SERIAL PRIMARY KEY,
            full_name TEXT NOT NULL,
            reason TEXT,
            added TIMESTAMP DEFAULT NOW(),
            UNIQUE(full_name)
        );
        """)

# --- FSM ---
class LatenessStates(StatesGroup):
    waiting_for_id = State()

class AddVolunteerStates(StatesGroup):
    waiting_for_full_name = State()
    waiting_for_contact = State()

class EditVolunteerStates(StatesGroup):
    waiting_for_id = State()
    waiting_for_field = State()
    waiting_for_new_value = State()

class SearchVolunteerStates(StatesGroup):
    waiting_for_query = State()

class DeleteVolunteerStates(StatesGroup):
    waiting_for_id = State()
    confirm_delete = State()

class WarningStates(StatesGroup):
    waiting_for_id = State()

class BlacklistDirectStates(StatesGroup):
    waiting_for_id = State()
    waiting_for_reason = State()

# --- Главное меню ---
def main_menu():
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="👥 Волонтёры", callback_data="menu_volunteers"),
                InlineKeyboardButton(text="⏰ Опоздания", callback_data="menu_lateness")
            ],
            [
                InlineKeyboardButton(text="⚠️ Замечание", callback_data="menu_warning"),
                InlineKeyboardButton(text="🚫 ЧС (вручную)", callback_data="menu_blacklist_direct")
            ],
            [
                InlineKeyboardButton(text="➕ Добавить", callback_data="menu_add_volunteer"),
                InlineKeyboardButton(text="✏️ Редактировать", callback_data="menu_edit_volunteer")
            ],
            [
                InlineKeyboardButton(text="🚫 Чёрный список", callback_data="menu_blacklist"),
                InlineKeyboardButton(text="⚙ Управление", callback_data="menu_manage")
            ]
        ]
    )
    return kb

# --- Дополнительное меню ---
def manage_menu():
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📊 Статистика", callback_data="menu_statistics"),
                InlineKeyboardButton(text="🔍 Поиск", callback_data="menu_search")
            ],
            [
                InlineKeyboardButton(text="🗑 Удалить волонтёра", callback_data="menu_delete_volunteer")
            ],
            [
                InlineKeyboardButton(text="⬅ Назад", callback_data="menu_back")
            ]
        ]
    )
    return kb

# --- Получение списка ---
async def get_volunteers():
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, full_name, status, contacts, lateness_count, warnings_count FROM volunteers ORDER BY id")
        return rows

async def get_blacklist():
    async with db_pool.acquire() as conn:
        rows = await conn.fetch("SELECT id, full_name, reason, added FROM blacklist ORDER BY id")
        return rows

# --- Добавление ---
async def add_volunteer(full_name: str, contacts: str):
    async with db_pool.acquire() as conn:
        existing = await conn.fetchrow(
            "SELECT id FROM volunteers WHERE full_name=$1 AND contacts=$2",
            full_name, contacts
        )
        if existing:
            raise ValueError("Такой волонтёр уже существует!")
        await conn.execute(
            "INSERT INTO volunteers (full_name, contacts) VALUES ($1, $2)",
            full_name, contacts
        )

# --- Подсчёт нарушений ---
async def check_and_blacklist(volunteer_id: int, message: types.Message):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, full_name, lateness_count, warnings_count, status FROM volunteers WHERE id = $1",
            volunteer_id
        )
        if not row:
            await message.answer("❌ Волонтёр не найден")
            return

        total_violations = (row['lateness_count'] or 0) + (row['warnings_count'] or 0)

        if total_violations >= 3 and row['status'] != "Blacklisted":
            await conn.execute("UPDATE volunteers SET status = 'Blacklisted' WHERE id = $1", volunteer_id)
            await conn.execute(
                "INSERT INTO blacklist (full_name, reason) VALUES ($1, $2) ON CONFLICT DO NOTHING",
                row['full_name'], f"{total_violations} нарушений (опоздания + замечания)"
            )
            await message.answer(f"🚨 Волонтёр {row['full_name']} внесён в ЧС! ({total_violations} нарушений)")
        else:
            await message.answer(
                f"⚠ Нарушение зафиксировано: {row['full_name']} "
                f"(Опозданий: {row['lateness_count']}, Замечаний: {row['warnings_count']}, Всего: {total_violations})"
            )

# --- Фиксация опоздания ---
async def add_lateness(volunteer_id: int, message: types.Message):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE volunteers SET lateness_count = lateness_count + 1 WHERE id = $1",
            volunteer_id
        )
    await check_and_blacklist(volunteer_id, message)

# --- Фиксация замечания ---
async def add_warning(volunteer_id: int, message: types.Message):
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE volunteers SET warnings_count = warnings_count + 1 WHERE id = $1",
            volunteer_id
        )
    await check_and_blacklist(volunteer_id, message)

# --- Прямое добавление в ЧС ---
async def add_direct_blacklist(volunteer_id: int, reason: str, message: types.Message):
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, full_name, status FROM volunteers WHERE id = $1",
            volunteer_id
        )
        if not row:
            await message.answer("❌ Волонтёр не найден")
            return
        await conn.execute("UPDATE volunteers SET status = 'Blacklisted' WHERE id = $1", volunteer_id)
        await conn.execute(
            "INSERT INTO blacklist (full_name, reason) VALUES ($1, $2) ON CONFLICT DO NOTHING",
            row['full_name'], reason
        )
        await message.answer(f"🚫 Волонтёр {row['full_name']} внесён в ЧС. Причина: {reason}")

# --- Старт ---
@dp.message(Command("start"))
async def start(message: types.Message):
    await message.answer("📌 Главное меню:", reply_markup=main_menu())


PAGE_SIZE = 5  # сколько записей показываем на странице

# Функция для создания кнопок пагинации
def pagination_markup(page: int, total_pages: int, prefix: str):
    buttons = []
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"{prefix}_page_{page-1}"))
    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("➡️ Вперёд", callback_data=f"{prefix}_page_{page+1}"))
    if nav_buttons:
        buttons.append(nav_buttons)
    buttons.append([InlineKeyboardButton("Главное меню", callback_data="menu_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# Функция показа записей с пагинацией
async def show_records(query: types.CallbackQuery, rows, page: int = 0, prefix: str = "volunteers", title: str = "Список"):
    if not rows:
        await query.message.edit_text(f"{title} пуст.", reply_markup=main_menu())
        return

    total_pages = (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE

    text = f"{title}:\n"
    for r in rows[start:end]:
        # Можно менять формат под волонтёров или черный список
        if prefix == "volunteers":
            text += f"{r['id']}. {r['full_name']} | {r['status']} | {r['contacts']} | Опозданий: {r['lateness_count']} | Замечаний: {r['warnings_count']}\n"
        elif prefix == "blacklist":
            text += f"{r['id']}. {r['full_name']} | {r['reason']}\n"

    await query.message.edit_text(text, reply_markup=pagination_markup(page, total_pages, prefix))



# --- Колбэки ---
@dp.callback_query()
async def callbacks(query: types.CallbackQuery, state: FSMContext):
    if query.data == "menu_volunteers":
        rows = await get_volunteers()
        await show_records(query, rows, page=0, prefix="volunteers", title="Список волонтёров")

    elif query.data == "menu_lateness":
        await query.message.edit_text("Введите ID волонтёра для фиксации опоздания:")
        await state.set_state(LatenessStates.waiting_for_id)

    elif query.data == "menu_warning":
        await query.message.edit_text("Введите ID волонтёра для фиксации замечания:")
        await state.set_state(WarningStates.waiting_for_id)

    elif query.data == "menu_blacklist":
        rows = await get_blacklist()
        await show_records(query, rows, page=0, prefix="blacklist", title="Черный список")

    elif query.data == "menu_blacklist_direct":
        await query.message.edit_text("Введите ID волонтёра для добавления в ЧС:")
        await state.set_state(BlacklistDirectStates.waiting_for_id)

    elif query.data == "menu_add_volunteer":
        await query.message.edit_text("Введите ФИО нового волонтёра:")
        await state.set_state(AddVolunteerStates.waiting_for_full_name)

    elif query.data == "menu_edit_volunteer":
        await query.message.edit_text("Введите ID волонтёра для редактирования:")
        await state.set_state(EditVolunteerStates.waiting_for_id)

    elif query.data == "menu_manage":
        await query.message.edit_text("⚙ Выберите действие:", reply_markup=manage_menu())

    elif query.data == "menu_statistics":
        async with db_pool.acquire() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM volunteers")
            total_lates = await conn.fetchval("SELECT SUM(lateness_count) FROM volunteers")
            total_warnings = await conn.fetchval("SELECT SUM(warnings_count) FROM volunteers")
            blacklist_count = await conn.fetchval("SELECT COUNT(*) FROM volunteers WHERE status='Blacklisted'")
        text = (
            f"📊 Статистика:\n"
            f"Всего волонтёров: {total}\n"
            f"Всего опозданий: {total_lates or 0}\n"
            f"Всего замечаний: {total_warnings or 0}\n"
            f"В ЧС: {blacklist_count}"
        )
        await query.message.edit_text(text, reply_markup=manage_menu())

    elif query.data == "menu_search":
        await query.message.edit_text("Введите ФИО или телефон для поиска:")
        await state.set_state(SearchVolunteerStates.waiting_for_query)

    elif query.data == "menu_delete_volunteer":
        await query.message.edit_text("Введите ID волонтёра для удаления:")
        await state.set_state(DeleteVolunteerStates.waiting_for_id)

    elif query.data == "menu_back":
        await query.message.edit_text("📌 Главное меню:", reply_markup=main_menu())

    elif query.data == "confirm_delete_yes":
        data = await state.get_data()
        volunteer_id = data.get("volunteer_id")
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM volunteers WHERE id=$1", volunteer_id)
        await query.message.edit_text("🗑 Волонтёр удалён.", reply_markup=manage_menu())
        await state.clear()

    elif query.data == "confirm_delete_no":
        await query.message.edit_text("❌ Удаление отменено.", reply_markup=manage_menu())
        await state.clear()

    elif query.data.startswith("edit_field_"):
        field = query.data.replace("edit_field_", "")
        await state.update_data(field=field)
        await state.set_state(EditVolunteerStates.waiting_for_new_value)
        await query.message.answer(f"Введите новое значение для {field}:")
        await query.answer()


# Обработка кнопок пагинации
@dp.callback_query()
async def paginate_records(query: types.CallbackQuery, state: FSMContext):
    if "_page_" not in query.data:
        return  # это не пагинация

    parts = query.data.split("_page_")
    prefix = parts[0]
    page = int(parts[1])

    if prefix == "volunteers":
        rows = await get_volunteers()
        await show_records(query, rows, page, prefix="volunteers", title="Список волонтёров")
    elif prefix == "blacklist":
        rows = await get_blacklist()
        await show_records(query, rows, page, prefix="blacklist", title="Черный список")



# --- FSM обработка ---
@dp.message()
async def handle_messages(message: types.Message, state: FSMContext):
    current_state = await state.get_state()

    if current_state == LatenessStates.waiting_for_id.state:
        try:
            volunteer_id = int(message.text)
            await add_lateness(volunteer_id, message)
        except ValueError:
            await message.answer("⚠ Введите число (ID).")
        finally:
            await state.clear()
            await message.answer("📌 Главное меню:", reply_markup=main_menu())

    elif current_state == WarningStates.waiting_for_id.state:
        try:
            volunteer_id = int(message.text)
            await add_warning(volunteer_id, message)
        except ValueError:
            await message.answer("⚠ Введите число (ID).")
        finally:
            await state.clear()
            await message.answer("📌 Главное меню:", reply_markup=main_menu())

    elif current_state == BlacklistDirectStates.waiting_for_id.state:
        try:
            volunteer_id = int(message.text)
            await state.update_data(volunteer_id=volunteer_id)
            await message.answer("Введите причину внесения в ЧС:")
            await state.set_state(BlacklistDirectStates.waiting_for_reason)
        except ValueError:
            await message.answer("⚠ Введите число (ID).")

    elif current_state == BlacklistDirectStates.waiting_for_reason.state:
        data = await state.get_data()
        volunteer_id = data['volunteer_id']
        reason = message.text
        await add_direct_blacklist(volunteer_id, reason, message)
        await state.clear()
        await message.answer("📌 Главное меню:", reply_markup=main_menu())

    elif current_state == AddVolunteerStates.waiting_for_full_name.state:
        await state.update_data(full_name=message.text)
        await message.answer("Введите номер телефона:")
        await state.set_state(AddVolunteerStates.waiting_for_contact)

    elif current_state == AddVolunteerStates.waiting_for_contact.state:
        data = await state.get_data()
        full_name = data.get("full_name")
        contacts = message.text
        try:
            await add_volunteer(full_name, contacts)
            await message.answer(f"✅ Волонтёр {full_name} добавлен.")
        except ValueError as ve:
            await message.answer(f"⚠ {ve}")
        finally:
            await state.clear()
            await message.answer("📌 Главное меню:", reply_markup=main_menu())

    elif current_state == EditVolunteerStates.waiting_for_id.state:
        try:
            volunteer_id = int(message.text)
            await state.update_data(volunteer_id=volunteer_id)
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="✏️ ФИО", callback_data="edit_field_full_name"),
                        InlineKeyboardButton(text="📞 Контакт", callback_data="edit_field_contacts")
                    ]
                ]
            )
            await message.answer("Выберите поле для редактирования:", reply_markup=kb)
            await state.set_state(EditVolunteerStates.waiting_for_field)
        except ValueError:
            await message.answer("⚠ Введите число (ID).")

    elif current_state == EditVolunteerStates.waiting_for_new_value.state:
        data = await state.get_data()
        volunteer_id = data['volunteer_id']
        field = data['field']
        new_value = message.text
        async with db_pool.acquire() as conn:
            await conn.execute(f"UPDATE volunteers SET {field}=$1 WHERE id=$2", new_value, volunteer_id)
        await message.answer(f"✅ {field} обновлено: {new_value}")
        await state.clear()
        await message.answer("📌 Главное меню:", reply_markup=main_menu())

    elif current_state == SearchVolunteerStates.waiting_for_query.state:
        query_text = message.text.strip()
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT id, full_name, status, contacts, lateness_count, warnings_count FROM volunteers "
                "WHERE full_name ILIKE $1 OR contacts ILIKE $1",
                f"%{query_text}%"
            )
        if rows:
            text = "🔍 Результаты поиска:\n"
            for r in rows:
                text += f"{r['id']}. {r['full_name']} | {r['status']} | {r['contacts']} | Опозданий: {r['lateness_count']} | Замечаний: {r['warnings_count']}\n"
        else:
            text = "❌ Ничего не найдено."
        await message.answer(text, reply_markup=manage_menu())
        await state.clear()

    elif current_state == DeleteVolunteerStates.waiting_for_id.state:
        try:
            volunteer_id = int(message.text)
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow("SELECT id, full_name FROM volunteers WHERE id=$1", volunteer_id)
            if not row:
                await message.answer("❌ Волонтёр не найден.", reply_markup=manage_menu())
                await state.clear()
            else:
                await state.update_data(volunteer_id=volunteer_id)
                kb = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(text="✅ Да, удалить", callback_data="confirm_delete_yes"),
                            InlineKeyboardButton(text="❌ Отмена", callback_data="confirm_delete_no")
                        ]
                    ]
                )
                await message.answer(f"Вы уверены, что хотите удалить {row['full_name']}?", reply_markup=kb)
                await state.set_state(DeleteVolunteerStates.confirm_delete)
        except ValueError:
            await message.answer("⚠ Введите число (ID).")

# --- Обработчик вебхуков ---
async def handle_webhook(request):
    data = await request.json()
    await dp.feed_webhook_update(bot, data)
    return web.Response()

# --- Запуск ---
async def main():
    global db_pool
    db_pool = await create_db_pool()
    logging.info("DB connected")

    # создаём таблицы
    await init_db()
    logging.info("Tables checked/created")

    # Устанавливаем webhook
    webhook_url = f"https://{os.environ['RENDER_EXTERNAL_HOSTNAME']}/webhook/{TOKEN}"
    await bot.set_webhook(webhook_url)
    logging.info(f"Webhook set to {webhook_url}")

    # Настройка aiohttp приложения
    app = web.Application()
    app.router.add_post(f"/webhook/{TOKEN}", handle_webhook)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
    logging.info("Server started on port 8080")

    # Запуск бесконечного цикла
    try:
        await asyncio.Future()  # Запуск бесконечного цикла
    except KeyboardInterrupt:
        pass
    finally:
        await runner.cleanup()
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
