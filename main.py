import asyncio
import os
import time
import httpx
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton
from PIL import Image
from dotenv import load_dotenv
from aiohttp import web

from downloader import download_soundcloud_track

# =====================================================================
# 1. ИНИЦИАЛИЗАЦИЯ БОТА И НАСТРОЙКИ
# =====================================================================

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise ValueError("❌ Ошибка: Переменная BOT_TOKEN не найдена в файле .env!")

bot = Bot(token=TOKEN)
dp = Dispatcher()

# Словарь для защиты от флуда
user_cooldowns = {}
# Словарь для отслеживания ID последнего сообщения меню у каждого юзера
user_menus = {}

# Очередь загрузок: замок и счётчик активных задач
download_lock = asyncio.Lock()
active_downloads = 0


# =====================================================================
# 2. КЛАВИАТУРЫ И ИНТЕРФЕЙС (MARKUP)
# =====================================================================

def get_main_menu():
    buttons = [
        [
            InlineKeyboardButton(text="ℹ️ FAQ", callback_data="menu_info"),
            InlineKeyboardButton(text="💬 Связь", callback_data="menu_donate")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_cancel_menu():
    buttons = [[InlineKeyboardButton(text="📱 Главное меню", callback_data="menu_cancel")]]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


support_keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [
        InlineKeyboardButton(text="💬 Написать создателю", url="tg://resolve?domain=trollzz1q")
    ],
    [
        InlineKeyboardButton(text="📱 Главное меню", callback_data="menu_cancel")
    ]
])


# =====================================================================
# 3. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (UTILITIES)
# =====================================================================

def process_thumbnail(image_path: str):
    with Image.open(image_path) as img:
        if img.mode != 'RGB':
            img = img.convert('RGB')

        min_side = min(img.size)
        left = (img.width - min_side) // 2
        top = (img.height - min_side) // 2
        right = left + min_side
        bottom = top + min_side

        img = img.crop((left, top, right, bottom))
        img = img.resize((300, 300), Image.Resampling.LANCZOS)
        img.save(image_path, "JPEG", quality=85)


async def update_progress_bar(message: types.Message, percent: float, last_update_time: list, status_prefix: str = ""):
    current_time = time.time()

    if current_time - last_update_time[0] < 2.0 and percent < 100:
        return

    last_update_time[0] = current_time

    steps = 10
    filled = int((percent / 100) * steps)
    bar = "🟧" * filled + "⬜" * (steps - filled)

    try:
        await message.edit_text(f"{status_prefix}{bar} {int(percent)}%")
    except Exception:
        pass


# =====================================================================
# 4. НАВИГАЦИЯ И ОБРАБОТКА МЕНЮ
# =====================================================================

@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    text_content = (
        f"👋 Привет, {message.from_user.first_name}!\n\n"
        "☁️ Cloudly Bot 2.5\n\n"
        "⚡ Отправь мне ссылку из SoundCloud в чат"
    )
    menu_msg = await message.answer(text=text_content, reply_markup=get_main_menu())
    user_menus[message.from_user.id] = menu_msg.message_id


@dp.callback_query(F.data == "menu_info")
async def press_info(callback: types.CallbackQuery):
    info_text = (
        "ℹ️ Информация ℹ️\n\n"
        "• Бот умеет скачивать аудио из SoundCloud в формате MP3.\n\n"
        "• Лимит на размер одного файла: 50 МБ (ограничение Telegram).\n\n"
        "• Принимаются только ссылки на синглы (не плейлисты)!"
    )
    user_menus[callback.from_user.id] = callback.message.message_id
    await callback.message.edit_text(text=info_text, reply_markup=get_cancel_menu())
    await callback.answer()


@dp.callback_query(F.data == "menu_donate")
async def press_donate(callback: types.CallbackQuery):
    donate_text = (
        "✨ Поддержка проекта ✨\n\n"
        "Если тебе нравится бот и ты хочешь помочь с оплатой хостинга или предложить идею - нажми на кнопку ниже и напиши создателю проекта напрямую!"
    )
    user_menus[callback.from_user.id] = callback.message.message_id
    await callback.message.edit_text(text=donate_text, reply_markup=support_keyboard)
    await callback.answer()


@dp.callback_query(F.data == "menu_cancel")
async def press_cancel(callback: types.CallbackQuery):
    await callback.answer()
    user_menus[callback.from_user.id] = callback.message.message_id
    await callback.message.edit_text(
        "☁️ Cloudly Bot 2.5\n\n"
        "⚡ Отправь мне ссылку из SoundCloud в чат",
        reply_markup=get_main_menu()
    )


# =====================================================================
# 5. ПРИЕМ ССЫЛОК И СКАЧИВАНИЕ МУЗЫКИ (CORE LOGIC)
# =====================================================================

@dp.message(F.text.contains("soundcloud.com"))
async def handle_link(message: types.Message):
    global active_downloads
    user_id = message.from_user.id
    current_time = time.time()

    if len(message.text) > 300:
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer("❌ Ошибка: Сообщение слишком длинное!", reply_markup=get_cancel_menu())
        return

    if user_id in user_cooldowns:
        if current_time - user_cooldowns[user_id] < 1.5:
            try:
                await message.delete()
            except Exception:
                pass
            return

    user_cooldowns[user_id] = current_time
    url = message.text.strip()

    try:
        await message.delete()
    except Exception:
        pass

    # Увеличиваем счетчик активных задач
    active_downloads += 1

    old_menu_id = user_menus.get(user_id)
    if old_menu_id:
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=old_menu_id)
            user_menus[user_id] = None
        except Exception:
            pass

    status_prefix = "⏳ В очереди... \n" if download_lock.locked() else ""
    status_msg = await message.answer(f"{status_prefix}⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜ 0%")

    last_update = [0.0]
    track_data = None
    thumb_path = None
    success = False

    async def progress_hook(percent):
        await update_progress_bar(status_msg, percent, last_update, status_prefix="")

    try:
        async with download_lock:
            try:
                await status_msg.edit_text("⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜ 0%")
            except Exception:
                pass

            track_data = await download_soundcloud_track(url, progress_callback=progress_hook)
            file_path = track_data['file_path']

            try:
                await status_msg.edit_text("⚙️ Обрабатываю аудио-файл...")
            except Exception:
                pass

            thumbnail_url = track_data.get('thumbnail_url')
            if thumbnail_url:
                try:
                    thumb_path = file_path + ".jpg"
                    async with httpx.AsyncClient() as client:
                        response = await client.get(thumbnail_url)
                        if response.status_code == 200:
                            with open(thumb_path, "wb") as f:
                                f.write(response.content)
                            process_thumbnail(thumb_path)
                except Exception as thumb_err:
                    print(f"Не удалось создать обложку: {thumb_err}")
                    thumb_path = None

            tg_thumb = FSInputFile(thumb_path) if thumb_path and os.path.exists(thumb_path) else None

            try:
                await status_msg.edit_text("📥 Отправляю аудио-файл")
            except Exception:
                pass

            if os.path.exists(file_path):
                file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
                if file_size_mb > 49.5:
                    raise ValueError(f"Файл слишком большой: {file_size_mb:.1f} MB")

                await message.answer_audio(
                    audio=FSInputFile(file_path),
                    title=track_data['title'],
                    performer=track_data['artist'],
                    duration=track_data['duration'],
                    thumbnail=tg_thumb
                )
            else:
                raise FileNotFoundError("Файл трека не найден на диске!")

            success = True

    except ValueError as val_err:
        print(f"Превышен лимит размера: {val_err}")
        error_size_text = "📁 Ошибка: Файл весит более 50 МБ.\nTelegram не позволяет отправлять такие тяжёлые треки."
        try:
            await status_msg.edit_text(text=error_size_text, reply_markup=get_cancel_menu())
            user_menus[user_id] = status_msg.message_id
        except Exception:
            err_msg = await message.answer(text=error_size_text, reply_markup=get_cancel_menu())
            user_menus[user_id] = err_msg.message_id

    except Exception as e:
        print(f"Ошибка при обработке ссылки: {e}")
        error_download_text = "🙈 Не удалось скачать этот аудио-файл. Возможно, он скрыт или удален."
        try:
            await status_msg.edit_text(text=error_download_text, reply_markup=get_cancel_menu())
            user_menus[user_id] = status_msg.message_id
        except Exception:
            err_msg = await message.answer(text=error_download_text, reply_markup=get_cancel_menu())
            user_menus[user_id] = err_msg.message_id

    finally:
        # Гарантированно уменьшаем счетчик активных задач, даже если упали в ошибку
        active_downloads = max(0, active_downloads - 1)

        if track_data and 'file_path' in track_data:
            if os.path.exists(track_data['file_path']):
                try:
                    os.remove(track_data['file_path'])
                except Exception:
                    pass
        if thumb_path and os.path.exists(thumb_path):
            try:
                os.remove(thumb_path)
            except Exception:
                pass

        if success and status_msg:
            try:
                await status_msg.delete()
            except Exception:
                pass

        # Меню вызывается только если это БЫЛА ПОСЛЕДНЯЯ задача и ВСЕ загрузки прошли УСПЕШНО
        if active_downloads == 0 and success:
            old_menu_id = user_menus.get(user_id)
            if old_menu_id:
                try:
                    await bot.delete_message(chat_id=message.chat.id, message_id=old_menu_id)
                except Exception:
                    pass

            final_menu = await message.answer(
                text="☁️ Cloudly Bot 2.5\n\nОтправь мне ссылку из SoundCloud в чат",
                reply_markup=get_main_menu()
            )
            user_menus[user_id] = final_menu.message_id


@dp.message()
async def echo_all(message: types.Message):
    old_menu_id = user_menus.get(message.from_user.id)
    if old_menu_id:
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=old_menu_id)
        except Exception:
            pass

    new_menu = await message.answer(
        "🤖 Чтобы я начал работу, отправь мне ссылку из SoundCloud.",
        reply_markup=get_main_menu()
    )
    user_menus[message.from_user.id] = new_menu.message_id


# =====================================================================
# 6. СЕРВЕРНАЯ ИНФРАСТРУКТУРА И ЗАПУСК
# =====================================================================

async def handle_ping(request):
    return web.Response(text="Bot is running!")


async def main():
    print("Bot successfully started in direct download mode!")

    app = web.Application()
    app.router.add_get("/", handle_ping)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 10000)
    asyncio.create_task(site.start())

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())