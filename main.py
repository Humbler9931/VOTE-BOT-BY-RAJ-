# main_advanced.py (Stylish & Feature-Rich PTB v20+ Vote Bot)
import os
import sys
import logging
import uuid
import aiosqlite
from datetime import datetime
from random import sample
import re
from typing import Optional, Dict, Any, List

# Telegram imports
import telegram
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode, ChatMemberStatus
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
from telegram.error import TelegramError

# --- Configuration & Setup (Same as before) ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME")
try:
    ADMIN_IDS = [int(i.strip()) for i in os.getenv("ADMIN_IDS", "").split(",") if i.strip().isdigit()]
except Exception:
    ADMIN_IDS = []

WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 8080))

if not TELEGRAM_BOT_TOKEN or not BOT_USERNAME or not WEBHOOK_URL:
    logging.error("CRITICAL: Essential environment variables missing.")
    sys.exit(1)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

# Conversation states
SELECT_CHANNEL, GET_IMAGE_URL, GET_DETAILS = range(3)
BROADCAST_MESSAGE = 99

DB_FILE = "advanced_giveaway_bot.db"
# Temporary storage for creation data (Stylish: Using Context.user_data for conversation flow)
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

# --- Database (Enhanced) ---
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
                is_active INTEGER NOT NULL DEFAULT 1,
                winner_count INTEGER DEFAULT 1 -- Stylish: Added winner_count
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
        # Ensure the new column exists if updating from old structure
        try:
            await db.execute("SELECT winner_count FROM giveaways LIMIT 1")
        except aiosqlite.OperationalError:
            await db.execute("ALTER TABLE giveaways ADD COLUMN winner_count INTEGER DEFAULT 1")

        await db.commit()
    logger.info("Database initialized successfully.")

# Save giveaway now includes winner_count
async def save_giveaway(giveaway_id: str, channel_id: str, creator_id: int, image_url: str, winner_count: int = 1):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO giveaways (giveaway_id, channel_id, creator_id, image_url, winner_count) VALUES (?, ?, ?, ?, ?)",
            (giveaway_id, channel_id, creator_id, image_url, winner_count),
        )
        await db.commit()

async def get_giveaway_by_id(giveaway_id: str) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute(
            "SELECT channel_id, is_active, image_url, winner_count FROM giveaways WHERE giveaway_id = ?", (giveaway_id,)
        )
        row = await cursor.fetchone()
        if row:
            return {"channel_id": row[0], "is_active": bool(row[1]), "image_url": row[2], "winner_count": row[3]}
        return None

# Rest of the DB functions (log_participant, get_all_participants_for_giveaway, etc.) remain mostly the same.
async def get_all_participants_for_giveaway(giveaway_id: str):
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute(
            "SELECT user_id, username, full_name FROM participants WHERE giveaway_id = ?", (giveaway_id,)
        )
        rows = await cursor.fetchall()
        return [{"user_id": r[0], "username": r[1], "full_name": r[2]} for r in rows]

async def select_random_winners(giveaway_id: str, count: int):
    participants = await get_all_participants_for_giveaway(giveaway_id)
    if not participants:
        return []
    count = min(count, len(participants))
    return sample(participants, count)

async def close_giveaway_db(giveaway_id: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("UPDATE giveaways SET is_active = 0 WHERE giveaway_id = ?", (giveaway_id,))
        await db.commit()
# --- Utilities & Checks (Enhanced) ---

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

async def check_bot_admin_status(bot_instance, channel_id: str) -> bool:
    try:
        channel_id = int(channel_id)
        me = await bot_instance.get_me()
        member = await bot_instance.get_chat_member(channel_id, me.id)
        # Check for administrative rights and ability to post
        return member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR] and member.can_post_messages
    except TelegramError:
        return False
    except Exception:
        return False

async def check_user_membership(bot_instance, channel_id: str, user_id: int) -> bool:
    try:
        channel_id = int(channel_id)
        member = await bot_instance.get_chat_member(channel_id, user_id)
        return member.status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]
    except TelegramError:
        return False
    except Exception:
        return False

# --- Core Handlers (Refined) ---

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args:
        giveaway_id = context.args[0]
        await handle_deep_link_participation(update, context, giveaway_id)
        return

    keyboard = [
        [InlineKeyboardButton("➕ Bot Ko Channel Me Jodein", url=f"https://t.me/{BOT_USERNAME}?startgroup=start")],
        [InlineKeyboardButton("🏆 Giveaway Shuru Karein", callback_data="start_giveaway_conv")],
        [InlineKeyboardButton("📚 Guide", url="https://telegra.ph/Your-Bot-Guide"),
         InlineKeyboardButton("❓ Support", url="https://t.me/your_support_group")],
    ]
    
    # Stylish: Using a clearer welcome message with HTML
    await update.effective_message.reply_html(
        caption=(
            f"🚀 <b>{BOT_USERNAME} Mein Aapka Swagat Hai!</b>\n\n"
            "<i>Yeh bot aapke Telegram channels ke liye Vote/Reaction based giveaways ko automate karta hai.</i>\n\n"
            "<b>» Admin Commands:</b> /giveaway, /active_polls, /stats\n"
            "<b>» User Command:</b> /my_entries"
        ),
        reply_markup=InlineKeyboardMarkup(keyboard),
    )

async def start_giveaway_conv_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the start of the giveaway conversation from an inline button."""
    query = update.callback_query
    await query.answer()
    return await start_giveaway(update, context) # Delegate to the main command handler

async def start_giveaway(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Initiates the giveaway creation flow."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        await update.effective_message.reply_html("❌ Aapko Vote-Poll banane ke liye **administrator** hona zaroori hai.")
        return ConversationHandler.END
    
    # Stylish: Using context.user_data for temporary session data
    context.user_data[user_id] = {} 
    
    await update.effective_message.reply_html(
        "🎁 <b>STEP 1/3: Channel Selection</b>\n\n"
        "Kripya channel se ek message **Forward** karein, ya channel ka **@username** ya **-100id** bhejein."
    )
    return SELECT_CHANNEL

async def get_image_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gets the image URL for the poll."""
    user_id = update.effective_user.id
    text = update.effective_message.text.strip() if update.effective_message.text else ""

    if text.upper() == "SKIP":
        context.user_data[user_id]["image_url"] = DEFAULT_GIVEAWAY_IMAGE
    else:
        if not URL_REGEX.match(text):
            await update.effective_message.reply_text("❌ Invalid URL. Pura public URL bhejein (http/https se shuru), ya **SKIP** type karein.")
            return GET_IMAGE_URL
        context.user_data[user_id]["image_url"] = text

    # Stylish: Introducing a new step to ask for winner count
    await update.effective_message.reply_html(
        "✅ Image saved. <b>STEP 3/4: Winner Count</b>\n\n"
        "Kripya winners ki sankhya (jaise: <code>1</code>, <code>3</code>, <code>5</code>) bhejein."
    )
    return GET_DETAILS

async def get_winner_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Gets the winner count and then publishes the poll."""
    user_id = update.effective_user.id
    data = context.user_data.get(user_id)
    
    if not data:
        await update.effective_message.reply_text("Session expired. Kripya /giveaway fir se shuru karein.")
        return ConversationHandler.END

    text = update.effective_message.text.strip() if update.effective_message.text else ""
    
    try:
        winner_count = int(text)
        if winner_count <= 0:
             raise ValueError
        data["winner_count"] = winner_count
    except ValueError:
        await update.effective_message.reply_text("❌ Kripya sahi winners ki sankhya (ek number) bhejein.")
        return GET_DETAILS

    
    giveaway_id = data["giveaway_id"]
    channel_id = data["channel_id"]
    image_url = data["image_url"]
    channel_title = data["channel_title"]
    channel_username = data.get("channel_username")

    # Stylish: Save winner_count in DB
    await save_giveaway(giveaway_id, channel_id, user_id, image_url, winner_count)
    participation_link = f"https://t.me/{BOT_USERNAME}?start={giveaway_id}"
    channel_url = f"https://t.me/{channel_username}" if channel_username else "https://t.me/telegram"

    keyboard = [
        [InlineKeyboardButton("✨ Channel Link", url=channel_url), InlineKeyboardButton("🏆 View Top 10", callback_data=f"show_top10|{giveaway_id}")],
        [InlineKeyboardButton(f"🛑 {winner_count} Winner Select Karein", callback_data=f"confirm_close_poll|{giveaway_id}")], # Stylish: New confirmation step
    ]

    caption = (
        f"✅ <b>VOTE-POLL SUCCESSFULLY CREATED!</b>\n\n"
        f"Channel: <b>{channel_title}</b>\n"
        f"Winners: <b>{winner_count}</b>\n"
        f"Poll ID: <code>{giveaway_id}</code>\n\n"
        f"Participation Link (Click to Copy):\n<code>{participation_link}</code>\n\n"
        f"<i>Participants must be subscribers.</i>"
    )

    await update.effective_message.reply_photo(
        photo=image_url, 
        caption=caption, 
        reply_markup=InlineKeyboardMarkup(keyboard), 
        parse_mode=ParseMode.HTML
    )
    
    context.user_data.pop(user_id, None)
    return ConversationHandler.END

# --- Winner Selection Flow (Stylish: Confirmation and Multi-winner support) ---

async def confirm_close_poll_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shows a confirmation dialog before closing the poll."""
    query = update.callback_query
    giveaway_id = query.data.split("|", 1)[1]
    
    if not is_admin(query.from_user.id):
        await query.answer("❌ Permission denied.", show_alert=True)
        return
    
    giveaway_data = await get_giveaway_by_id(giveaway_id)
    if not giveaway_data or not giveaway_data.get("is_active", False):
        await query.answer("❌ Poll already closed or invalid.", show_alert=True)
        return
    
    winner_count = giveaway_data.get("winner_count", 1)
    total_participants = len(await get_all_participants_for_giveaway(giveaway_id))
    
    if total_participants < winner_count:
        await query.answer(f"⚠️ Enough participants nahi hain! Total: {total_participants}, Required: {winner_count}", show_alert=True)
        return

    keyboard = [
        [InlineKeyboardButton(f"✅ Select {winner_count} Winner(s)!", callback_data=f"close_poll|{giveaway_id}")],
        [InlineKeyboardButton("⬅️ Wapas Jayein", callback_data=f"show_top10|{giveaway_id}")]
    ]
    
    await query.edit_message_caption(
        caption=f"⚠️ **CONFIRMATION:**\n\nKya aap Poll ID <code>{giveaway_id}</code> ko band karke **{winner_count}** winner chunna chahte hain?\n\nTotal Entries: **{total_participants}**",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )

async def close_poll_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Closes the poll, selects winners, and announces."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    giveaway_id = None
    source_chat_id = update.effective_chat.id
    
    if update.callback_query:
        query = update.callback_query
        await query.answer("Winners chune jaa rahe hain...")
        giveaway_id = query.data.split("|", 1)[1]
        source_chat_id = query.message.chat_id
    elif update.effective_message and update.effective_message.text:
        m = update.effective_message.text
        match = re.search(r"^/close_poll_([a-zA-Z0-9]+)$", m)
        if match:
            giveaway_id = match.group(1)
        # Handle invalid text command here
        if not giveaway_id: return

    giveaway_data = await get_giveaway_by_id(giveaway_id)
    if not giveaway_data or not giveaway_data.get("is_active", False):
        await context.bot.send_message(source_chat_id, f"❌ Poll <code>{giveaway_id}</code> pehle hi band ho chuka hai.", parse_mode=ParseMode.HTML)
        return

    winner_count = giveaway_data.get("winner_count", 1) # Stylish: Get configured winner count
    
    winners = await select_random_winners(giveaway_id, count=winner_count)
    total_participants = len(await get_all_participants_for_giveaway(giveaway_id))
    await close_giveaway_db(giveaway_id)

    channel_id = giveaway_data["channel_id"]
    
    # 1. Channel Announcement
    lines = [f"🛑 <b>GIVEAWAY CLOSED!</b>\n\nPoll ID <code>{giveaway_id}</code> band ho gaya hai.", f"Total Entries: <b>{total_participants}</b>\n"]
    if winners:
        lines.append(f"🎉 <b>CONGRATULATIONS TO THE {len(winners)} WINNERS!</b> 🎉")
        
        # Stylish: Winners ko mention karein
        winner_mentions = []
        for i, w in enumerate(winners):
            winner_mentions.append(f"<b>{i+1}.</b> <a href='tg://user?id={w['user_id']}'>{w['full_name']}</a> (@{w['username']})")
            
            # Stylish: Winner ko private message bhej kar notify karein
            await notify_winner_private(context.bot, w, giveaway_id, channel_id)

        lines.extend(winner_mentions)
    else:
        lines.append("⚠️ Koi participants nahi mila.")
    announcement = "\n".join(lines)

    try:
        await context.bot.send_message(chat_id=channel_id, text=announcement, parse_mode=ParseMode.HTML)
    except TelegramError as e:
        logger.warning("Failed to notify channel %s about poll closure: %s", channel_id, e)

    # 2. Admin Confirmation
    await context.bot.send_message(
        source_chat_id, 
        f"✅ Poll <code>{giveaway_id}</code> **safaltapoorvak band** ho gaya. Winners ki ghoshna channel mein kar di gayi hai."
        f"\n\n**Winners (Private Copy):**\n" + "\n".join([f"- {w['full_name']} (ID: {w['user_id']})" for w in winners]) if winners else "",
        parse_mode=ParseMode.HTML
    )

async def notify_winner_private(bot, winner: Dict[str, Any], giveaway_id: str, channel_id: str):
    """Stylish: Notifies the winner privately."""
    try:
        channel_info = await bot.get_chat(channel_id)
        channel_name = getattr(channel_info, "title", "Channel")
        channel_link = f"https://t.me/{getattr(channel_info, 'username', 'telegram')}"
    except TelegramError:
        channel_name = f"ID: {channel_id}"
        channel_link = "https://t.me/telegram"

    try:
        await bot.send_message(
            chat_id=winner["user_id"],
            text=(
                f"🎉 **CONGRATULATIONS!** 🎉\n\n"
                f"Aapne **'{channel_name}'** ke Vote-Poll (ID: <code>{giveaway_id}</code>) mein jeet hasil ki hai!"
                f"\n\nPrize claim karne ke liye kripya **{channel_link}** par channel admin se sampark karein."
            ),
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        logger.warning(f"Failed to notify winner {winner['user_id']} privately: {e}")

# --- Application init & run (Minor changes) ---

async def post_init(application: Application):
    await init_db()
    logger.info("Post-init done.")


def main() -> None:
    # ensure PTB version is correct (your original check function should be defined/imported)
    # ensure_ptb_v20_or_exit() 

    url_path = TELEGRAM_BOT_TOKEN
    webhook_url = f"{WEBHOOK_URL.rstrip('/')}/{url_path}"

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    # Conversation handlers
    giveaway_conv = ConversationHandler(
        entry_points=[CommandHandler("giveaway", start_giveaway)],
        states={
            SELECT_CHANNEL: [MessageHandler(filters.ChatType.PRIVATE & filters.ALL, handle_channel_share)],
            GET_IMAGE_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_image_url)],
            GET_DETAILS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_winner_count)], # Stylish: New handler for winner count
        },
        fallbacks=[CommandHandler("cancel", cancel_giveaway)],
        allow_reentry=True,
    )

    # Core handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("active_polls", active_polls))
    # ... add other command handlers (stats_command, my_entries, export_participants)

    application.add_handler(giveaway_conv)
    # ... add broadcast_conv

    # CallbackQuery handlers
    application.add_handler(CallbackQueryHandler(start_giveaway_conv_callback, pattern="^start_giveaway_conv$"))
    application.add_handler(CallbackQueryHandler(confirm_close_poll_callback, pattern=r"^confirm_close_poll\|")) # Stylish: New confirmation step
    application.add_handler(CallbackQueryHandler(close_poll_handler, pattern=r"^close_poll\|"))
    application.add_handler(CallbackQueryHandler(show_top_participants, pattern=r"^show_top10\|"))

    # Direct text handler for /close_poll_<id>
    application.add_handler(MessageHandler(filters.Regex(r"^/close_poll_[a-zA-Z0-9]+$"), close_poll_handler))

    # Error handler
    application.add_error_handler(error_handler)

    logger.info("Starting webhook...")
    application.run_webhook(listen="0.0.0.0", port=PORT, url_path=url_path, webhook_url=webhook_url)


if __name__ == "__main__":
    # Note: You need to include the missing functions (handle_channel_share, cancel_giveaway,
    # help_command, active_polls, show_top_participants, error_handler, etc.) 
    # from your original code in this final version to make it run.
    main()
