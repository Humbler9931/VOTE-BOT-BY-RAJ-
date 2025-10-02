# main.py
import os
import logging
import uuid
import aiosqlite
from datetime import datetime
from random import sample
import re
import sys
import asyncio

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    ConversationHandler, MessageHandler, filters, CallbackQueryHandler
)
from telegram.error import TelegramError

# ----------------------------------------------------------------------
# 1. Configuration & Setup
# ----------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME")
try:
    ADMIN_IDS = [int(i.strip()) for i in os.getenv("ADMIN_IDS", "").split(',') if i.strip().isdigit()]
except Exception:
    ADMIN_IDS = []

WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 8080))

# CRITICAL CHECK
if not TELEGRAM_BOT_TOKEN or not BOT_USERNAME or not WEBHOOK_URL:
    logging.error("CRITICAL: Essential environment variables (TOKEN, USERNAME, WEBHOOK_URL) are missing.")
    sys.exit(1)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Conversation states
SELECT_CHANNEL, GET_IMAGE_URL, GET_DETAILS = range(3)
BROADCAST_MESSAGE = 99
DB_FILE = "advanced_giveaway_bot.db"

GIVEAWAY_CREATION_DATA = {}
DEFAULT_GIVEAWAY_IMAGE = "https://envs.sh/GhJ.jpg/IMG20250925634.jpg"

URL_REGEX = re.compile(
    r'^(?:http|ftp)s?://'
    r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+(?:[A-Z]{2,6}\.?|[A-Z0-9-]{2,}\.?)|'
    r'localhost|'
    r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'
    r'(?::\d+)?'
    r'(?:/?|[/?]\S+)$', re.IGNORECASE
)

# ----------------------------------------------------------------------
# 2. Database Functions (Asynchronous)
# ----------------------------------------------------------------------

async def init_db():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS giveaways (
                giveaway_id TEXT PRIMARY KEY,
                channel_id TEXT NOT NULL,
                creator_id INTEGER NOT NULL,
                image_url TEXT,
                start_time DATETIME DEFAULT CURRENT_TIMESTAMP,
                is_active INTEGER NOT NULL DEFAULT 1
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS participants (
                giveaway_id TEXT,
                user_id INTEGER,
                username TEXT,
                full_name TEXT,
                participation_time DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (giveaway_id, user_id)
            )
        """)
        await db.commit()
    logger.info("Database initialized successfully.")

async def save_giveaway(giveaway_id, channel_id, creator_id, image_url):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO giveaways (giveaway_id, channel_id, creator_id, image_url) VALUES (?, ?, ?, ?)",
            (giveaway_id, channel_id, creator_id, image_url)
        )
        await db.commit()

async def get_giveaway_by_id(giveaway_id):
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("SELECT channel_id, is_active, image_url FROM giveaways WHERE giveaway_id = ?", (giveaway_id,))
        row = await cursor.fetchone()
        if row:
            return {"channel_id": row[0], "is_active": bool(row[1]), "image_url": row[2]}
        return None

async def log_participant(giveaway_id, user_id, username, full_name):
    async with aiosqlite.connect(DB_FILE) as db:
        try:
            await db.execute(
                "INSERT INTO participants (giveaway_id, user_id, username, full_name) VALUES (?, ?, ?, ?)",
                (giveaway_id, user_id, username, full_name)
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

async def get_top_participants(giveaway_id: str, limit: int = 10):
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("""
            SELECT full_name, username, user_id, participation_time 
            FROM participants 
            WHERE giveaway_id = ? 
            ORDER BY participation_time DESC 
            LIMIT ?
        """, (giveaway_id, limit))
        return await cursor.fetchall()

async def get_all_active_giveaways():
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("SELECT giveaway_id, channel_id, start_time FROM giveaways WHERE is_active = 1 ORDER BY start_time DESC")
        return await cursor.fetchall()

async def get_all_participants_for_giveaway(giveaway_id: str):
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("SELECT user_id, username, full_name FROM participants WHERE giveaway_id = ?", (giveaway_id,))
        return [{"user_id": row[0], "username": row[1], "full_name": row[2]} for row in await cursor.fetchall()]

async def close_giveaway_db(giveaway_id: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("UPDATE giveaways SET is_active = 0 WHERE giveaway_id = ?", (giveaway_id,))
        await db.commit()

async def select_random_winners(giveaway_id: str, count: int = 1):
    participants = await get_all_participants_for_giveaway(giveaway_id)
    if not participants:
        return []
    count = min(count, len(participants))
    winners = sample(participants, count)
    return winners

# ----------------------------------------------------------------------
# 3. Utility Functions & Formatters
# ----------------------------------------------------------------------

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def format_participant_details(user: dict) -> str:
    full_name = user.get('full_name', 'N/A')
    username = user.get('username', 'N/A')
    user_id = user.get('id', 'N/A')
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    message_text = (
        f"<b>[⚡] NEW VOTE-ENTRY [⚡]</b>\n\n"
        f"► 👤 USER: <a href='tg://user?id={user_id}'>{full_name}</a>\n"
        f"► 🆔 USER-ID: <code>{user_id}</code>\n"
        f"► 📛 USERNAME: @{username}\n"
        f"► 🕰️ TIME: <i>{timestamp}</i>\n\n"
        f"<b>» VOTE:</b> Click the REACTION BUTTON on this post!\n"
        f"CREATED BY USING @{BOT_USERNAME}"
    )
    return message_text

async def check_bot_admin_status(bot_instance, channel_id: int) -> bool:
    try:
        channel_id = int(channel_id)
        me = await bot_instance.get_me()
        bot_id = me.id
        member = await bot_instance.get_chat_member(channel_id, bot_id)
        # member could be ChatMemberAdministrator or ChatMemberOwner etc.
        # check for permissions where available
        status = getattr(member, "status", "")
        can_post = getattr(member, "can_post_messages", True)  # assume True if not present (e.g., in groups)
        return (status in ['administrator', 'creator']) and can_post
    except TelegramError as e:
        logger.error(f"Error checking admin status in {channel_id}: {e}")
        return False
    except Exception as e:
        logger.error(f"Unexpected error checking admin status: {e}")
        return False

async def check_user_membership(bot_instance, channel_id: int, user_id: int) -> bool:
    try:
        channel_id = int(channel_id)
        member = await bot_instance.get_chat_member(channel_id, user_id)
        return member.status in ['member', 'administrator', 'creator']
    except TelegramError:
        return False
    except Exception:
        return False

# ----------------------------------------------------------------------
# 4. Command Handlers & Error Handler
# ----------------------------------------------------------------------

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
                text="⚠️ An unexpected error occurred. The bot has logged the issue. Please try again or contact support.",
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"Failed to send error message back to user {chat_id}: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.args:
        giveaway_id = context.args[0]
        await handle_deep_link_participation(update, context, giveaway_id)
        return

    keyboard = [
        [InlineKeyboardButton("➕ Add Bot To Channel", url=f"https://t.me/{BOT_USERNAME}?startgroup=start")],
        [InlineKeyboardButton("📚 Tutorial", url="https://youtube.com/your_bot_tutorial"),
         InlineKeyboardButton("❓ Support", url="https://t.me/your_support_group")],
    ]

    # Prefer effective_message to be robust
    message = update.effective_message
    if message:
        await message.reply_photo(
            photo=DEFAULT_GIVEAWAY_IMAGE,
            caption=(
                f"🚀 <b>Welcome to @{BOT_USERNAME}: The Ultimate Vote Bot!</b>\n\n"
                "<i>Automate vote-based giveaways & content contests in your Telegram channels with Advanced Subscriber Verification.</i>\n\n"
                "<b>» How to Get Started:</b>\n"
                "• Admins use /giveaway to launch a new vote-poll.\n"
                "• Use /help to see all features."
            ),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = (
        "📚 *BOT COMMANDS & USAGE*\n\n"
        "*» ADMIN COMMANDS (Admin Only):*\n"
        "• /giveaway - Start the multi-step process to create a new Vote-Poll.\n"
        "• /active_polls - List all currently running polls.\n"
        "• /close_poll_<ID> - Manually close a specific poll and announce winner(s).\n"
        "• /broadcast <ID> - Send a message to ALL participants of a poll.\n\n"
        "*» USER COMMANDS:*\n"
        "• /start - View the welcome message and main menu.\n"
        "• /help - Display this help message."
    )
    await update.effective_message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN)

# ----------------------------------------------------------------------
# 5. Giveaway Conversation Handler (Admin Flow)
# ----------------------------------------------------------------------

async def start_giveaway(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ You must be an <b>administrator</b> to create this Vote-Poll.", parse_mode=ParseMode.HTML)
        return ConversationHandler.END

    user_id = update.effective_user.id
    GIVEAWAY_CREATION_DATA[user_id] = {}

    await update.message.reply_text(
        "🎁 <b>STEP 1/3: Channel Selection</b>\n\n"
        "Please forward a message from the channel or share the channel link/username where the giveaway will run. (e.g., `@MyChannel` or `-100123456789`)",
        parse_mode=ParseMode.HTML
    )
    return SELECT_CHANNEL

async def handle_channel_share(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    chat_message = update.message

    channel = None
    if chat_message is None:
        await update.effective_message.reply_text("❌ Please forward a message from the channel or send channel username/ID.")
        return SELECT_CHANNEL

    if chat_message.forward_from_chat and getattr(chat_message.forward_from_chat, "type", "") in ['channel', 'supergroup']:
        channel = chat_message.forward_from_chat
    elif chat_message.text:
        text = chat_message.text.strip()
        # Regex to match @username or -100ID
        if re.match(r'^@[A-Za-z0-9_]+$|^-\d+$', text):
            try:
                channel = await context.bot.get_chat(text)
            except TelegramError as e:
                logger.warning(f"Failed to get chat for {text}: {e}")
                channel = None

    if not channel:
        await chat_message.reply_text("❌ Invalid channel format. Please forward a message from the channel or use a correct `@username` / `-100 ID`.", parse_mode=ParseMode.MARKDOWN)
        return SELECT_CHANNEL

    channel_id = str(channel.id)
    channel_title = channel.title if channel.title else "Untitled Channel"
    channel_username = channel.username

    message = await update.message.reply_text(f"⏳ Verifying admin status in {channel_title}...", parse_mode=ParseMode.HTML)

    if not await check_bot_admin_status(context.bot, channel_id):
        await message.edit_text(
            f"❌ <b>ADMIN CHECK FAILED!</b>\n\n"
            f"I'm <b>NOT</b> an admin in <i>{channel_title}</i>. Please add me and grant Post Messages permission.",
            parse_mode=ParseMode.HTML
        )
        return SELECT_CHANNEL

    await message.edit_text("✅ <b>Admin Status Verified! Bot has required permissions.</b>", parse_mode=ParseMode.HTML)

    giveaway_id = str(uuid.uuid4()).replace('-', '')[:10]
    GIVEAWAY_CREATION_DATA[user_id]['channel_id'] = channel_id
    GIVEAWAY_CREATION_DATA[user_id]['channel_title'] = channel_title
    GIVEAWAY_CREATION_DATA[user_id]['channel_username'] = channel_username
    GIVEAWAY_CREATION_DATA[user_id]['giveaway_id'] = giveaway_id

    await update.message.reply_text(
        f"🖼️ <b>STEP 2/3: Image URL</b>\n\n"
        f"Please send the Public HTTPS URL for the image you want to use.\n"
        f"(e.g., {DEFAULT_GIVEAWAY_IMAGE})",
        parse_mode=ParseMode.HTML
    )
    return GET_IMAGE_URL

async def get_image_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if not update.message or not update.message.text:
        await update.message.reply_text("❌ Please send a text URL (http/https).")
        return GET_IMAGE_URL

    image_url = update.message.text.strip()
    if not URL_REGEX.match(image_url):
        await update.message.reply_text("❌ Invalid or non-public URL. Please provide a full public URL starting with 'http' or 'https'.")
        return GET_IMAGE_URL

    GIVEAWAY_CREATION_DATA[user_id]['image_url'] = image_url

    await update.message.reply_text(
        "🎉 Image URL Saved!\n\nSTEP 3/3: Launch!\nTo confirm and launch the poll, just send 'LAUNCH'.",
        parse_mode=ParseMode.MARKDOWN
    )
    return GET_DETAILS

async def handle_details_and_publish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    data = GIVEAWAY_CREATION_DATA.get(user_id)
    if not data:
        await update.message.reply_text("❌ Giveaway creation session expired. Start again with /giveaway.")
        return ConversationHandler.END

    text = update.message.text if update.message and update.message.text else ""
    if text.upper() != 'LAUNCH':
        await update.message.reply_text("Please type LAUNCH to proceed.", parse_mode=ParseMode.MARKDOWN)
        return GET_DETAILS

    channel_title = data['channel_title']
    channel_username = data.get('channel_username')
    giveaway_id = data['giveaway_id']
    image_url = data['image_url']

    await save_giveaway(giveaway_id, data['channel_id'], user_id, image_url)

    participation_link = f"https://t.me/{BOT_USERNAME}?start={giveaway_id}"
    channel_url = f"https://t.me/{channel_username}" if channel_username else "https://t.me/telegram"

    keyboard = [
        [
            InlineKeyboardButton("✨ Channel Link", url=channel_url),
            InlineKeyboardButton("🏆 View Top 10", callback_data=f"show_top10|{giveaway_id}")
        ],
        [InlineKeyboardButton("🛑 CLOSE POLL & SELECT WINNER(S)", callback_data=f"close_poll|{giveaway_id}")]
    ]

    admin_success_caption = (
        f"✅ <b>VOTE-POLL CREATED SUCCESSFULLY!</b>\n\n"
        f"Channel: <b>{channel_title}</b>\n"
        f"Poll ID: <code>{giveaway_id}</code>\n\n"
        f"Participation Link (Share this!):\n<code>{participation_link}</code>\n\n"
        f"<i>Participants must be subscribers to log their entry.</i>"
    )

    await update.message.reply_photo(
        photo=image_url,
        caption=admin_success_caption,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )

    del GIVEAWAY_CREATION_DATA[user_id]
    return ConversationHandler.END

async def cancel_giveaway(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if user_id in GIVEAWAY_CREATION_DATA:
        del GIVEAWAY_CREATION_DATA[user_id]
    await update.message.reply_text("🛑 Vote-Poll creation cancelled.", parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

# ----------------------------------------------------------------------
# 6. Deep Link Handler (Participant Flow)
# ----------------------------------------------------------------------

async def handle_deep_link_participation(update: Update, context: ContextTypes.DEFAULT_TYPE, giveaway_id: str) -> None:
    user = update.effective_user

    giveaway_data = await get_giveaway_by_id(giveaway_id)
    if not giveaway_data or not giveaway_data['is_active']:
        await update.effective_message.reply_text("❌ This vote-poll has ended or is invalid. Please contact the channel admin.")
        return

    channel_id = giveaway_data['channel_id']
    image_url = giveaway_data.get('image_url')

    try:
        channel_info = await context.bot.get_chat(channel_id)
        channel_link = f"https://t.me/{channel_info.username}" if channel_info.username else "https://t.me/telegram"
        channel_name = channel_info.title
    except TelegramError:
        channel_link = f"Channel ID: <code>{channel_id}</code>"
        channel_name = "Unknown Channel"

    is_subscriber = await check_user_membership(context.bot, channel_id, user.id)
    if not is_subscriber:
        caption_text = (
            f"⚠️ <b>PARTICIPATION DENIED!</b>\n\n"
            f"To join the <b>'{channel_name}'</b> poll, you must be a subscriber.\n"
            f"Please Join Channel, then click the link again."
        )

        await update.effective_message.reply_photo(
            photo=image_url,
            caption=caption_text,
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🚀 Join Channel", url=channel_link)],
                [InlineKeyboardButton("✅ I have Joined, Try Again", url=f"https://t.me/{BOT_USERNAME}?start={giveaway_id}")]
            ])
        )
        return

    user_full_name = user.full_name
    user_username = user.username if user.username else f"id{user.id}"

    success = await log_participant(giveaway_id, user.id, user_username, user_full_name)
    if success:
        participant_message = format_participant_details({
            'full_name': user_full_name,
            'username': user_username,
            'id': user.id
        })
        try:
            await context.bot.send_message(
                chat_id=channel_id,
                text=participant_message,
                parse_mode=ParseMode.HTML
            )

            await update.effective_message.reply_text(
                f"🎉 <b>CONGRATULATIONS!</b>\n\n"
                f"You are now a registered participant for the <b>'{channel_name}'</b> vote-poll (ID: <code>{giveaway_id}</code>).\n"
                f"Your entry has been securely logged in the channel. Ask your friends to vote for your entry there!",
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.error(f"Failed to post to channel {channel_id}: {e}")
            await update.effective_message.reply_text("❌ Participation logged, but failed to post details to the channel. Check bot permissions (Post Messages).")
    else:
        await update.effective_message.reply_text(
            "💡 <b>ALREADY PARTICIPATED</b>\n\nYou have already been registered for this vote-poll.",
            parse_mode=ParseMode.HTML
        )

# ----------------------------------------------------------------------
# 7. Advanced Admin Features (Winner Selection & Top 10)
# ----------------------------------------------------------------------

async def show_top_participants(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None:
        return

    # Only allow private chats to request this list (or admins)
    if query.message and query.message.chat_id < 0 and not is_admin(query.from_user.id):
        await query.answer("You can only view this list in a private chat with the bot.", show_alert=True)
        return

    await query.answer("Fetching top 10 recent participants...")

    try:
        giveaway_id = query.data.split('|')[1]
    except Exception:
        await query.message.reply_text("❌ Invalid query data.")
        return

    participants_data = await get_top_participants(giveaway_id, limit=10)

    message_lines = [f"🏆 <b>TOP 10 RECENT PARTICIPANTS (Poll ID: {giveaway_id})</b> 🏆", ""]
    if not participants_data:
        message_lines.append("No participants registered yet.")
    else:
        for i, (full_name, username, user_id, participation_time) in enumerate(participants_data):
            try:
                dt_obj = datetime.strptime(participation_time.split('.')[0], "%Y-%m-%d %H:%M:%S")
                time_str = dt_obj.strftime("%H:%M:%S")
            except Exception:
                time_str = "Unknown Time"
            message_lines.append(f"<b>{i+1}.</b> <a href='tg://user?id={user_id}'>{full_name}</a> (@{username}) — <i>{time_str}</i>")

    message_text = "\n".join(message_lines)
    try:
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=message_text,
            parse_mode=ParseMode.HTML
        )
    except TelegramError as e:
        logger.error(f"Failed to send top 10 list: {e}")
        await query.message.reply_text("❌ Failed to send the list. Check bot permissions.")

async def active_polls(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return

    active_giveaways = await get_all_active_giveaways()
    if not active_giveaways:
        await update.effective_message.reply_text("⭐ No active Vote-Polls found! Use /giveaway to start one.", parse_mode=ParseMode.MARKDOWN)
        return

    lines = ["✨ <b>ACTIVE VOTE-POLLS:</b> ✨", ""]
    for i, (giveaway_id, channel_id, start_time) in enumerate(active_giveaways):
        try:
            channel_info = await context.bot.get_chat(channel_id)
            channel_name = channel_info.title
        except TelegramError:
            channel_name = f"ID: {channel_id}"
        lines.append(f"<b>{i+1}. {channel_name}</b>")
        lines.append(f"   ID: <code>{giveaway_id}</code>")
        lines.append(f"   Start: <i>{start_time.split('.')[0]}</i>")
        lines.append(f"   Close: <code>/close_poll_{giveaway_id}</code>")
        lines.append("")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

async def close_poll_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # This handler can be triggered either by CallbackQuery or by a text message matching /close_poll_<id>
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return

    giveaway_id = None
    source_chat = update.effective_chat

    if update.callback_query:
        query = update.callback_query
        await query.answer("Closing poll and selecting winner(s)...")
        try:
            giveaway_id = query.data.split('|')[1]
        except Exception:
            await query.message.reply_text("❌ Invalid query data.")
            return
        source_chat = query.message.chat
    elif update.message and update.message.text:
        match = re.search(r'^/close_poll_([a-zA-Z0-9]+)$', update.message.text)
        if match:
            giveaway_id = match.group(1)
        else:
            await update.message.reply_text("❌ Invalid command format. Use /close_poll_<ID>.", parse_mode=ParseMode.MARKDOWN)
            return

    if not giveaway_id:
        return

    giveaway_data = await get_giveaway_by_id(giveaway_id)
    if not giveaway_data or not giveaway_data['is_active']:
        await context.bot.send_message(source_chat.id, f"❌ Poll <code>{giveaway_id}</code> is already closed or does not exist.", parse_mode=ParseMode.HTML)
        return

    winners = await select_random_winners(giveaway_id, count=1)
    total_participants = len(await get_all_participants_for_giveaway(giveaway_id))

    await close_giveaway_db(giveaway_id)

    channel_id = giveaway_data['channel_id']

    winner_announcement_lines = [
        "🛑 <b>GIVEAWAY CLOSED!</b>",
        "",
        f"Vote-Poll ID <code>{giveaway_id}</code> is now closed.",
        f"Total Entries: <b>{total_participants}</b>",
        ""
    ]

    if winners:
        for i, winner in enumerate(winners):
            winner_link = f"<a href='tg://user?id={winner['user_id']}'>{winner['full_name']}</a>"
            winner_announcement_lines.append(f"<b>{i+1}.</b> {winner_link} (@{winner['username']})")
    else:
        winner_announcement_lines.append("⚠️ <b>No participants found!</b> No winner could be selected.")

    winner_announcement = "\n".join(winner_announcement_lines)

    try:
        await context.bot.send_message(channel_id, winner_announcement, parse_mode=ParseMode.HTML)
    except TelegramError as e:
        logger.warning(f"Failed to notify channel {channel_id} about poll closure: {e}")

    await context.bot.send_message(source_chat.id, f"✅ Poll <code>{giveaway_id}</code> successfully CLOSED.\nWinner(s) Announced in Channel.", parse_mode=ParseMode.HTML)

# ----------------------------------------------------------------------
# 7b. Broadcast Handlers
# ----------------------------------------------------------------------

async def start_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END

    if not context.args:
        await update.message.reply_text("❌ Please specify the giveaway ID: `/broadcast GIVEAWAY_ID`", parse_mode=ParseMode.MARKDOWN)
        return ConversationHandler.END

    giveaway_id = context.args[0]
    giveaway_data = await get_giveaway_by_id(giveaway_id)
    if not giveaway_data:
        await update.message.reply_text(f"❌ Poll ID <code>{giveaway_id}</code> not found.", parse_mode=ParseMode.HTML)
        return ConversationHandler.END

    context.user_data['broadcast_id'] = giveaway_id
    await update.message.reply_text(
        f"📣 BROADCAST MODE ACTIVATED for Poll ID: <code>{giveaway_id}</code>\n\n"
        "Please send the message (text, photo, video, or animation) you want to broadcast to all participants.",
        parse_mode=ParseMode.HTML
    )
    return BROADCAST_MESSAGE

async def perform_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    giveaway_id = context.user_data.pop('broadcast_id', None)
    if not giveaway_id:
        await update.message.reply_text("❌ Broadcast session expired.")
        return ConversationHandler.END

    participants = [p['user_id'] for p in await get_all_participants_for_giveaway(giveaway_id)]
    total_users = len(participants)
    success_count = 0

    message_type = 'Text'
    if update.message.photo:
        message_type = 'Photo'
    elif update.message.video:
        message_type = 'Video'
    elif update.message.animation:
        message_type = 'Animation'

    await update.message.reply_text(f"🚀 Starting {message_type} broadcast to {total_users} participants of {giveaway_id}...", parse_mode=ParseMode.HTML)

    # Use safe text/caption extraction
    text_payload = update.message.text if update.message.text else None
    caption_payload = update.message.caption if getattr(update.message, "caption", None) else None

    for user_id in participants:
        try:
            if update.message.photo:
                await context.bot.send_photo(chat_id=user_id, photo=update.message.photo[-1].file_id, caption=caption_payload or "", parse_mode=ParseMode.HTML)
            elif update.message.video:
                await context.bot.send_video(chat_id=user_id, video=update.message.video.file_id, caption=caption_payload or "", parse_mode=ParseMode.HTML)
            elif update.message.animation:
                await context.bot.send_animation(chat_id=user_id, animation=update.message.animation.file_id, caption=caption_payload or "", parse_mode=ParseMode.HTML)
            elif text_payload:
                await context.bot.send_message(chat_id=user_id, text=text_payload, parse_mode=ParseMode.HTML)
            success_count += 1
        except TelegramError as e:
            logger.warning(f"Broadcast failed for user {user_id} (giveaway {giveaway_id}): {str(e)}")
        except Exception as e:
            logger.warning(f"Unexpected error sending broadcast to {user_id}: {str(e)}")

    await update.message.reply_text(f"✅ BROADCAST COMPLETE!\n\nSent successfully to {success_count} users out of {total_users}.")
    return ConversationHandler.END

async def cancel_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Broadcast cancelled.")
    return ConversationHandler.END

# ----------------------------------------------------------------------
# 8. Main Runner (Webhook Setup)
# ----------------------------------------------------------------------

async def post_init(application: Application):
    await init_db()
    logger.info("Bot application post_init complete.")

def main() -> None:
    url_path = TELEGRAM_BOT_TOKEN
    webhook_url = f"{WEBHOOK_URL.rstrip('/')}/{url_path}"

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    # Handlers
    giveaway_handler = ConversationHandler(
        entry_points=[CommandHandler("giveaway", start_giveaway)],
        states={
            # Permit all private messages here; handler validates content.
            SELECT_CHANNEL: [MessageHandler(filters.ChatType.PRIVATE & filters.ALL, handle_channel_share)],
            GET_IMAGE_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_image_url)],
            GET_DETAILS: [MessageHandler(filters.TEXT & filters.Regex(r'^(LAUNCH|launch)$'), handle_details_and_publish)],
        },
        fallbacks=[CommandHandler("cancel", cancel_giveaway)]
    )

    broadcast_handler = ConversationHandler(
        entry_points=[CommandHandler("broadcast", start_broadcast)],
        states={
            BROADCAST_MESSAGE: [MessageHandler(filters.ALL & ~filters.COMMAND, perform_broadcast)],
        },
        fallbacks=[CommandHandler("cancel", cancel_broadcast)]
    )

    # Core handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("active_polls", active_polls))
    application.add_handler(giveaway_handler)
    application.add_handler(broadcast_handler)

    # CallbackQuery handlers
    application.add_handler(CallbackQueryHandler(close_poll_handler, pattern=r"^close_poll\|"))
    application.add_handler(CallbackQueryHandler(show_top_participants, pattern=r"^show_top10\|"))

    # Support direct text command /close_poll_<id>
    application.add_handler(MessageHandler(filters.Regex(r'^/close_poll_[a-zA-Z0-9]+$'), close_poll_handler))

    application.add_error_handler(error_handler)

    logger.info(f"Setting up Webhook on port {PORT} at URL path: /{url_path}")
    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=url_path,
        webhook_url=webhook_url
    )

if __name__ == "__main__":
    main()
