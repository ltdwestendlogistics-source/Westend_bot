import os
import json
import logging
import io
from datetime import datetime

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
BOT_TOKEN       = os.environ["BOT_TOKEN"]
DRIVE_FOLDER_ID = os.environ["DRIVE_FOLDER_ID"]
GOOGLE_CREDS    = os.environ["GOOGLE_CREDENTIALS"]   # full JSON string

DRIVERS = [
    "ჯემალი", "მამუკა", "ამირანი", "ალეკო", "მანუჩარი",
    "აკაკი",  "ზური",   "ვიტო",   "თემო",  "ზია",
]

TRUCKS = [
    "WE125ST / WE125S",
    "WE120ST / WE120S",
    "WE545ST / WE545S",
    "TT030AA / BB420G",
    "MP040TT / BB408G",
    "WE989ST / WE989S",
    "WE126ST / WE126S",
    "BX906XX",
    "CC332DD",
    "WE450ST / WE450S",
]

# Conversation states
DRIVER, TRUCK, DOCUMENTS, PHOTOS = range(4)

# ── Google Drive helpers ───────────────────────────────────────────────────────
def drive_service():
    info  = json.loads(GOOGLE_CREDS)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive"]
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def get_or_create_folder(svc, name, parent_id):
    q = (
        f"name='{name}' and '{parent_id}' in parents "
        f"and mimeType='application/vnd.google-apps.folder' and trashed=false"
    )
    res = svc.files().list(q=q, fields="files(id)").execute()
    if res["files"]:
        return res["files"][0]["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
    return svc.files().create(body=meta, fields="id").execute()["id"]


def create_folder(svc, name, parent_id):
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
    return svc.files().create(body=meta, fields="id").execute()["id"]


def upload_bytes(svc, data: bytes, filename: str, mimetype: str, folder_id: str):
    meta  = {"name": filename, "parents": [folder_id]}
    media = MediaIoBaseUpload(io.BytesIO(data), mimetype=mimetype)
    svc.files().create(body=meta, media_body=media, fields="id").execute()


# ── Keyboards ──────────────────────────────────────────────────────────────────
def driver_keyboard():
    rows = [[d] for d in DRIVERS]
    return ReplyKeyboardMarkup(rows, one_time_keyboard=True, resize_keyboard=True)


def truck_keyboard():
    rows = [[t] for t in TRUCKS]
    return ReplyKeyboardMarkup(rows, one_time_keyboard=True, resize_keyboard=True)


def docs_keyboard():
    rows = [["✅ CMR", "✅ Invoice"], ["✅ CMR + Invoice", "❌ არცერთი"]]
    return ReplyKeyboardMarkup(rows, one_time_keyboard=True, resize_keyboard=True)


# ── Handlers ───────────────────────────────────────────────────────────────────
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data.clear()
    await update.message.reply_text(
        "🚛 *დატვირთვის ფორმა*\n\nაირჩიე შენი სახელი:",
        reply_markup=driver_keyboard(),
        parse_mode="Markdown",
    )
    return DRIVER


async def driver_chosen(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    name = update.message.text.strip()
    if name not in DRIVERS:
        await update.message.reply_text("⚠️ სიიდან აირჩიე სახელი.", reply_markup=driver_keyboard())
        return DRIVER
    ctx.user_data["driver"] = name
    await update.message.reply_text(
        f"✅ {name}\n\nაირჩიე მანქანა:", reply_markup=truck_keyboard()
    )
    return TRUCK


async def truck_chosen(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    truck = update.message.text.strip()
    if truck not in TRUCKS:
        await update.message.reply_text("⚠️ სიიდან აირჩიე მანქანა.", reply_markup=truck_keyboard())
        return TRUCK
    ctx.user_data["truck"] = truck
    await update.message.reply_text(
        f"✅ {truck}\n\nრომელი დოკუმენტები მიიღე?", reply_markup=docs_keyboard()
    )
    return DOCUMENTS


async def docs_chosen(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data["documents"] = update.message.text.strip()
    ctx.user_data["photos"]    = []
    await update.message.reply_text(
        "📸 გამოგზავნე დატვირთვის ფოტოები.\n\nყველა ფოტოს შემდეგ გამოგზავნე /done",
        reply_markup=ReplyKeyboardRemove(),
    )
    return PHOTOS


async def photo_received(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    file_id = update.message.photo[-1].file_id
    ctx.user_data["photos"].append(file_id)
    n = len(ctx.user_data["photos"])
    await update.message.reply_text(f"📸 {n} ფოტო მიღებულია. მეტი გამოგზავნე ან /done")
    return PHOTOS


async def done(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    photos = ctx.user_data.get("photos", [])
    if not photos:
        await update.message.reply_text("⚠️ მინიმუმ ერთი ფოტო გამოგზავნე.")
        return PHOTOS

    await update.message.reply_text("⏳ ვინახავ Google Drive-ში...")

    driver    = ctx.user_data["driver"]
    truck     = ctx.user_data["truck"]
    documents = ctx.user_data["documents"]
    now       = datetime.now()
    date_str  = now.strftime("%Y-%m-%d")
    time_str  = now.strftime("%H:%M")
    folder_name = f"{date_str}_{truck.replace(' / ', '_')}"

    try:
        svc = drive_service()

        # Loadings → Driver → Date_Truck
        driver_folder  = get_or_create_folder(svc, driver, DRIVE_FOLDER_ID)
        session_folder = create_folder(svc, folder_name, driver_folder)

        # info.txt
        info = (
            f"მძღოლი:      {driver}\n"
            f"მანქანა:     {truck}\n"
            f"თარიღი:      {date_str}\n"
            f"დრო:         {time_str}\n"
            f"დოკუმენტები: {documents}\n"
            f"ფოტო:        {len(photos)}\n"
        )
        upload_bytes(svc, info.encode("utf-8"), "info.txt", "text/plain", session_folder)

        # photos
        for i, fid in enumerate(photos, 1):
            tg_file    = await ctx.bot.get_file(fid)
            photo_data = await tg_file.download_as_bytearray()
            upload_bytes(svc, bytes(photo_data), f"photo_{i:02d}.jpg", "image/jpeg", session_folder)

        await update.message.reply_text(
            f"✅ *შენახულია!*\n\n"
            f"👤 {driver}\n"
            f"🚛 {truck}\n"
            f"📄 {documents}\n"
            f"📸 {len(photos)} ფოტო\n"
            f"📁 Drive → Loadings / {driver} / {folder_name}",
            parse_mode="Markdown",
        )
    except Exception as e:
        logger.exception("Drive upload failed")
        await update.message.reply_text(f"❌ შეცდომა Drive-ზე: {e}")

    ctx.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data.clear()
    await update.message.reply_text("❌ გაუქმებულია.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            DRIVER:    [MessageHandler(filters.TEXT & ~filters.COMMAND, driver_chosen)],
            TRUCK:     [MessageHandler(filters.TEXT & ~filters.COMMAND, truck_chosen)],
            DOCUMENTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, docs_chosen)],
            PHOTOS: [
                MessageHandler(filters.PHOTO, photo_received),
                CommandHandler("done", done),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    logger.info("Bot started.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
