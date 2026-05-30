import asyncio
import os
import random
import yt_dlp
import imageio_ffmpeg


def extract_link_info(url: str) -> dict:
    """Делает супер-быстрый запрос к SoundCloud для определения типа ссылки
    и извлечения общих метаданных альбома/трека без ухода в бан.
    """
    ydl_opts = {
        'extract_flat': 'in_playlist',
        'quiet': True,
        'nocheckcertificate': True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

        if 'entries' in info:
            entries = list(info['entries'])
            playlist_thumb = info.get('thumbnail') or info.get('thumbnails', [{}])[0].get('url')

            return {
                'is_playlist': True,
                'playlist_title': info.get('title', 'Unknown Playlist'),
                'playlist_uploader': info.get('uploader', info.get('user', {}).get('username', 'Unknown Artist')),
                'playlist_thumbnail': playlist_thumb,
                'entries': entries,
                'total_tracks': len(entries)
            }
        else:
            return {
                'is_playlist': False,
                'title': info.get('title', 'Unknown Title'),
                'artist': info.get('uploader', 'Unknown Artist')
            }


async def download_soundcloud_track(url: str, progress_callback=None, output_dir: str = "downloads") -> dict:
    """Скачивает ОДИН конкретный трек и перехватывает его данные на лету."""
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    random_id = f"track_{random.randint(100000, 999999)}"
    outtmpl_path = os.path.join(output_dir, f"{random_id}.%(ext)s")

    main_loop = asyncio.get_event_loop()

    def ydl_progress_hook(d):
        if d['status'] == 'downloading' and progress_callback:
            downloaded = d.get('downloaded_bytes', 0)
            total = d.get('total_bytes') or d.get('total_bytes_estimate')

            # Извлекаем данные трека прямо во время загрузки!
            info_dict = d.get('info_dict', {})
            fetched_title = info_dict.get('title')
            fetched_thumb = info_dict.get('thumbnail') or info_dict.get('thumbnails', [{}])[0].get('url')

            if total:
                percent = (downloaded / total) * 100
                # Передаем в коллбэк не только процент, но и имя с обложкой
                asyncio.run_coroutine_threadsafe(
                    progress_callback(percent, fetched_title, fetched_thumb),
                    main_loop
                )

    # Автоматически получаем путь к бинарнику ffmpeg, встроенному в библиотеку
    auto_ffmpeg_path = imageio_ffmpeg.get_ffmpeg_exe()

    ydl_opts = {
        'format': 'bestaudio/best',
        'outtmpl': outtmpl_path,
        'noplaylist': True,
        'quiet': True,
        'progress_hooks': [ydl_progress_hook],
        'nocheckcertificate': True,
        'ffmpeg_location': auto_ffmpeg_path,  # Точный автономный путь
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                          '(KHTML, Skin/8.0) Chrome/122.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Referer': 'https://soundcloud.com/',
        },
        'postprocessors': [
            {
                'key': 'FFmpegExtractAudio',
                'preferredcodec': 'mp3',
                'preferredquality': 'auto',
            },
            {
                'key': 'FFmpegMetadata',
            }
        ],
    }

    def extract():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filepath = os.path.join(output_dir, f"{random_id}.mp3")
            thumbnail_url = info.get('thumbnail') or info.get('thumbnails', [{}])[0].get('url')

            return {
                'file_path': filepath,
                'title': info.get('title', 'Unknown Title'),
                'artist': info.get('uploader', 'Unknown Artist'),
                'duration': int(info.get('duration', 0)),
                'thumbnail_url': thumbnail_url
            }

    return await main_loop.run_in_executor(None, extract)