# /// script
# requires-python = ">=3.11"
# dependencies = ["python-telegram-bot>=22", "yt-dlp[default]"]
# ///
"""Download chat video links and reply with playable Telegram videos."""

import asyncio
from contextlib import suppress
from hashlib import sha1
import json
import logging
import os
from pathlib import Path
import re
import shutil
import signal
import sys
import tempfile
from urllib.parse import urlsplit

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, InlineQueryResultArticle,
    InlineQueryResultCachedVideo, InputMediaVideo, InputTextMessageContent, Message, Update,
)
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import (
    Application, CallbackQueryHandler, ChosenInlineResultHandler, CommandHandler,
    ContextTypes, InlineQueryHandler, MessageHandler, filters,
)


logging.basicConfig(
    level=logging.INFO, stream=sys.stdout,
    format="%(asctime)s %(levelname)s %(message)s",
)
# HTTP request logs can include the bot token in the URL.
logging.getLogger("httpx").setLevel(logging.WARNING)
LOG = logging.getLogger(__name__)
LIMIT = 50 * 1024 * 1024
DOWNLOAD_SLOT = asyncio.Semaphore(1)
ALLOWED_USERS: set[int] = set()
VIDEO_CACHE: dict[str, tuple[str, str]] = {}
CACHE_PATH = Path(__file__).resolve().with_name("cache.json")


def key(url: str) -> str:
    return sha1(url.encode()).hexdigest()[:10]


def save_cache() -> None:
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=CACHE_PATH.parent,
                                         prefix=".cache-", suffix=".tmp", delete=False) as file:
            temp = Path(file.name)
            json.dump(VIDEO_CACHE, file, ensure_ascii=False)
        os.replace(temp, CACHE_PATH)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def load_cache() -> None:
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or any(
            not isinstance(value, list) or len(value) != 2
            or not all(isinstance(item, str) for item in value)
            for value in data.values()
        ):
            raise ValueError("Invalid video cache.")
    except (OSError, ValueError):
        return
    VIDEO_CACHE.clear()
    VIDEO_CACHE.update((url, tuple(value)) for url, value in list(data.items())[-200:])
    if len(data) > 200:
        save_cache()


def load_env() -> None:
    path = Path(__file__).resolve().with_name(".env")
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.removeprefix("export ").partition("=")
        if sep:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            os.environ.setdefault(key.strip(), value)


async def authorized(update: Update) -> bool:
    user = update.effective_user
    if not user or not update.effective_message:
        return False
    if not ALLOWED_USERS:
        await update.effective_message.reply_text(
            f"Your user ID: {user.id}. Put it in ALLOWED_USERS."
        )
        return False
    return user.id in ALLOWED_USERS


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await authorized(update):
        await update.effective_message.reply_text(
            "Paste a video link here, then tap “Send to a chat”. "
            f"Or type @{context.bot.username} in any chat to pick a recent video."
        )


async def run_process(args: list[str], timeout: int) -> tuple[int, bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except BaseException:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.communicate()
        raise
    return process.returncode, stdout, stderr


async def download(url: str, directory: Path, resolution: int) -> tuple[Path, dict]:
    args = [
        "yt-dlp", "--ignore-config", "--no-playlist", "--no-simulate",
        "-S", f"res:{resolution},vcodec:h264,acodec:aac",
        "--merge-output-format", "mp4", "--remux-video", "mp4",
        "--postprocessor-args", "ffmpeg:-movflags +faststart",
        "--no-progress", "--print-json",
        "-o", str(directory / "video.%(ext)s"), "--", url,
    ]
    hostname = (urlsplit(url).hostname or "").lower()
    cookies = os.environ.get("REDDIT_COOKIES", "").strip()
    if (hostname in ("reddit.com", "redd.it") or hostname.endswith((".reddit.com", ".redd.it"))) \
            and cookies and Path(cookies).is_file():
        args[-2:-2] = ["--cookies", cookies]
    for attempt in range(1, 4):
        code, stdout, stderr = await run_process(args, 300)
        if not code:
            break
        lines = stderr.decode(errors="replace").strip().splitlines()
        LOG.warning("Download attempt %d/3 failed for %s:\n%s", attempt, url,
                    "\n".join(lines[-5:]))
        if attempt == 3:
            error = next((line for line in reversed(lines) if line.startswith("ERROR")),
                         lines[-1] if lines else "yt-dlp failed without an error message.")
            raise RuntimeError(error[:300])
        shutil.rmtree(directory)
        directory.mkdir()
        await asyncio.sleep(3)

    info = {}
    for line in stdout.decode(errors="replace").splitlines():
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict):
            info = candidate

    # The JSON filename can precede remuxing; prefer the final MP4 on disk.
    candidates = list(directory.glob("*.mp4"))
    candidates.extend(Path(item["filepath"]) for item in info.get("requested_downloads", [])
                      if item.get("filepath"))
    if info.get("filename"):
        candidates.append(Path(info["filename"]))
    for path in candidates:
        if path.is_file() and path.resolve().parent == directory.resolve():
            return path, info
    raise RuntimeError("yt-dlp produced no video file.")


async def metadata(path: Path, info: dict) -> dict:
    if not info.get("width") or not info.get("height"):
        code, stdout, stderr = await run_process([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height:format=duration", "-of", "json", str(path),
        ], 30)
        if code:
            raise RuntimeError("Could not probe video dimensions.")
        probe = json.loads(stdout)
        streams = probe.get("streams", [])
        if streams:
            info.update({key: streams[0][key] for key in ("width", "height")
                         if streams[0].get(key)})
        if not info.get("duration"):
            info["duration"] = probe.get("format", {}).get("duration")
    return {key: int(float(info[key])) for key in ("width", "height", "duration")
            if info.get(key) is not None}


async def fetch(url: str, temp_dir: Path) -> tuple[Path, dict, dict]:
    for resolution in (1080, 720):
        directory = temp_dir / str(resolution)
        directory.mkdir()
        path, info = await download(url, directory, resolution)
        size = path.stat().st_size
        if size <= LIMIT:
            return path, info, await metadata(path, info)
        # Free the oversized first attempt before downloading again.
        path.unlink()
    raise RuntimeError(f"Video is still too large: {size / 1024 / 1024:.1f} MB (limit 50 MB).")


def extract_urls(text: str) -> list[str]:
    return [url.rstrip(".,;!?\"'") for url in re.findall(r"https?://[^\s<>]+", text)]


def cache_video(url: str, message: Message, title: str) -> str:
    media = message.video or message.animation or message.document
    if media is None:
        raise RuntimeError("Telegram returned no video file ID.")
    VIDEO_CACHE.pop(url, None)
    VIDEO_CACHE[url] = (media.file_id, title)
    while len(VIDEO_CACHE) > 200:
        del VIDEO_CACHE[next(iter(VIDEO_CACHE))]
    save_cache()
    return media.file_id


async def upload_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    while True:
        with suppress(TelegramError):
            await context.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_VIDEO)
        await asyncio.sleep(4)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await authorized(update):
        return
    for url in extract_urls(update.effective_message.text or ""):
        LOG.info("Video request: %s", url)
        async with DOWNLOAD_SLOT:
            action = asyncio.create_task(upload_action(update, context))
            try:
                markup = InlineKeyboardMarkup([[
                    InlineKeyboardButton("Send to a chat", switch_inline_query=key(url)),
                ]])
                if url in VIDEO_CACHE:
                    file_id, title = VIDEO_CACHE[url]
                    try:
                        message = await context.bot.send_video(
                            chat_id=update.effective_chat.id,
                            reply_to_message_id=update.effective_message.message_id,
                            video=file_id, supports_streaming=True,
                            caption=title, reply_markup=markup,
                        )
                    except TelegramError:
                        del VIDEO_CACHE[url]
                        save_cache()
                    else:
                        cache_video(url, message, title)
                        continue
                with tempfile.TemporaryDirectory(prefix="ytbot-") as temp:
                    path, info, dimensions = await fetch(url, Path(temp))
                    title = str(info.get("title") or "Video")[:1024]
                    with path.open("rb") as video:
                        message = await context.bot.send_video(
                            chat_id=update.effective_chat.id,
                            reply_to_message_id=update.effective_message.message_id,
                            video=video, supports_streaming=True,
                            caption=title, reply_markup=markup,
                            read_timeout=120, write_timeout=120, **dimensions,
                        )
                    cache_video(url, message, title)
                    LOG.info("Sent video (%d bytes)", path.stat().st_size)
            except asyncio.TimeoutError:
                LOG.warning("Video subprocess timed out")
                await update.effective_message.reply_text("Video processing timed out.")
            except (RuntimeError, OSError, ValueError, TelegramError) as exc:
                LOG.warning("Video failed: %s", exc)
                await update.effective_message.reply_text(str(exc)[-300:])
            finally:
                action.cancel()
                with suppress(asyncio.CancelledError):
                    await action


async def handle_inline(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.inline_query
    urls = extract_urls(query.query)
    if query.from_user.id not in ALLOWED_USERS:
        await query.answer([], cache_time=0, is_personal=True)
        return
    if not urls:
        text = query.query.strip()
        entries = list(reversed(VIDEO_CACHE.items()))
        match = next((entry for entry in entries if key(entry[0]) == text), None)
        entries = [match] if match else [
            entry for entry in entries if text.casefold() in entry[1][1].casefold()
        ]
        results = [InlineQueryResultCachedVideo(
            id=key(url), video_file_id=file_id, title=title, caption=title,
        ) for url, (file_id, title) in entries[:10]]
        await query.answer(results, cache_time=0, is_personal=True)
        return
    url = urls[0]
    LOG.info("Inline video request: %s", url)
    result_id = sha1(url.encode()).hexdigest()
    if url in VIDEO_CACHE:
        file_id, title = VIDEO_CACHE[url]
        result = InlineQueryResultCachedVideo(
            id=result_id, video_file_id=file_id, title=title, caption=title,
        )
    else:
        result = InlineQueryResultArticle(
            id=result_id, title="Send video", description=url,
            input_message_content=InputTextMessageContent("⏳ Downloading video…"),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("⏳ Downloading…", callback_data="noop"),
            ]]),
        )
    await query.answer([result], cache_time=0, is_personal=True)


async def handle_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chosen = update.chosen_inline_result
    if not chosen.inline_message_id or chosen.from_user.id not in ALLOWED_USERS:
        return
    urls = extract_urls(chosen.query)
    if not urls:
        return
    url = urls[0]
    LOG.info("Chosen inline video request: %s", url)
    helper = None
    try:
        async with DOWNLOAD_SLOT:
            with tempfile.TemporaryDirectory(prefix="ytbot-") as temp:
                path, info, dimensions = await fetch(url, Path(temp))
                title = str(info.get("title") or "Video")[:1024]
                with path.open("rb") as video:
                    helper = await context.bot.send_video(
                        chat_id=chosen.from_user.id, disable_notification=True,
                        video=video, supports_streaming=True, caption=title,
                        read_timeout=120, write_timeout=120, **dimensions,
                    )
                file_id = cache_video(url, helper, title)
                await context.bot.edit_message_media(
                    inline_message_id=chosen.inline_message_id,
                    media=InputMediaVideo(media=file_id, caption=title,
                                          supports_streaming=True, **dimensions),
                )
    except Exception as exc:
        error = "Video processing timed out." if isinstance(exc, asyncio.TimeoutError) else str(exc)[-300:]
        LOG.warning("Inline video failed for %s: %s", url, exc)
        with suppress(TelegramError):
            await context.bot.edit_message_text(
                inline_message_id=chosen.inline_message_id, text="❌ " + error,
            )
    finally:
        if helper is not None:
            with suppress(TelegramError):
                await context.bot.delete_message(chat_id=chosen.from_user.id,
                                                 message_id=helper.message_id)


async def handle_noop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()


def main() -> None:
    global ALLOWED_USERS
    load_env()
    load_cache()
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("BOT_TOKEN is required (environment or .env).")
    try:
        ALLOWED_USERS = {int(value.strip()) for value in os.environ.get("ALLOWED_USERS", "").split(",")
                         if value.strip()}
    except ValueError:
        raise SystemExit("ALLOWED_USERS must contain comma-separated numeric user IDs.") from None
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text, block=False))
    app.add_handler(InlineQueryHandler(handle_inline))
    app.add_handler(ChosenInlineResultHandler(handle_chosen, block=False))
    app.add_handler(CallbackQueryHandler(handle_noop, pattern="^noop$"))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
