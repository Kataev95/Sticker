import asyncio
import json
import logging
import os
import re
import tempfile
import uuid
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, Message, CallbackQuery
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.environ["BOT_TOKEN"]
REMOVE_BG = os.getenv("REMOVE_BG", "true").lower() in ("true", "1", "yes")
BG_MODEL = os.getenv("BG_MODEL", "u2netp")
MAX_INPUT = int(os.getenv("MAX_INPUT_MB", "18")) * 1048576
ALLOWED = {int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()}
router = Router()
pending = {}
locks = {}
username = ""
bg_lock = asyncio.Lock()
bg_session = None


def allowed(uid):
    return not ALLOWED or uid in ALLOWED


def buttons():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📦 Создать стикерпак", callback_data="new")],
        [InlineKeyboardButton(text="➕ Добавить в стикерпак", callback_data="add")]
    ])


async def run(*args, timeout=120):
    process = await asyncio.create_subprocess_exec(
        *map(str, args), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.communicate()
        raise ValueError("Превышено время обработки")
    if process.returncode:
        raise ValueError(stderr.decode(errors="replace")[-450:])
    return stdout.decode(errors="replace")


async def probe(path):
    data = await run("ffprobe", "-v", "error", "-show_entries",
                     "format=duration:stream=codec_name,codec_type,width,height",
                     "-of", "json", path)
    return json.loads(data)


def remove_frames(frames):
    global bg_session
    from PIL import Image
    from rembg import new_session, remove
    if bg_session is None:
        bg_session = new_session(BG_MODEL)
    for file in sorted(frames.glob("*.png")):
        with Image.open(file) as image:
            rgba = remove(image.convert("RGBA"), session=bg_session)
            rgba.save(file)


async def convert(src, dst):
    info = await probe(src)
    if not any(s.get("codec_type") == "video" for s in info.get("streams", [])):
        raise ValueError("Не найден видеопоток")
    duration = float(info.get("format", {}).get("duration") or 2.95)
    duration = max(0.1, min(duration, 2.95))
    source = src
    if REMOVE_BG:
        frames = dst.parent / "frames"
        frames.mkdir()
        await run("ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src,
                  "-t", str(duration), "-vf",
                  "fps=12,scale=512:512:force_original_aspect_ratio=decrease:flags=lanczos",
                  "-frames:v", "36", frames / "%04d.png")
        if not list(frames.glob("*.png")):
            raise ValueError("Не удалось извлечь кадры")
        async with bg_lock:
            await asyncio.to_thread(remove_frames, frames)
        source = frames / "%04d.png"
    for fps, crf in [(24, 36), (24, 42), (20, 46), (16, 49),
                     (12, 53), (10, 57), (8, 60)]:
        args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
        if REMOVE_BG:
            args += ["-stream_loop", "-1", "-framerate", "12"]
        elif duration < 2.9:
            args += ["-stream_loop", "-1"]
        args += ["-i", str(source), "-t", "2.95", "-an",
                 "-vf", f"fps={fps},scale=512:512:force_original_aspect_ratio=decrease:flags=lanczos,"
                        "format=rgba,pad=512:512:(ow-iw)/2:(oh-ih)/2:color=black@0,format=yuva420p",
                 "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", str(crf),
                 "-deadline", "realtime", "-cpu-used", "5", "-row-mt", "1",
                 "-pix_fmt", "yuva420p", "-auto-alt-ref", "0", "-f", "webm", dst]
        await run(*args)
        if dst.stat().st_size <= 256 * 1024:
            meta = await probe(dst)
            video = next(s for s in meta["streams"] if s.get("codec_type") == "video")
            if video["codec_name"] == "vp9" and video["width"] == 512 and video["height"] == 512:
                return dst.stat().st_size
    raise ValueError("Не удалось сжать до 256 КБ. Попробуйте более простую GIF")


async def telegram_api(method, fields, file=None):
    import aiohttp
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=90)) as session:
        url = f"https://api.telegram.org/bot{TOKEN}/{method}"
        if file:
            form = aiohttp.FormData()
            for key, value in fields.items():
                form.add_field(key, json.dumps(value, ensure_ascii=False)
                               if isinstance(value, (list, dict)) else str(value))
            with open(file, "rb") as stream:
                form.add_field("sticker", stream, filename="sticker.webm",
                               content_type="video/webm")
                async with session.post(url, data=form) as response:
                    result = await response.json(content_type=None)
        else:
            async with session.post(url, json=fields) as response:
                result = await response.json(content_type=None)
    if not result.get("ok"):
        raise ValueError(result.get("description", "Ошибка Telegram API"))
    return result["result"]


@router.message(CommandStart())
async def start(message: Message):
    if allowed(message.from_user.id):
        await message.answer("🎬 Отправь GIF, MP4, MOV или WEBM. Я удалю фон, "
                             "сделаю WEBM VP9 и помогу создать стикерпак. /cancel — отмена.")


@router.message(Command("cancel"))
async def cancel(message: Message):
    pending.pop(message.from_user.id, None)
    await message.answer("Операция отменена")


@router.message(F.document | F.animation | F.video)
async def media(message: Message, bot: Bot):
    uid = message.from_user.id
    if not allowed(uid):
        return
    item = message.document or message.animation or message.video
    name = (getattr(item, "file_name", None) or
            ("clip.mp4" if message.video else "clip.gif")).lower()
    ext = Path(name).suffix
    if ext not in (".gif", ".mp4", ".mov", ".webm"):
        await message.answer("Поддерживаются GIF, MP4, MOV, WEBM")
        return
    if item.file_size and item.file_size > MAX_INPUT:
        await message.answer("Файл слишком большой")
        return
    lock = locks.setdefault(uid, asyncio.Lock())
    if lock.locked():
        await message.answer("⏳ Дождись завершения предыдущей обработки")
        return
    async with lock:
        status = await message.answer("⏳ Удаляю фон и конвертирую...")
        try:
            with tempfile.TemporaryDirectory(prefix="sticker_") as directory:
                src = Path(directory) / ("input" + ext)
                dst = Path(directory) / "sticker.webm"
                await bot.download(item, destination=src)
                if src.stat().st_size > MAX_INPUT:
                    raise ValueError("Превышен лимит входного файла")
                size = await convert(src, dst)
                await message.answer_document(FSInputFile(dst),
                                              caption=f"✅ WEBM VP9 · 512×512 · {size // 1024} КБ")
                uploaded = await telegram_api("uploadStickerFile",
                    {"user_id": uid, "sticker_format": "video"}, dst)
                pending[uid] = {"file_id": uploaded["file_id"]}
                await message.answer("Что дальше?", reply_markup=buttons())
            await status.delete()
        except Exception as error:
            logging.exception("Processing failed")
            await status.edit_text(f"❌ {str(error)[:600]}")


@router.callback_query(F.data.in_({"new", "add"}))
async def choose(callback: CallbackQuery):
    uid = callback.from_user.id
    if not allowed(uid) or uid not in pending:
        await callback.answer("Сначала отправь GIF", show_alert=True)
        return
    mode = callback.data
    pending[uid].update(mode=mode, stage="title" if mode == "new" else "pack")
    await callback.answer()
    await callback.message.answer("Название нового набора (до 64 символов):" if mode == "new"
                                  else "Короткое имя набора, созданного этим ботом:")


@router.message(F.text)
async def input_text(message: Message):
    uid = message.from_user.id
    if not allowed(uid):
        return
    state = pending.get(uid)
    if not state or "stage" not in state:
        await message.answer("Отправь GIF или /start")
        return
    value = message.text.strip()
    if state["stage"] == "title":
        if not 1 <= len(value) <= 64:
            await message.answer("Название должно содержать 1–64 символа")
            return
        state["title"] = value
        state["stage"] = "emoji"
        await message.answer("Пришли эмодзи, например 😂")
    elif state["stage"] == "pack":
        value = value.rsplit("/", 1)[-1]
        if not re.fullmatch(r"[A-Za-z0-9_]{5,128}", value):
            await message.answer("Некорректное имя набора")
            return
        state["pack"] = value
        state["stage"] = "emoji"
        await message.answer("Пришли эмодзи, например 😂")
    else:
        if not value or len(value) > 12:
            await message.answer("Отправь один короткий эмодзи")
            return
        sticker = {"sticker": state["file_id"], "format": "video", "emoji_list": [value]}
        try:
            if state["mode"] == "new":
                name = f"reactions_{uid}_{uuid.uuid4().hex[:8]}_by_{username}"
                await telegram_api("createNewStickerSet", {
                    "user_id": uid, "name": name, "title": state["title"],
                    "stickers": [sticker], "sticker_type": "regular"})
            else:
                name = state["pack"]
                if not name.endswith("_by_" + username):
                    raise ValueError("Можно редактировать только наборы этого бота")
                await telegram_api("addStickerToSet", {
                    "user_id": uid, "name": name, "sticker": sticker})
            pending.pop(uid, None)
            await message.answer(f"🎉 Стикерпак: https://t.me/addstickers/{name}")
        except Exception as error:
            logging.exception("Sticker API failed")
            await message.answer(f"❌ {str(error)[:450]}\nИсправь данные или /cancel")


async def main():
    global username
    logging.basicConfig(level=logging.INFO)
    bot = Bot(TOKEN)
    username = (await bot.get_me()).username
    dispatcher = Dispatcher()
    dispatcher.include_router(router)
    await bot.delete_webhook(drop_pending_updates=False)
    try:
        await dispatcher.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
