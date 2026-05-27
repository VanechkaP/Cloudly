import asyncio
import os
import time  # Нужен для замера времени между сообщениями
import httpx
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.types import FSInputFile
from PIL import Image
from dotenv import load_dotenv
from aiohttp import web

from downloader import download_soundcloud_track

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")

bot = Bot(token=TOKEN)
dp = Dispatcher()

# Словарь для защиты от флуда: {user_id: timestamp_последнего_сообщения}
user_cooldowns = {}


def process_thumbnail(image_path: str):
    """Обрезает картинку до квадрата и сжимает до 300x300 для идеального превью в TG"""
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


# --- ОБРАБОТКА КОМАНДЫ СТАРТ ---

@dp.message(Command("start"))
async def start_cmd(message: types.Message):
    await message.answer(
        f"Привет, {message.from_user.first_name}!\n"
        "Отправь мне ссылку на трек!"
    )


# --- ПРИЕМ ССЫЛКИ И ОТПРАВКА АУДИО (С ЗАЩИТОЙ) ---

@dp.message()
async def handle_link(message: types.Message):
    user_id = message.from_user.id
    current_time = time.time()

    # 🛑 ЗАЩИТА 1: Ограничение на длину текста (Защита от тяжелого спама)
    # Если текст длиннее 300 символов, мы его даже не обрабатываем
    if message.text and len(message.text) > 300:
        await message.answer("❌ Ошибка: Сообщение слишком длинное!")
        return

    # 🛑 ЗАЩИТА 2: Анти-флуд (Ограничение: 1 запрос в 3 секунды)
    if user_id in user_cooldowns:
        last_time = user_cooldowns[user_id]
        if current_time - last_time < 3:  # 3 секунды кулдауна
            await message.answer("⛔ Не спамь! Подожди пару секунд.")
            return

    # Запоминаем время текущего запроса пользователя
    user_cooldowns[user_id] = current_time

    url = message.text.strip() if message.text else ""

    if "soundcloud.com" not in url:
        await message.answer("🔃 Отправь мне корректную ссылку из SoundCloud.")
        return

    status_msg = await message.answer("⚡ Разбираю ссылку и скачиваю трек...")

    try:
        # 1. Скачиваем трек
        track_data = await download_soundcloud_track(url)
        audio_file = FSInputFile(track_data['file_path'])

        # 2. Безопасно скачиваем и обрабатываем обложку
        thumb_path = None
        thumbnail_url = track_data.get('thumbnail_url')

        if thumbnail_url:
            try:
                thumb_path = track_data['file_path'] + ".jpg"
                async with httpx.AsyncClient() as client:
                    response = await client.get(thumbnail_url)
                    if response.status_code == 200:
                        with open(thumb_path, "wb") as f:
                            f.write(response.content)

                        process_thumbnail(thumb_path)
            except Exception as thumb_err:
                print(f"Не удалось создать обложку: {thumb_err}")
                thumb_path = None

        # 3. Готовим превью к отправке
        tg_thumb = FSInputFile(thumb_path) if thumb_path and os.path.exists(thumb_path) else None

        # 4. Отправляем в Telegram
        await message.answer_audio(
            audio=audio_file,
            title=track_data['title'],
            performer=track_data['artist'],
            duration=track_data['duration'],
            thumbnail=tg_thumb
        )

        # Подчищаем файлы
        if os.path.exists(track_data['file_path']):
            os.remove(track_data['file_path'])
        if thumb_path and os.path.exists(thumb_path):
            os.remove(thumb_path)

        await status_msg.delete()

    except Exception as e:
        print(f"Ошибка при обработке ссылки: {e}")
        await status_msg.edit_text("🙈 Не удалось скачать этот трек. Возможно, он скрыт или удален.")


# --- ВЕБ-СЕРВЕР ДЛЯ ХОСТИНГА (RENDER PING) ---
async def handle_ping(request):
    return web.Response(text="Bot is running!")


# --- ЗАПУСК БОТА И ВЕБ-СЕРВЕРА ---
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