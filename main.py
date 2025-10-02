# main.py (fixed & PTB v20+ ready)
import os
import sys
import logging
import uuid
import aiosqlite
from datetime import datetime
from random import sample
import re
from typing import Optional, Dict, Any

# Telegram imports
import telegram
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, InputFile
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

# ---------------- Configuration & Setup ----------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME")  # without @
try:
    ADMIN_IDS = [int(i.strip()) for i in os.getenv("ADMIN_IDS", "").split(",") if i.strip().isdigit()]
except Exception:
    ADMIN_IDS = []

WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 8080))

if not TELEGRAM_BOT_TOKEN or not BOT_USERNAME or not WEBHOOK_URL:
    logging.error("CRITICAL: Essential environment variables (TELEGRAM_BOT_TOKEN, BOT_USERNAME, WEBHOOK_URL) are missing.")
    sys.exit(1)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Conversation states
SELECT_CHANNEL, GET_IMAGE_URL, GET_DETAILS = range(3)
BROADCAST_MESSAGE = 99

DB_FILE = "advanced_giveaway_bot.db"
GIVEAWAY_CREATION_DATA: Dict[int, Dict[str, Any]] = {}
DEFAULT_GIVEAWAY_IMAGE = "https://envs.sh/GhJ.jpg/IMG20250925634.jpg"

URL_REGEX = re.compile(
    r"^(?:http|ftp)s?://"
    r"(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+(?:[A-Z]{2,6}\.?|[A-Z0-9-]{2,}\.?)|"
    r"localhost|"
    r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})"
    r"(?::\d+)?"
    r"(?:/?|[/?]\S+)$",
    re.IGNORECASE,
)


# ---------------- Helper: PTB version check ----------------
def ensure_ptb_v20_or_exit():
    v = getattr(telegram, "__version__", "0")
    try:
        major = int(str(v).split(".")[0])
    except Exception:
        major = 0
    if major < 20:
        logger.critical(
            "Installed python-telegram-bot is outdated (version %s). This bot requires PTB v20+. "
            "Please update your environment with: pip install --upgrade 'python-telegram-bot>=20.3' "
            "and redeploy.", v
        )
        # Provide a clear exit message (Render logs will show this)
        print("ERROR: python-telegram-bot version must be >= 20. Installed:", v, file=sys.stderr)
        sys.exit(1)


# ---------------- Database (async) ----------------
async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS giveaways (
                giveaway_id TEXT PRIMARY KEY,
                channel_id TEXT NOT NULL,
                creator_id INTEGER NOT NULL,
                image_url TEXT,
                start_time DATETIME DEFAULT CURRENT_TIMESTAMP,
                is_active INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        await db.execute(
            """
            CREATE TABLE IF NOT EXISTS participants (
                giveaway_id TEXT,
                user_id INTEGER,
                username TEXT,
                full_name TEXT,
                participation_time DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (giveaway_id, user_id)
            )
            """
        )
        await db.commit()
    logger.info("Database initialized successfully.")


async def save_giveaway(giveaway_id: str, channel_id: str, creator_id: int, image_url: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO giveaways (giveaway_id, channel_id, creator_id, image_url) VALUES (?, ?, ?, ?)",
            (giveaway_id, channel_id, creator_id, image_url),
        )
        await db.commit()


async def get_giveaway_by_id(giveaway_id: str) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute(
            "SELECT channel_id, is_active, image_url FROM giveaways WHERE giveaway_id = ?", (giveaway_id,)
        )
        row = await cursor.fetchone()
        if row:
            return {"channel_id": row[0], "is_active": bool(row[1]), "image_url": row[2]}
        return None


async def log_participant(giveaway_id: str, user_id: int, username: str, full_name: str) -> bool:
    async with aiosqlite.connect(DB_FILE) as db:
        try:
            await db.execute(
                "INSERT INTO participants (giveaway_id, user_id, username, full_name) VALUES (?, ?, ?, ?)",
                (giveaway_id, user_id, username, full_name),
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def get_top_participants(giveaway_id: str, limit: int = 10):
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute(
            """
            SELECT full_name, username, user_id, participation_time
            FROM participants
            WHERE giveaway_id = ?
            ORDER BY participation_time DESC
            LIMIT ?
            """,
            (giveaway_id, limit),
        )
        return await cursor.fetchall()


async def get_all_active_giveaways():
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute(
            "SELECT giveaway_id, channel_id, start_time FROM giveaways WHERE is_active = 1 ORDER BY start_time DESC"
        )
        return await cursor.fetchall()


async def get_all_participants_for_giveaway(giveaway_id: str):
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute(
            "SELECT user_id, username, full_name FROM participants WHERE giveaway_id = ?", (giveaway_id,)
        )
        rows = await cursor.fetchall()
        return [{"user_id": r[0], "username": r[1], "full_name": r[2]} for r in rows]


async def close_giveaway_db(giveaway_id: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("UPDATE giveaways SET is_active = 0 WHERE giveaway_id = ?", (giveaway_id,))
        await db.commit()


async def select_random_winners(giveaway_id: str, count: int = 1):
    participants = await get_all_participants_for_giveaway(giveaway_id)
    if not participants:
        return []
    count = min(count, len(participants))
    return sample(participants, count)


async def get_entries_of_user(user_id: int):
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute(
            "SELECT giveaway_id, participation_time FROM participants WHERE user_id = ? ORDER BY participation_time DESC", (user_id,)
        )
        return await cursor.fetchall()


async def count_total_participants() -> int:
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("SELECT COUNT(*) FROM participants")
        row = await cursor.fetchone()
        return row[0] if row else 0


# ---------------- Utilities & Formatters ----------------
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def format_participant_details(user: Dict[str, Any]) -> str:
    full_name = user.get("full_name", "N/A")
    username = user.get("username", "N/A")
    user_id = user.get("id", "N/A")
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return (
        f"<b>[⚡] NEW VOTE-ENTRY [⚡]</b>\n\n"
        f"► 👤 USER: <a href='tg://user?id={user_id}'>{full_name}</a>\n"
        f"► 🆔 USER-ID: <code>{user_id}</code>\n"
        f"► 📛 USERNAME: @{username}\n"
        f"► 🕰️ TIME: <i>{timestamp}</i>\n\n"
        f"<b>» VOTE:</b> Click the REACTION BUTTON on this post!\n"
        f"CREATED BY @{BOT_USERNAME}"
    )


async def check_bot_admin_status(bot_instance, channel_id: str) -> bool:
    try:
        channel_id = int(channel_id)
        me = await bot_instance.get_me()
        bot_id = me.id
        member = await bot_instance.get_chat_member(channel_id, bot_id)
        status = getattr(member, "status", "")
        can_post = getattr(member, "can_post_messages", True)
        return (status in ["administrator", "creator"]) and can_post
    except TelegramError as e:
        logger.error("Error checking admin status in %s: %s", channel_id, e)
        return False
    except Exception as e:
        logger.error("Unexpected error checking admin status: %s", e)
        return False


async def check_user_membership(bot_instance, channel_id: str, user_id: int) -> bool:
    try:
        channel_id = int(channel_id)
        member = await bot_instance.get_chat_member(channel_id, user_id)
        return member.status in ["member", "administrator", "creator"]
    except TelegramError:
        return False
    except Exception:
        return False


# ---------------- Error Handler ----------------
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)
    chat_id = None
    if getattr(update, "effective_chat", None):
        chat_id = update.effective_chat.id
    elif getattr(update, "callback_query", None) and update.callback_query.message:
        chat_id = update.callback_query.message.chat_id
    else:
        return
    if chat_id and chat_id > 0:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text="⚠️ An unexpected error occurred. The bot has logged the issue. Please try again later.",
                parse_mode=ParseMode.MARKDOWN,
            )
        except Exception as e:
            logger.error("Failed to send error message back to user %s: %s", chat_id, e)


# ---------------- Command & Conversation Handlers ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # deep link handling
    if context.args:
        giveaway_id = context.args[0]
        await handle_deep_link_participation(update, context, giveaway_id)
        return

    keyboard = [
        [InlineKeyboardButton("➕ Add Bot To Channel", url=f"https://t.me/{BOT_USERNAME}?startgroup=start")],
        [InlineKeyboardButton("📚 Tutorial", url="https://youtube.com/your_bot_tutorial"),
         InlineKeyboardButton("❓ Support", url="https://t.me/your_support_group")],
    ]
    await update.effective_message.reply_photo(
        photo=DEFAULT_GIVEAWAY_IMAGE,
        caption=(
            f"🚀 <b>Welcome to @{BOT_USERNAME}: The Ultimate Vote Bot!</b>\n\n"
            "<i>Automate vote-based giveaways & content contests in your Telegram channels.</i>\n\n"
            "<b>» How to Get Started:</b>\n"
            "• Admins use /giveaway to launch a new vote-poll.\n"
            "• Use /help to see all features."
        ),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = (
        "📚 *BOT COMMANDS & USAGE*\n\n"
        "*» ADMIN COMMANDS (Admin Only):*\n"
        "• /giveaway - Start the multi-step process to create a new Vote-Poll.\n"
        "• /active_polls - List currently running polls.\n"
        "• /close_poll_<ID> - Close poll & select winner(s).\n"
        "• /broadcast <ID> - Send message to poll participants.\n\n"
        "*» USER COMMANDS:*\n"
        "• /start - Main menu\n"
        "• /help - This message"
    )
    await update.effective_message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)


async def start_giveaway(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("❌ You must be an administrator to create a Vote-Poll.", parse_mode=ParseMode.HTML)
        return ConversationHandler.END
    user_id = update.effective_user.id
    GIVEAWAY_CREATION_DATA[user_id] = {}
    await update.effective_message.reply_text(
        "🎁 <b>STEP 1/3: Channel Selection</b>\n\n"
        "Please forward a message from the channel or send the channel @username or -100id.",
        parse_mode=ParseMode.HTML,
    )
    return SELECT_CHANNEL


async def handle_channel_share(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    msg = update.effective_message
    channel = None

    if msg is None:
        await update.effective_message.reply_text("❌ Please forward a channel message or send channel @username / -100id.")
        return SELECT_CHANNEL

    # If forwarded from chat
    if getattr(msg, "forward_from_chat", None) and getattr(msg.forward_from_chat, "type", "") in ["channel", "supergroup"]:
        channel = msg.forward_from_chat
    elif getattr(msg, "text", None):
        text = msg.text.strip()
        if re.match(r"^@[A-Za-z0-9_]+$|^-\d+$", text):
            try:
                channel = await context.bot.get_chat(text)
            except TelegramError as e:
                logger.warning("get_chat failed for %s: %s", text, e)
                channel = None

    if not channel:
        await msg.reply_text("❌ Invalid channel. Forward channel message or send @username / -100id.", parse_mode=ParseMode.MARKDOWN)
        return SELECT_CHANNEL

    channel_id = str(channel.id)
    channel_title = getattr(channel, "title", "Untitled Channel")
    channel_username = getattr(channel, "username", None)

    status_msg = await msg.reply_text(f"⏳ Verifying admin status for {channel_title}...", parse_mode=ParseMode.HTML)
    if not await check_bot_admin_status(context.bot, channel_id):
        await status_msg.edit_text("❌ ADMIN CHECK FAILED! Add bot as admin with Post Messages permission.", parse_mode=ParseMode.HTML)
        return SELECT_CHANNEL

    await status_msg.edit_text("✅ Admin status verified.", parse_mode=ParseMode.HTML)

    giveaway_id = str(uuid.uuid4()).replace("-", "")[:10]
    GIVEAWAY_CREATION_DATA[user_id] = {
        "channel_id": channel_id,
        "channel_title": channel_title,
        "channel_username": channel_username,
        "giveaway_id": giveaway_id,
    }

    await msg.reply_text(
        f"🖼 STEP 2/3: Send public HTTPS image URL (or type SKIP to use default).\nExample: {DEFAULT_GIVEAWAY_IMAGE}",
        parse_mode=ParseMode.HTML,
    )
    return GET_IMAGE_URL


async def get_image_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    text = update.effective_message.text.strip() if update.effective_message and getattr(update.effective_message, "text", None) else ""

    if text.upper() == "SKIP":
        GIVEAWAY_CREATION_DATA[user_id]["image_url"] = DEFAULT_GIVEAWAY_IMAGE
    else:
        if not URL_REGEX.match(text):
            await update.effective_message.reply_text("❌ Invalid URL. Send full public URL starting with http/https or type SKIP.")
            return GET_IMAGE_URL
        GIVEAWAY_CREATION_DATA[user_id]["image_url"] = text

    await update.effective_message.reply_text("✅ Image saved. STEP 3/3: Type LAUNCH to publish the poll.", parse_mode=ParseMode.MARKDOWN)
    return GET_DETAILS


async def handle_details_and_publish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    data = GIVEAWAY_CREATION_DATA.get(user_id)
    if not data:
        await update.effective_message.reply_text("Session expired. Start /giveaway again.")
        return ConversationHandler.END

    if not update.effective_message or not getattr(update.effective_message, "text", None) or update.effective_message.text.strip().upper() != "LAUNCH":
        await update.effective_message.reply_text("Please type LAUNCH to proceed.", parse_mode=ParseMode.MARKDOWN)
        return GET_DETAILS

    giveaway_id = data["giveaway_id"]
    channel_id = data["channel_id"]
    image_url = data["image_url"]
    channel_title = data["channel_title"]
    channel_username = data.get("channel_username")

    await save_giveaway(giveaway_id, channel_id, update.effective_user.id, image_url)
    participation_link = f"https://t.me/{BOT_USERNAME}?start={giveaway_id}"
    channel_url = f"https://t.me/{channel_username}" if channel_username else "https://t.me/telegram"

    keyboard = [
        [InlineKeyboardButton("✨ Channel Link", url=channel_url), InlineKeyboardButton("🏆 View Top 10", callback_data=f"show_top10|{giveaway_id}")],
        [InlineKeyboardButton("🛑 CLOSE POLL & SELECT WINNER(S)", callback_data=f"close_poll|{giveaway_id}")],
    ]

    caption = (
        f"✅ <b>VOTE-POLL CREATED!</b>\n\n"
        f"Channel: <b>{channel_title}</b>\n"
        f"Poll ID: <code>{giveaway_id}</code>\n\n"
        f"Participation Link:\n<code>{participation_link}</code>\n\n"
        f"<i>Participants must be subscribers to log entry.</i>"
    )

    await update.effective_message.reply_photo(photo=image_url, caption=caption, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.HTML)
    GIVEAWAY_CREATION_DATA.pop(user_id, None)
    return ConversationHandler.END


async def cancel_giveaway(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    GIVEAWAY_CREATION_DATA.pop(user_id, None)
    await update.effective_message.reply_text("🛑 Giveaway creation cancelled.", parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END


# ---------------- Deep Link Participation ----------------
async def handle_deep_link_participation(update: Update, context: ContextTypes.DEFAULT_TYPE, giveaway_id: str) -> None:
    user = update.effective_user
    giveaway_data = await get_giveaway_by_id(giveaway_id)
    if not giveaway_data or not giveaway_data.get("is_active", False):
        await update.effective_message.reply_text("❌ This vote-poll has ended or is invalid. Please contact the channel admin.")
        return

    channel_id = giveaway_data["channel_id"]
    image_url = giveaway_data.get("image_url")
    try:
        channel_info = await context.bot.get_chat(channel_id)
        channel_link = f"https://t.me/{channel_info.username}" if getattr(channel_info, "username", None) else "https://t.me/telegram"
        channel_name = getattr(channel_info, "title", "Channel")
    except TelegramError:
        channel_link = f"Channel ID: <code>{channel_id}</code>"
        channel_name = "Unknown Channel"

    is_subscriber = await check_user_membership(context.bot, channel_id, user.id)
    if not is_subscriber:
        caption_text = (
            f"⚠️ <b>PARTICIPATION DENIED!</b>\n\n"
            f"To join the <b>'{channel_name}'</b> poll, you must be a <b>subscriber</b>.\n"
            f"Please Join Channel, then click the link again."
        )
        await update.effective_message.reply_photo(
            photo=image_url,
            caption=caption_text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🚀 Join Channel", url=channel_link)], [InlineKeyboardButton("✅ I have Joined, Try Again", url=f"https://t.me/{BOT_USERNAME}?start={giveaway_id}")]]
            ),
        )
        return

    # Log participant
    user_full_name = user.full_name
    user_username = user.username if user.username else f"id{user.id}"
    success = await log_participant(giveaway_id, user.id, user_username, user_full_name)

    if success:
        participant_message = format_participant_details({"full_name": user_full_name, "username": user_username, "id": user.id})
        try:
            await context.bot.send_message(chat_id=channel_id, text=participant_message, parse_mode=ParseMode.HTML)
            await update.effective_message.reply_text(
                f"🎉 <b>CONGRATULATIONS!</b>\n\nYou are now a registered participant for the <b>'{channel_name}'</b> vote-poll (ID: <code>{giveaway_id}</code>).",
                parse_mode=ParseMode.HTML,
            )
        except Exception as e:
            logger.error("Failed to post to channel %s: %s", channel_id, e)
            await update.effective_message.reply_text("❌ Participation logged, but failed to post details to the channel. Check bot permissions (Post Messages).")
    else:
        await update.effective_message.reply_text("💡 You have already participated in this poll.", parse_mode=ParseMode.MARKDOWN)


# ---------------- Admin Features ----------------
async def show_top_participants(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    if query.message and query.message.chat_id < 0 and not is_admin(query.from_user.id):
        await query.answer("You can only view this list in a private chat with the bot.", show_alert=True)
        return

    await query.answer("Fetching top 10 recent participants...")
    try:
        giveaway_id = query.data.split("|", 1)[1]
    except Exception:
        await query.message.reply_text("❌ Invalid query data.")
        return

    participants_data = await get_top_participants(giveaway_id, limit=10)

    if not participants_data:
        text = f"🏆 <b>TOP 10 RECENT PARTICIPANTS (Poll ID: {giveaway_id})</b>\n\nNo participants registered yet."
    else:
        lines = [f"🏆 <b>TOP 10 RECENT PARTICIPANTS (Poll ID: {giveaway_id})</b>\n"]
        for i, (full_name, username, user_id, participation_time) in enumerate(participants_data):
            try:
                dt_obj = datetime.strptime(participation_time.split(".")[0], "%Y-%m-%d %H:%M:%S")
                time_str = dt_obj.strftime("%H:%M:%S")
            except Exception:
                time_str = "Unknown Time"
            lines.append(f"<b>{i+1}.</b> <a href='tg://user?id={user_id}'>{full_name}</a> (@{username}) — <i>{time_str}</i>")
        text = "\n".join(lines)

    try:
        await context.bot.send_message(chat_id=query.message.chat_id, text=text, parse_mode=ParseMode.HTML)
    except TelegramError as e:
        logger.error("Failed to send top list: %s", e)
        await query.message.reply_text("❌ Failed to send the list. Check bot permissions.")


async def active_polls(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return
    active_giveaways = await get_all_active_giveaways()
    if not active_giveaways:
        await update.effective_message.reply_text("⭐ No active Vote-Polls found! Use /giveaway to start one.", parse_mode=ParseMode.MARKDOWN)
        return

    lines = ["✨ <b>ACTIVE VOTE-POLLS:</b> ✨\n"]
    for i, (giveaway_id, channel_id, start_time) in enumerate(active_giveaways):
        try:
            channel_info = await context.bot.get_chat(channel_id)
            channel_name = getattr(channel_info, "title", f"ID:{channel_id}")
        except TelegramError:
            channel_name = f"ID: {channel_id}"
        lines.append(f"<b>{i+1}. {channel_name}</b>")
        lines.append(f"   ID: <code>{giveaway_id}</code>")
        lines.append(f"   Start: <i>{start_time.split('.')[0]}</i>")
        lines.append(f"   Close: <code>/close_poll_{giveaway_id}</code>\n")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def close_poll_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    giveaway_id = None
    source_chat = update.effective_chat

    if update.callback_query:
        query = update.callback_query
        await query.answer("Closing poll and selecting winner(s)...")
        try:
            giveaway_id = query.data.split("|", 1)[1]
        except Exception:
            await query.message.reply_text("Invalid query.")
            return
        source_chat = query.message.chat
    elif update.effective_message and getattr(update.effective_message, "text", None):
        m = update.effective_message.text
        match = re.search(r"^/close_poll_([a-zA-Z0-9]+)$", m)
        if match:
            giveaway_id = match.group(1)
        else:
            await update.effective_message.reply_text("❌ Invalid command format. Use /close_poll_<ID>.", parse_mode=ParseMode.MARKDOWN)
            return

    if not giveaway_id:
        return

    giveaway_data = await get_giveaway_by_id(giveaway_id)
    if not giveaway_data or not giveaway_data.get("is_active", False):
        await context.bot.send_message(source_chat.id, f"❌ Poll <code>{giveaway_id}</code> is already closed or does not exist.", parse_mode=ParseMode.HTML)
        return

    winners = await select_random_winners(giveaway_id, count=1)
    total_participants = len(await get_all_participants_for_giveaway(giveaway_id))
    await close_giveaway_db(giveaway_id)

    channel_id = giveaway_data["channel_id"]
    lines = [f"🛑 <b>GIVEAWAY CLOSED!</b>\n\nVote-Poll ID <code>{giveaway_id}</code> is now closed.", f"Total Entries: <b>{total_participants}</b>\n"]
    if winners:
        for i, w in enumerate(winners):
            lines.append(f"<b>{i+1}.</b> <a href='tg://user?id={w['user_id']}'>{w['full_name']}</a> (@{w['username']})")
    else:
        lines.append("⚠️ No participants found.")
    announcement = "\n".join(lines)

    try:
        await context.bot.send_message(chat_id=channel_id, text=announcement, parse_mode=ParseMode.HTML)
    except TelegramError as e:
        logger.warning("Failed to notify channel %s about poll closure: %s", channel_id, e)

    await context.bot.send_message(source_chat.id, f"✅ Poll <code>{giveaway_id}</code> successfully CLOSED. Winner(s) announced in channel.", parse_mode=ParseMode.HTML)


# ---------------- Broadcast Flow ----------------
async def start_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
    if not context.args:
        await update.effective_message.reply_text("❌ Please specify the giveaway ID: /broadcast GIVEAWAY_ID", parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END
    giveaway_id = context.args[0]
    gd = await get_giveaway_by_id(giveaway_id)
    if not gd:
        await update.effective_message.reply_text(f"❌ Poll ID <code>{giveaway_id}</code> not found.", parse_mode=ParseMode.HTML)
        return ConversationHandler.END
    context.user_data["broadcast_id"] = giveaway_id
    await update.effective_message.reply_text(
        f"📣 BROADCAST MODE ACTIVATED for Poll ID: <code>{giveaway_id}</code>\n\nSend the message (text/photo/video/animation) to broadcast to all participants.",
        parse_mode=ParseMode.HTML,
    )
    return BROADCAST_MESSAGE


async def perform_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    giveaway_id = context.user_data.pop("broadcast_id", None)
    if not giveaway_id:
        await update.effective_message.reply_text("❌ Broadcast session expired.")
        return ConversationHandler.END

    participants = [p["user_id"] for p in await get_all_participants_for_giveaway(giveaway_id)]
    total_users = len(participants)
    success_count = 0

    text_payload = getattr(update.effective_message, "text", None)
    caption_payload = getattr(update.effective_message, "caption", None)

    await update.effective_message.reply_text(f"🚀 Starting broadcast to {total_users} participants...", parse_mode=ParseMode.HTML)

    for uid in participants:
        try:
            if getattr(update.effective_message, "photo", None):
                await context.bot.send_photo(chat_id=uid, photo=update.effective_message.photo[-1].file_id, caption=caption_payload or "", parse_mode=ParseMode.HTML)
            elif getattr(update.effective_message, "video", None):
                await context.bot.send_video(chat_id=uid, video=update.effective_message.video.file_id, caption=caption_payload or "", parse_mode=ParseMode.HTML)
            elif getattr(update.effective_message, "animation", None):
                await context.bot.send_animation(chat_id=uid, animation=update.effective_message.animation.file_id, caption=caption_payload or "", parse_mode=ParseMode.HTML)
            elif text_payload:
                await context.bot.send_message(chat_id=uid, text=text_payload, parse_mode=ParseMode.HTML)
            success_count += 1
        except TelegramError as e:
            logger.warning("Broadcast failed for %s: %s", uid, e)
        except Exception as e:
            logger.warning("Unexpected broadcast error for %s: %s", uid, e)

    await update.effective_message.reply_text(f"✅ BROADCAST COMPLETE! Sent to {success_count} / {total_users}.")
    return ConversationHandler.END


async def cancel_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.effective_message.reply_text("Broadcast cancelled.")
    return ConversationHandler.END


# ---------------- New admin helpers (optional) ----------------
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("❌ Admins only.")
        return
    active = await get_all_active_giveaways()
    total_participants = await count_total_participants()
    await update.effective_message.reply_text(f"📊 Active giveaways: {len(active)}\n📥 Total participants: {total_participants}")


async def my_entries(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = await get_entries_of_user(update.effective_user.id)
    if not rows:
        await update.effective_message.reply_text("You have no entries yet.")
        return
    lines = ["📋 Your recent entries:"]
    for gid, t in rows:
        lines.append(f"- {gid} at {t}")
    await update.effective_message.reply_text("\n".join(lines))


async def export_participants(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.effective_message.reply_text("❌ Admins only.")
        return
    if not context.args:
        await update.effective_message.reply_text("Usage: /export_participants <GIVEAWAY_ID>")
        return
    giveaway_id = context.args[0]
    rows = await get_all_participants_for_giveaway(giveaway_id)
    if not rows:
        await update.effective_message.reply_text("No participants or invalid giveaway id.")
        return
    import io
    buf = io.StringIO()
    buf.write("user_id,username,full_name\n")
    for p in rows:
        uid = p["user_id"]
        uname = p["username"] or ""
        fname = (p["full_name"] or "").replace(",", " ")
        buf.write(f"{uid},{uname},{fname}\n")
    bio = io.BytesIO(buf.getvalue().encode())
    bio.name = f"participants_{giveaway_id}.csv"
    await update.effective_message.reply_document(document=InputFile(bio, filename=bio.name), caption=f"Participants for {giveaway_id}")


# ---------------- Application init & run ----------------
async def post_init(application: Application):
    await init_db()
    logger.info("Post-init done.")


def main() -> None:
    # ensure PTB version is correct (helps avoid cryptic Updater errors)
    ensure_ptb_v20_or_exit()

    url_path = TELEGRAM_BOT_TOKEN
    webhook_url = f"{WEBHOOK_URL.rstrip('/')}/{url_path}"

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    # Conversation handlers
    giveaway_conv = ConversationHandler(
        entry_points=[CommandHandler("giveaway", start_giveaway)],
        states={
            SELECT_CHANNEL: [MessageHandler(filters.ChatType.PRIVATE & filters.ALL, handle_channel_share)],
            GET_IMAGE_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_image_url)],
            GET_DETAILS: [MessageHandler(filters.TEXT & filters.Regex(r"^(LAUNCH|launch)$"), handle_details_and_publish)],
        },
        fallbacks=[CommandHandler("cancel", cancel_giveaway)],
        allow_reentry=True,
    )

    broadcast_conv = ConversationHandler(
        entry_points=[CommandHandler("broadcast", start_broadcast)],
        states={BROADCAST_MESSAGE: [MessageHandler(filters.ALL & ~filters.COMMAND, perform_broadcast)]},
        fallbacks=[CommandHandler("cancel", cancel_broadcast)],
        allow_reentry=True,
    )

    # Core handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("active_polls", active_polls))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("my_entries", my_entries))
    application.add_handler(CommandHandler("export_participants", export_participants))

    application.add_handler(giveaway_conv)
    application.add_handler(broadcast_conv)

    # CallbackQuery handlers
    application.add_handler(CallbackQueryHandler(close_poll_handler, pattern=r"^close_poll\|"))
    application.add_handler(CallbackQueryHandler(show_top_participants, pattern=r"^show_top10\|"))

    # Direct text handler for /close_poll_<id>
    application.add_handler(MessageHandler(filters.Regex(r"^/close_poll_[a-zA-Z0-9]+$"), close_poll_handler))

    # Error handler
    application.add_error_handler(error_handler)

    logger.info("Starting webhook on port %s at /%s", PORT, url_path)
    application.run_webhook(listen="0.0.0.0", port=PORT, url_path=url_path, webhook_url=webhook_url)


if __name__ == "__main__":
    main()
