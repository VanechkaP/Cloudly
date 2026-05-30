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

from downloader import download_soundcloud_track, extract_link_info

# =====================================================================
# 1. ИНИЦИАЛИЗАЦИЯ БОТА И НАСТРОЙКИ
# =====================================================================

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")

if not TOKEN:
    raise ValueError("❌ Ошибка: Переменная BOT_TOKEN не найдена в файле .env!")

bot = Bot(token=TOKEN)
dp = Dispatcher()

user_cooldowns = {}
user_menus = {}

# Хранилище для неудавшихся треков: { user_id: { "playlist_title": ..., "playlist_artist": ..., "playlist_thumbnail": ..., "tracks": [...], "error_msg_ids": [...] } }
failed_downloads_store = {}

download_lock = asyncio.Lock()
active_downloads = 0


# =====================================================================
# 2. КЛАВИАТУРЫ И ИНТЕРФЕЙС (MARKUP)
# =====================================================================

def get_main_menu():
    buttons = [
        [
            InlineKeyboardButton(text="📝 Список изменений", callback_data="menu_changelog")
        ],
        [
            InlineKeyboardButton(text="ℹ️ FAQ", callback_data="menu_info"),
            InlineKeyboardButton(text="💬 Связь", callback_data="menu_donate")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_cancel_menu():
    buttons = [[InlineKeyboardButton(text="📱 Главное меню", callback_data="menu_cancel")]]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_retry_menu():
    buttons = [
        [InlineKeyboardButton(text="🔄 Повторить загрузку", callback_data="retry_failed")],
        [InlineKeyboardButton(text="📱 Главное меню", callback_data="menu_cancel")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


support_keyboard = InlineKeyboardMarkup(inline_keyboard=[
    [InlineKeyboardButton(text="💬 Написать создателю", url="tg://resolve?domain=trollzz1q")],
    [InlineKeyboardButton(text="📱 Главное меню", callback_data="menu_cancel")]
])


# =====================================================================
# 3. ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ (UTILITIES)
# =====================================================================

def process_thumbnail(image_path: str) -> bool:
    """Обрабатывает картинку. Возвращает True, если всё ок, и False, если файл битый."""
    try:
        if not os.path.exists(image_path) or os.path.getsize(image_path) == 0:
            return False

        with Image.open(image_path) as img:
            img.verify()  # Быстрая проверка структуры файла на валидность

        # Открываем заново для реальной обработки, так как verify() закрывает дескриптор
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
        return True
    except Exception as img_err:
        print(f"❌ Картинка битая или повреждена: {img_err}")
        return False


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


@dp.callback_query(F.data == "menu_changelog")
async def press_changelog(callback: types.CallbackQuery):
    changelog_text = (
        "🚀 *Список изменений* 🚀\n\n"
        "*2.5.3:*\n"
        "- Добавлена поддержка ссылок на альбомы и плейлисты.\n"
        "- Обновлён дизайн загрузки аудио.\n\n"
        "*2.5.2:*\n"
        "- Изменения в стабильности сервера.\n\n"
        "*2.5.1:*\n"
        "- Экстренный фикс отправки и сжатия аудио.\n\n"
        "*2.5:*\n"
        "- Глобальный редизайн меню.\n"
        "- Новая логика отправки аудио.\n"
        "- Повышена стабильность отправки аудио.\n\n"
        "*2.0:*\n"
        "- Добавлена полоса загрузки аудио.\n"
        "- Исправление известных багов."
    )

    user_id = callback.from_user.id
    user_menus[user_id] = callback.message.message_id

    try:
        await callback.message.edit_text(
            text=changelog_text,
            reply_markup=get_cancel_menu(),
            parse_mode="Markdown"
        )
    except Exception as e:
        print(f"Ошибка edit_text в чейнджлоге: {e}")
        try:
            err_msg = await callback.message.answer(
                text=changelog_text,
                reply_markup=get_cancel_menu(),
                parse_mode="Markdown"
            )
            user_menus[user_id] = err_msg.message_id
        except Exception as flood_err:
            print(f"Полная блокировка отправки: {flood_err}")

    await callback.answer()


@dp.callback_query(F.data == "menu_info")
async def press_info(callback: types.CallbackQuery):
    info_text = (
        "ℹ️ Информация ℹ️\n\n"
        "• Бот умеет скачивать треки и плейлисты из SoundCloud в формате MP3.\n\n"
        "• Лимит на размер одного файла: 50 МБ (ограничение Telegram).\n\n"
        "• Скачивание альбомов происходит поштучно в порядке очереди!"
    )
    user_menus[callback.from_user.id] = callback.message.message_id
    try:
        await callback.message.edit_text(text=info_text, reply_markup=get_cancel_menu())
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data == "menu_donate")
async def press_donate(callback: types.CallbackQuery):
    donate_text = (
        "✨ Поддержка проекта ✨\n\n"
        "Если тебе нравится бот и ты хочешь помочь с оплатой хостинга или предложить идею - нажми на кнопку ниже и напиши создателю проекта напрямую!"
    )
    user_menus[callback.from_user.id] = callback.message.message_id
    try:
        await callback.message.edit_text(text=donate_text, reply_markup=support_keyboard)
    except Exception:
        pass
    await callback.answer()


@dp.callback_query(F.data == "menu_cancel")
async def press_cancel(callback: types.CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id

    if user_id in failed_downloads_store:
        old_errors = failed_downloads_store[user_id].get("error_msg_ids", [])
        for msg_id in old_errors:
            try:
                await bot.delete_message(chat_id=callback.message.chat.id, message_id=msg_id)
            except Exception:
                pass
        failed_downloads_store.pop(user_id, None)

    user_menus[user_id] = callback.message.message_id
    try:
        await callback.message.edit_text(
            "☁️ Cloudly Bot 2.5\n\n"
            "⚡ Отправь мне ссылку из SoundCloud в чат",
            reply_markup=get_main_menu()
        )
    except Exception:
        pass


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

    active_downloads += 1

    old_menu_id = user_menus.get(user_id)
    if old_menu_id:
        try:
            await bot.delete_message(chat_id=message.chat.id, message_id=old_menu_id)
            user_menus[user_id] = None
        except Exception:
            pass

    status_prefix = "⏳ В очереди... \n" if download_lock.locked() else ""
    status_msg = await message.answer(f"{status_prefix}🔍 Анализирую ссылку...")

    main_success = False
    has_errors = False

    try:
        link_info = await asyncio.get_event_loop().run_in_executor(None, extract_link_info, url)

        async with download_lock:
            if link_info.get('is_playlist'):
                # === СЦЕНАРИЙ А: СКАЧИВАНИЕ ПЛЕЙЛИСТА / АЛЬБОМА ===
                playlist_title = link_info['playlist_title']
                playlist_artist = link_info['playlist_uploader']
                playlist_thumb = link_info.get('playlist_thumbnail')
                entries = link_info['entries']
                total_tracks = link_info['total_tracks']

                if total_tracks > 35:
                    raise ValueError(f"Плейлист слишком большой ({total_tracks} треков). Лимит бота — 35.")

                try:
                    await status_msg.delete()
                except Exception:
                    pass

                status_msg = await message.answer(
                    f"📂 Плейлист: {playlist_title}\nВсего треков: {total_tracks}\n\n🚀 Начинаю загрузку...")
                await asyncio.sleep(1.0)

                failed_tracks = []
                track_error_msg_ids = []
                successful_count = 0

                for index, entry in enumerate(entries, start=1):
                    track_url = entry.get('url') or entry.get('webpage_url')
                    if not track_url:
                        continue

                    initial_prefix = f"📂 Альбом: {playlist_title} — {playlist_artist}\n📥 Подключение к треку {index} из {total_tracks}...\n\n"

                    try:
                        await status_msg.delete()
                    except Exception:
                        pass

                    status_msg = await message.answer(f"{initial_prefix}⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜ 0%")

                    actual_title = [None]
                    actual_thumb = [None]
                    last_update = [0.0]

                    async def playlist_progress(percent, fetched_title=None, fetched_thumb=None):
                        if fetched_title:
                            actual_title[0] = fetched_title
                        if fetched_thumb:
                            actual_thumb[0] = fetched_thumb

                        current_title = actual_title[0] or entry.get('title') or f"Трек {index}"
                        dynamic_prefix = f"📂 Альбом: {playlist_title} — {playlist_artist}\n📥 Скачиваю {index} из {total_tracks}:\n└ *{current_title}*\n\n"
                        await update_progress_bar(status_msg, percent, last_update, status_prefix=dynamic_prefix)

                    track_data = None
                    thumb_path = None
                    try:
                        track_data = await download_soundcloud_track(track_url, progress_callback=playlist_progress)
                        file_path = track_data['file_path']

                        if track_data.get('title'):
                            actual_title[0] = track_data['title']

                        try:
                            await status_msg.edit_text(
                                f"⚙️ Обработка и отправка: *{actual_title[0] or f'Трек {index}'}*...")
                        except Exception:
                            pass

                        thumbnail_url = actual_thumb[0] or track_data.get('thumbnail_url') or playlist_thumb
                        is_thumb_ready = False

                        if thumbnail_url:
                            try:
                                thumb_path = file_path + ".jpg"
                                async with httpx.AsyncClient(timeout=10.0) as client:
                                    response = await client.get(thumbnail_url)
                                    if response.status_code == 200:
                                        with open(thumb_path, "wb") as f:
                                            f.write(response.content)
                                        is_thumb_ready = process_thumbnail(thumb_path)
                                    else:
                                        if playlist_thumb and thumbnail_url != playlist_thumb:
                                            resp_alt = await client.get(playlist_thumb)
                                            if resp_alt.status_code == 200:
                                                with open(thumb_path, "wb") as f:
                                                    f.write(resp_alt.content)
                                                is_thumb_ready = process_thumbnail(thumb_path)
                            except Exception:
                                is_thumb_ready = False

                        tg_thumb = FSInputFile(thumb_path) if (
                                    is_thumb_ready and thumb_path and os.path.exists(thumb_path)) else None

                        if os.path.exists(file_path):
                            file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
                            if file_size_mb <= 49.5:
                                await message.answer_audio(
                                    audio=FSInputFile(file_path),
                                    title=actual_title[0] or track_data['title'],
                                    performer=track_data['artist'],
                                    duration=track_data['duration'],
                                    thumbnail=tg_thumb
                                )
                                successful_count += 1
                    except Exception as single_track_err:
                        print(f"Ошибка скачивания трека {index}: {single_track_err}")

                        # Определяем имя трека и автора для форматирования ошибки
                        failed_title = actual_title[0] or entry.get('title') or f"Трек {index}"
                        failed_artist = (track_data.get('artist') if track_data else None) or entry.get(
                            'uploader') or playlist_artist

                        # Сохраняем во внутреннюю структуру
                        failed_tracks.append({"url": track_url, "title": failed_title, "artist": failed_artist})

                        # Форматируем ошибку в виде: трек - автор
                        error_report = f"⚠️ Не удалось скачать трек {index}: *{failed_title} - {failed_artist}* (Пропущен)"
                        err_msg = await message.answer(error_report, parse_mode="Markdown")
                        track_error_msg_ids.append(err_msg.message_id)
                    finally:
                        if track_data and os.path.exists(track_data['file_path']):
                            try:
                                os.remove(track_data['file_path'])
                            except Exception:
                                pass
                        if thumb_path and os.path.exists(thumb_path):
                            try:
                                os.remove(thumb_path)
                            except Exception:
                                pass

                if failed_tracks:
                    has_errors = True
                    failed_downloads_store[user_id] = {
                        "playlist_title": playlist_title,
                        "playlist_artist": playlist_artist,
                        "playlist_thumbnail": playlist_thumb,
                        "tracks": failed_tracks,
                        "error_msg_ids": track_error_msg_ids
                    }

                    try:
                        await status_msg.delete()
                    except Exception:
                        pass

                    report_menu = await message.answer(
                        text=f"📊 *Загрузка завершена!*\n\n✅ Успешно отправлено: {successful_count}\n⚠️ Пропущено из-за ошибок: {len(failed_tracks)}\n\n_Вы можете попробовать перекачать только неудавшиеся треки заново._",
                        reply_markup=get_retry_menu()
                    )
                    user_menus[user_id] = report_menu.message_id
                else:
                    main_success = True

            else:
                # === СЦЕНАРИЙ Б: СКАЧИВАНИЕ ОБЫЧНОГО СИНГЛА ===
                try:
                    await status_msg.edit_text("⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜ 0%")
                except Exception:
                    pass

                last_update = [0.0]

                async def single_progress(percent, fetched_title=None, fetched_thumb=None):
                    await update_progress_bar(status_msg, percent, last_update, status_prefix="")

                track_data = await download_soundcloud_track(url, progress_callback=single_progress)
                file_path = track_data['file_path']

                try:
                    await status_msg.edit_text("⚙️ Обрабатываю аудио-файл...")
                except Exception:
                    pass

                thumbnail_url = track_data.get('thumbnail_url')
                thumb_path = None
                is_thumb_ready = False

                if thumbnail_url:
                    try:
                        thumb_path = file_path + ".jpg"
                        async with httpx.AsyncClient(timeout=10.0) as client:
                            response = await client.get(thumbnail_url)
                            if response.status_code == 200:
                                with open(thumb_path, "wb") as f:
                                    f.write(response.content)
                                is_thumb_ready = process_thumbnail(thumb_path)
                    except Exception:
                        is_thumb_ready = False

                tg_thumb = FSInputFile(thumb_path) if (
                            is_thumb_ready and thumb_path and os.path.exists(thumb_path)) else None

                try:
                    await status_msg.edit_text("📥 Отправляю аудио-файл...")
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

                if thumb_path and os.path.exists(thumb_path):
                    try:
                        os.remove(thumb_path)
                    except Exception:
                        pass
                if os.path.exists(file_path):
                    try:
                        os.remove(file_path)
                    except Exception:
                        pass

                main_success = True

    except ValueError as val_err:
        print(f"Ошибка валидации: {val_err}")
        error_text = f"📁 Ошибка при обработке:\n{str(val_err)}"
        try:
            await status_msg.edit_text(text=error_text, reply_markup=get_cancel_menu())
            user_menus[user_id] = status_msg.message_id
        except Exception:
            err_msg = await message.answer(text=error_text, reply_markup=get_cancel_menu())
            user_menus[user_id] = err_msg.message_id

    except Exception as e:
        print(f"Критическая ошибка работы ссылки: {e}")
        error_download_text = "🙈 Не удалось обработать эту ссылку. Возможно, профиль скрыт, плейлист пуст или превышены лимиты Telegram."

        edited = False
        if 'status_msg' in locals() and status_msg:
            try:
                await status_msg.edit_text(text=error_download_text, reply_markup=get_cancel_menu())
                user_menus[user_id] = status_msg.message_id
                edited = True
            except Exception:
                pass

        if not edited:
            try:
                err_msg = await message.answer(text=error_download_text, reply_markup=get_cancel_menu())
                user_menus[user_id] = err_msg.message_id
            except Exception as flood_err:
                print(f"Полный блок отправки из-за флуда: {flood_err}")

    finally:
        active_downloads = max(0, active_downloads - 1)

        if main_success and not has_errors and status_msg:
            try:
                await status_msg.delete()
            except Exception:
                pass

        if active_downloads == 0 and main_success and not has_errors:
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


# =====================================================================
# 6. ХЭНДЛЕР ПОВТОРНОЙ ЗАГРУЗКИ (RETRY LOGIC)
# =====================================================================

@dp.callback_query(F.data == "retry_failed")
async def process_retry(callback: types.CallbackQuery):
    global active_downloads
    user_id = callback.from_user.id

    if user_id not in failed_downloads_store:
        await callback.answer("❌ Нет аудио для повторной загрузки или сессия устарела.", show_alert=True)
        return

    await callback.answer("🔄 Начинаю повторную попытку...")

    data = failed_downloads_store.pop(user_id)
    playlist_title = data["playlist_title"]
    playlist_artist = data["playlist_artist"]
    playlist_thumb = data["playlist_thumbnail"]
    tracks_to_retry = data["tracks"]
    old_error_msg_ids = data.get("error_msg_ids", [])
    total_tracks = len(tracks_to_retry)

    for msg_id in old_error_msg_ids:
        try:
            await bot.delete_message(chat_id=callback.message.chat.id, message_id=msg_id)
        except Exception:
            pass

    try:
        await callback.message.delete()
    except Exception:
        pass

    active_downloads += 1
    status_msg = await callback.message.answer("⏳ Подготовка к повторной загрузке...")

    has_errors = False
    failed_tracks = []
    track_error_msg_ids = []
    successful_count = 0

    try:
        async with download_lock:
            for index, entry in enumerate(tracks_to_retry, start=1):
                track_url = entry["url"]
                cached_title = entry["title"]
                cached_artist = entry.get("artist") or playlist_artist

                initial_prefix = f"🔄 Повтор [{playlist_title}]:\n📥 Подключение к треку {index} из {total_tracks}...\n\n"

                try:
                    await status_msg.delete()
                except Exception:
                    pass

                status_msg = await callback.message.answer(f"{initial_prefix}⬜⬜⬜⬜⬜⬜⬜⬜⬜⬜ 0%")

                actual_title = [cached_title]
                actual_thumb = [None]
                last_update = [0.0]

                async def retry_progress(percent, fetched_title=None, fetched_thumb=None):
                    if fetched_title:
                        actual_title[0] = fetched_title
                    if fetched_thumb:
                        actual_thumb[0] = fetched_thumb

                    current_title = actual_title[0] or cached_title
                    dynamic_prefix = f"🔄 Повтор [{playlist_title}]:\n📥 Скачиваю {index} из {total_tracks}:\n└ *{current_title}*\n\n"
                    await update_progress_bar(status_msg, percent, last_update, status_prefix=dynamic_prefix)

                track_data = None
                thumb_path = None
                try:
                    track_data = await download_soundcloud_track(track_url, progress_callback=retry_progress)
                    file_path = track_data['file_path']

                    if track_data.get('title'):
                        actual_title[0] = track_data['title']

                    try:
                        await status_msg.edit_text(f"⚙️ Обработка и отправка: *{actual_title[0]}*...")
                    except Exception:
                        pass

                    thumbnail_url = actual_thumb[0] or track_data.get('thumbnail_url') or playlist_thumb
                    is_thumb_ready = False

                    if thumbnail_url:
                        try:
                            thumb_path = file_path + ".jpg"
                            async with httpx.AsyncClient(timeout=10.0) as client:
                                response = await client.get(thumbnail_url)
                                if response.status_code == 200:
                                    with open(thumb_path, "wb") as f:
                                        f.write(response.content)
                                    is_thumb_ready = process_thumbnail(thumb_path)
                                else:
                                    if playlist_thumb and thumbnail_url != playlist_thumb:
                                        resp_alt = await client.get(playlist_thumb)
                                        if resp_alt.status_code == 200:
                                            with open(thumb_path, "wb") as f:
                                                f.write(resp_alt.content)
                                            is_thumb_ready = process_thumbnail(thumb_path)
                        except Exception:
                            is_thumb_ready = False

                    tg_thumb = FSInputFile(thumb_path) if (
                                is_thumb_ready and thumb_path and os.path.exists(thumb_path)) else None

                    if os.path.exists(file_path):
                        file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
                        if file_size_mb <= 49.5:
                            await callback.message.answer_audio(
                                audio=FSInputFile(file_path),
                                title=actual_title[0],
                                performer=track_data['artist'],
                                duration=track_data['duration'],
                                thumbnail=tg_thumb
                            )
                            successful_count += 1
                except Exception as retry_err:
                    print(f"Ошибка при повторном скачивании трека: {retry_err}")

                    failed_title = actual_title[0] or cached_title
                    failed_artist = (track_data.get('artist') if track_data else None) or cached_artist

                    failed_tracks.append({"url": track_url, "title": failed_title, "artist": failed_artist})

                    # Форматируем ошибку повтора в виде: трек - автор
                    err_msg = await callback.message.answer(
                        f"⚠️ Повторная попытка не удалась: *{failed_title} - {failed_artist}*",
                        parse_mode="Markdown"
                    )
                    track_error_msg_ids.append(err_msg.message_id)
                finally:
                    if track_data and os.path.exists(track_data['file_path']):
                        try:
                            os.remove(track_data['file_path'])
                        except Exception:
                            pass
                    if thumb_path and os.path.exists(thumb_path):
                        try:
                            os.remove(thumb_path)
                        except Exception:
                            pass

            if failed_tracks:
                has_errors = True
                failed_downloads_store[user_id] = {
                    "playlist_title": playlist_title,
                    "playlist_artist": playlist_artist,
                    "playlist_thumbnail": playlist_thumb,
                    "tracks": failed_tracks,
                    "error_msg_ids": track_error_msg_ids
                }
                try:
                    await status_msg.delete()
                except Exception:
                    pass

                report_menu = await callback.message.answer(
                    text=f"📊 *Повторная загрузка завершена!*\n\n✅ Успешно докачано: {successful_count}\n⚠️ Всё ещё с ошибками: {len(failed_tracks)}",
                    reply_markup=get_retry_menu()
                )
                user_menus[user_id] = report_menu.message_id
            else:
                try:
                    await status_msg.delete()
                except Exception:
                    pass

                final_menu = await callback.message.answer(
                    text="☁️ Cloudly Bot 2.5\n\nВсе аудио успешно докачаны!\nОтправь мне новую ссылку из SoundCloud в чат",
                    reply_markup=get_main_menu()
                )
                user_menus[user_id] = final_menu.message_id

    except Exception as global_retry_err:
        print(f"Критический сбой повтора: {global_retry_err}")
        err_msg = await callback.message.answer("❌ Сбой инфраструктуры при попытке повтора.",
                                                reply_markup=get_cancel_menu())
        user_menus[user_id] = err_msg.message_id
    finally:
        active_downloads = max(0, active_downloads - 1)


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
# 7. СЕРВЕРНАЯ ИНФРАСТРУКТУРА И ЗАПУСК
# =====================================================================

async def handle_ping(request):
    return web.Response(text="Bot is running!")


async def main():
    print("Bot started")

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