import os
import logging
import uuid
import aiosqlite
from datetime import datetime
from random import sample 
import re 
import sys 

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, ContextTypes, 
    ConversationHandler, MessageHandler, filters, CallbackQueryHandler
)
from telegram.error import TelegramError

# ----------------------------------------------------------------------
# 1. Configuration & Setup (Render Optimized)
# ----------------------------------------------------------------------

# NOTE: Environment variables are loaded directly from the Render environment.

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME")

# Safely parse ADMIN_IDS
try:
    ADMIN_IDS = [int(i.strip()) for i in os.getenv("ADMIN_IDS", "").split(',') if i.strip().isdigit()]
except Exception:
    ADMIN_IDS = []
    
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 8080))

# Critical Check for environment variables
if not TELEGRAM_BOT_TOKEN or not BOT_USERNAME or not WEBHOOK_URL:
    logging.error("CRITICAL: TELEGRAM_BOT_TOKEN, BOT_USERNAME, and WEBHOOK_URL must be set in the environment.")
    sys.exit(1)

# Logging setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# State constants for ConversationHandler
SELECT_CHANNEL, GET_IMAGE_URL, GET_DETAILS = range(3)
BROADCAST_MESSAGE = 99 
DB_FILE = "advanced_giveaway_bot.db"

# In-memory storage for giveaway creation state (Only used for active setup flow)
GIVEAWAY_CREATION_DATA = {} 

# Default Image URL (Ensure this is a public link!)
DEFAULT_GIVEAWAY_IMAGE = "https://envs.sh/GhJ.jpg/IMG20250925634.jpg" 

# Robust URL regex for better image link validation
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
    """Initializes the database and creates necessary tables."""
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
    """Saves a new active giveaway."""
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO giveaways (giveaway_id, channel_id, creator_id, image_url) VALUES (?, ?, ?, ?)",
            (giveaway_id, channel_id, creator_id, image_url)
        )
        await db.commit()

async def get_giveaway_by_id(giveaway_id):
    """Retrieves an active giveaway by its ID."""
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("SELECT channel_id, is_active, image_url FROM giveaways WHERE giveaway_id = ?", (giveaway_id,))
        row = await cursor.fetchone()
        if row:
            return {"channel_id": row[0], "is_active": bool(row[1]), "image_url": row[2]}
        return None

async def log_participant(giveaway_id, user_id, username, full_name):
    """Logs a participant, ensuring they only participate once per giveaway."""
    async with aiosqlite.connect(DB_FILE) as db:
        try:
            await db.execute(
                "INSERT INTO participants (giveaway_id, user_id, username, full_name) VALUES (?, ?, ?, ?)",
                (giveaway_id, user_id, username, full_name)
            )
            await db.commit()
            return True # Success
        except aiosqlite.IntegrityError:
            return False # Already participated

async def get_top_participants(giveaway_id: str, limit: int = 10):
    """Fetches top N participants (by time, latest first)."""
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
    """Fetches all active giveaways for admin list."""
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("SELECT giveaway_id, channel_id, start_time FROM giveaways WHERE is_active = 1 ORDER BY start_time DESC")
        return await cursor.fetchall()

async def get_all_participants_for_giveaway(giveaway_id: str):
    """Fetches all participant user IDs and details for winner selection/broadcast."""
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("SELECT user_id, username, full_name FROM participants WHERE giveaway_id = ?", (giveaway_id,))
        return [{"user_id": row[0], "username": row[1], "full_name": row[2]} for row in await cursor.fetchall()]

async def close_giveaway_db(giveaway_id: str):
    """Sets a giveaway to inactive."""
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("UPDATE giveaways SET is_active = 0 WHERE giveaway_id = ?", (giveaway_id,))
        await db.commit()
        
async def select_random_winners(giveaway_id: str, count: int = 1):
    """Selects a random sample of winners from all participants."""
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
    """Checks if the user is a configured admin."""
    return user_id in ADMIN_IDS

def format_participant_details(user: dict) -> str:
    """Formats the participant message for posting in the channel (HTML Style)."""
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
        f"<b>» VOTE:</b> Click the *REACTION BUTTON* on this post!\n"
        f"CREATED BY USING @{BOT_USERNAME}"
    )
    return message_text

async def check_bot_admin_status(bot_instance, channel_id: int) -> bool:
    """Checks if the bot is an admin in the channel and has required permissions."""
    try:
        member = await bot_instance.get_chat_member(channel_id, bot_instance.id)
        return member.status in ['administrator', 'creator'] and member.can_post_messages
    except TelegramError as e:
        logger.error(f"Error checking admin status in {channel_id}: {e}")
        return False

async def check_user_membership(bot_instance, channel_id: int, user_id: int) -> bool:
    """Checks if a user is a member of the channel (Subscriber check)."""
    try:
        member = await bot_instance.get_chat_member(channel_id, user_id)
        return member.status in ['member', 'administrator', 'creator']
    except TelegramError:
        return False

# ----------------------------------------------------------------------
# 4. Command Handlers & Error Handler
# ----------------------------------------------------------------------

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Safely logs the error and notifies the user if possible."""
    logger.error("Exception while handling an update:", exc_info=context.error)
    
    chat_id = None
    if update.effective_message:
        chat_id = update.effective_message.chat_id
    elif update.callback_query and update.callback_query.message:
        chat_id = update.callback_query.message.chat_id
    elif update.effective_chat:
        chat_id = update.effective_chat.id
    else:
        return

    if chat_id and chat_id > 0: # Only reply in private chats for errors
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text="⚠️ **An unexpected error occurred.** The bot has logged the issue. Please try again or contact support.",
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Failed to send error message back to user {chat_id}: {e}")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles /start command, including Deep Links for participation."""
    user = update.effective_user
    
    if context.args:
        giveaway_id = context.args[0]
        await handle_deep_link_participation(update, context, giveaway_id)
        return

    keyboard = [
        [InlineKeyboardButton("➕ Add Bot To Channel", url=f"https://t.me/{BOT_USERNAME}?startgroup=start")],
        [InlineKeyboardButton("📚 Tutorial", url="https://youtube.com/your_bot_tutorial"),
         InlineKeyboardButton("❓ Support", url="https://t.me/your_support_group")],
    ]
    
    # Use reply_text for simplicity unless a photo is mandatory for /start
    await update.message.reply_text(
        # Note: I'm using reply_text here for better compatibility, 
        # but you can switch to reply_photo if you ensure the image is always accessible.
        # photo=DEFAULT_GIVEAWAY_IMAGE, 
        (
            f"🚀 <b>Welcome to @{BOT_USERNAME}: The Ultimate Vote Bot!</b>\n\n"
            "<i>Automate vote-based giveaways & content contests in your Telegram channels with **Advanced Subscriber Verification**</i>.\n\n"
            "<b>» How to Get Started:</b>\n"
            "• Admins use /giveaway to launch a new vote-poll.\n"
            "• Use /help to see all features."
        ),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Displays the help message with all available commands."""
    help_text = (
        "📚 **BOT COMMANDS & USAGE**\n\n"
        "**» ADMIN COMMANDS (Admin Only):**\n"
        "• `/giveaway` - Start the multi-step process to create a new Vote-Poll.\n"
        "• `/active_polls` - List all currently running polls.\n"
        "• `/close_poll_[ID]` - Manually close a specific poll and announce winner(s).\n"
        "• `/broadcast [ID]` - Send a message to ALL participants of a poll.\n"
        "\n"
        "**» USER COMMANDS:**\n"
        "• `/start` - View the welcome message and main menu.\n"
        "• `/help` - Display this help message.\n"
    )
    await update.message.reply_text(help_text, parse_mode='Markdown')


# ----------------------------------------------------------------------
# 5. Giveaway Conversation Handler (Admin Flow)
# ----------------------------------------------------------------------

async def start_giveaway(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the /giveaway conversation."""
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ You must be an <b>administrator</b> to create this Vote-Poll.", parse_mode=ParseMode.HTML)
        return ConversationHandler.END
        
    user_id = update.effective_user.id
    GIVEAWAY_CREATION_DATA[user_id] = {} 
    
    await update.message.reply_text(
        "🎁 <b>STEP 1/3: Channel Selection</b>\n\n"
        "Please **forward a message** from the channel or **share the channel link/username** where the giveaway will run.",
        parse_mode=ParseMode.HTML
    )
    return SELECT_CHANNEL

async def handle_channel_share(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handles the channel shared/forwarded by the user."""
    user_id = update.effective_user.id
    chat_message = update.message
    
    channel = None
    if chat_message.forward_from_chat and chat_message.forward_from_chat.type in ['channel', 'supergroup']:
        channel = chat_message.forward_from_chat
    elif chat_message.text:
        text = chat_message.text.strip()
        if text.startswith('@') or text.startswith('-100'):
            try:
                channel = await context.bot.get_chat(text)
            except TelegramError:
                pass

    if not channel:
        await chat_message.reply_text("❌ Invalid channel format. Please **forward a message from the channel** or use a correct @username / -100 ID.")
        return SELECT_CHANNEL
        
    channel_id = channel.id
    channel_title = channel.title if channel.title else str(channel_id)

    if channel.username:
        channel_link = f"@{channel.username}"
    else:
        channel_link = f"ID: <code>{channel_id}</code>"
    
    message = await update.message.reply_text(f"⏳ **Verifying admin status in {channel_title} ({channel_link})...**", parse_mode=ParseMode.HTML)
    
    if not await check_bot_admin_status(context.bot, channel_id):
        await message.edit_text(
            f"❌ <b>ADMIN CHECK FAILED!</b>\n\n"
            f"I'm **NOT** an admin in <i>{channel_title}</i>. Please add me and grant **Post Messages** permission.",
            parse_mode=ParseMode.HTML
        )
        return SELECT_CHANNEL

    await message.edit_text(f"✅ **Admin Status Verified!** Bot has required permissions.", parse_mode='Markdown')

    giveaway_id = str(uuid.uuid4()).replace('-', '')[:10]
    GIVEAWAY_CREATION_DATA[user_id]['channel_id'] = str(channel_id)
    GIVEAWAY_CREATION_DATA[user_id]['channel_title'] = channel_title
    GIVEAWAY_CREATION_DATA[user_id]['channel_username'] = channel.username
    GIVEAWAY_CREATION_DATA[user_id]['giveaway_id'] = giveaway_id

    await update.message.reply_text(
        f"🖼️ <b>STEP 2/3: Image URL</b>\n\n"
        f"Please send the **Public HTTPS URL** for the image you want to use.\n"
        f"<i>(e.g., {DEFAULT_GIVEAWAY_IMAGE})</i>",
        parse_mode=ParseMode.HTML
    )
    return GET_IMAGE_URL

async def get_image_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Stores the image URL and proceeds."""
    user_id = update.effective_user.id
    image_url = update.message.text.strip()
    
    if not URL_REGEX.match(image_url):
        await update.message.reply_text("❌ Invalid or non-public URL. Please provide a full public URL starting with 'http' or 'https'.")
        return GET_IMAGE_URL

    GIVEAWAY_CREATION_DATA[user_id]['image_url'] = image_url

    await update.message.reply_text(
        f"🎉 **Image URL Saved!**\n\n"
        "**STEP 3/3: Launch!**\n"
        "To confirm and launch the poll, just send **'LAUNCH'**.",
        parse_mode='Markdown'
    )
    return GET_DETAILS


async def handle_details_and_publish(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Publishes the giveaway and generates the participation link."""
    user_id = update.effective_user.id
    data = GIVEAWAY_CREATION_DATA.get(user_id)
    
    if update.message.text.upper() != 'LAUNCH':
         await update.message.reply_text("Please type **LAUNCH** to proceed.", parse_mode='Markdown')
         return GET_DETAILS

    if not data:
        await update.message.reply_text("❌ Giveaway creation session expired. Start again with /giveaway.")
        return ConversationHandler.END

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
        "✅ **VOTE-POLL CREATED SUCCESSFULLY!**\n\n"
        f"Channel: <b>{channel_title}</b>\n"
        f"Poll ID: <code>{giveaway_id}</code>\n\n"
        f"**Participation Link** (Share this!):\n"
        f"<code>{participation_link}</code>\n\n"
        "<i>Participants must be subscribers to log their entry.</i>"
    )
    
    # Using reply_photo for the final stylish output
    await update.message.reply_photo(
        photo=image_url,
        caption=admin_success_caption,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )

    del GIVEAWAY_CREATION_DATA[user_id]
    return ConversationHandler.END

async def cancel_giveaway(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancels the giveaway creation process."""
    user_id = update.effective_user.id
    if user_id in GIVEAWAY_CREATION_DATA:
        del GIVEAWAY_CREATION_DATA[user_id]
    await update.message.reply_text("🛑 **Vote-Poll creation cancelled.**", parse_mode='Markdown')
    return ConversationHandler.END

# ----------------------------------------------------------------------
# 6. Deep Link Handler (Participant Flow)
# ----------------------------------------------------------------------

async def handle_deep_link_participation(update: Update, context: ContextTypes.DEFAULT_TYPE, giveaway_id: str) -> None:
    """Handles the user clicking the unique participation link."""
    user = update.effective_user
    
    giveaway_data = await get_giveaway_by_id(giveaway_id)
    
    if not giveaway_data or not giveaway_data['is_active']:
        await update.message.reply_text("❌ This vote-poll has ended or is invalid. Please contact the channel admin.")
        return

    channel_id = int(giveaway_data['channel_id'])
    image_url = giveaway_data.get('image_url')

    try:
        channel_info = await context.bot.get_chat(channel_id)
        channel_link = f"https://t.me/{channel_info.username}" if channel_info.username else "Private Channel"
        channel_name = channel_info.title
    except TelegramError:
        channel_link = f"Channel ID: <code>{channel_id}</code>"
        channel_name = "Unknown Channel"

    
    is_subscriber = await check_user_membership(context.bot, channel_id, user.id)
    
    if not is_subscriber:
        caption_text = (
            f"⚠️ <b>PARTICIPATION DENIED!</b>\n\n"
            f"To join the <b>'{channel_name}'</b> poll, you must be a **subscriber**.\n"
            f"Please <b>Join Channel</b>, then click the link again."
        )
        
        await update.message.reply_photo(
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
            
            await update.message.reply_text(
                f"🎉 **CONGRATULATIONS!**\n\n"
                f"You are now a registered participant for the **'{channel_name}'** vote-poll (ID: <code>{giveaway_id}</code>).\n"
                f"Your entry has been **securely logged** in the channel. Ask your friends to vote for your entry there!",
                parse_mode=ParseMode.HTML
            )
            
        except Exception as e:
             logger.error(f"Failed to post to channel {channel_id}: {e}")
             await update.message.reply_text("❌ Participation logged, but failed to post details to the channel. Check bot permissions (Post Messages).")
             
    else:
        await update.message.reply_text(
            f"💡 **ALREADY PARTICIPATED**\n\n"
            "You have already been registered for this vote-poll.",
            parse_mode='Markdown'
        )

# ----------------------------------------------------------------------
# 7. Advanced Admin Features (Winner Selection & Top 10)
# ----------------------------------------------------------------------

async def show_top_participants(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the callback query to display the top 10 recent participants."""
    query = update.callback_query
    await query.answer("Fetching top 10 recent participants...")
    
    try:
        giveaway_id = query.data.split('|')[1]
    except IndexError:
        await query.message.reply_text("❌ Invalid query data.")
        return

    participants_data = await get_top_participants(giveaway_id, limit=10)
    
    message_text = f"🏆 **TOP 10 RECENT PARTICIPANTS (Poll ID: {giveaway_id})** 🏆\n\n"
    
    if not participants_data:
        message_text += "No participants registered yet."
    else:
        for i, (full_name, username, user_id, participation_time) in enumerate(participants_data):
            time_str = datetime.strptime(participation_time.split('.')[0], "%Y-%m-%d %H:%M:%S").strftime("%H:%M:%S")
            
            message_text += (
                f"**{i+1}.** <a href='tg://user?id={user_id}'>{full_name}</a> (@{username})\n"
                f"   _Joined at: {time_str}_\n"
            )
            
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
    """Lists all active giveaways (Admin Only)."""
    if not is_admin(update.effective_user.id):
        return

    active_giveaways = await get_all_active_giveaways()
    
    if not active_giveaways:
        await update.message.reply_text("⭐ **No active Vote-Polls found!** Use /giveaway to start one.", parse_mode='Markdown')
        return

    message_text = "✨ <b>ACTIVE VOTE-POLLS:</b> ✨\n\n"
    for i, (giveaway_id, channel_id, start_time) in enumerate(active_giveaways):
        try:
            channel_info = await context.bot.get_chat(channel_id)
            channel_name = channel_info.title
        except TelegramError:
            channel_name = f"ID: {channel_id}"
            
        message_text += f"<b>{i+1}. {channel_name}</b>\n"
        message_text += f"   ID: <code>{giveaway_id}</code>\n"
        message_text += f"   Start: <i>{start_time.split('.')[0]}</i>\n"
        message_text += f"   Close: <code>/close_poll_{giveaway_id}</code>\n\n"
        
    await update.message.reply_text(message_text, parse_mode=ParseMode.HTML)

async def close_poll_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Closes the poll, selects winner(s), and announces results."""
    user_id = update.effective_user.id
    if not is_admin(user_id):
        return
        
    giveaway_id = None
    source_chat = update.effective_chat
    
    if update.callback_query:
        query = update.callback_query
        await query.answer("Closing poll and selecting winner(s)...")
        giveaway_id = query.data.split('|')[1]
        source_chat = query.message.chat
    else:
        match = re.search(r'(_[a-zA-Z0-9]+)$', update.message.text)
        if match:
            giveaway_id = match.group(0).lstrip('_')
        else:
            await update.message.reply_text("❌ Invalid command format. Use `/close_poll_GIVEAWAYID`.", parse_mode='Markdown')
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
    
    winner_announcement = f"🛑 <b>GIVEAWAY CLOSED!</b>\n\nVote-Poll ID <code>{giveaway_id}</code> is now closed.\nTotal Entries: **{total_participants}**\n\n"
    
    if winners:
        winner_list = []
        for i, winner in enumerate(winners):
            winner_link = f"<a href='tg://user?id={winner['user_id']}'>{winner['full_name']}</a>"
            winner_list.append(f"<b>{i+1}.</b> {winner_link} (@{winner['username']})")
            
        winner_announcement += "🎉🎉 **CONGRATULATIONS TO THE WINNER(S)!** 🎉🎉\n" + "\n".join(winner_list)
        
    else:
        winner_announcement += "⚠️ **No participants found!** No winner could be selected."
        
    try:
        await context.bot.send_message(channel_id, winner_announcement, parse_mode=ParseMode.HTML)
    except TelegramError as e:
        logger.warning(f"Failed to notify channel {channel_id} about poll closure: {e}")

    await context.bot.send_message(source_chat.id, f"✅ Poll <code>{giveaway_id}</code> successfully **CLOSED**.\n**Winner(s) Announced in Channel.**", parse_mode=ParseMode.HTML)

async def start_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the /broadcast conversation (Admin Only)."""
    if not is_admin(update.effective_user.id):
        return ConversationHandler.END
        
    if not context.args:
        await update.message.reply_text("❌ Please specify the giveaway ID: `/broadcast GIVEAWAY_ID`", parse_mode='Markdown')
        return ConversationHandler.END
        
    giveaway_id = context.args[0]
    giveaway_data = await get_giveaway_by_id(giveaway_id)
    
    if not giveaway_data:
        await update.message.reply_text(f"❌ Poll ID <code>{giveaway_id}</code> not found.", parse_mode=ParseMode.HTML)
        return ConversationHandler.END

    context.user_data['broadcast_id'] = giveaway_id
    
    await update.message.reply_text(
        f"📣 **BROADCAST MODE ACTIVATED** for Poll ID: <code>{giveaway_id}</code>\n\n"
        "Please send the message (text, photo, video, or animation) you want to broadcast to all participants.",
        parse_mode=ParseMode.HTML
    )
    return BROADCAST_MESSAGE

async def perform_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Sends the message to all participants of a specific giveaway."""
    giveaway_id = context.user_data.pop('broadcast_id', None)
    if not giveaway_id:
        await update.message.reply_text("❌ Broadcast session expired.")
        return ConversationHandler.END

    participants = [p['user_id'] for p in await get_all_participants_for_giveaway(giveaway_id)]
    total_users = len(participants)
    success_count = 0
    
    message_type = 'Text'
    if update.message.photo: message_type = 'Photo'
    elif update.message.video: message_type = 'Video'
    elif update.message.animation: message_type = 'Animation'

    await update.message.reply_text(f"🚀 Starting **{message_type}** broadcast to **{total_users}** participants of <code>{giveaway_id}</code>...", parse_mode=ParseMode.HTML)

    for user_id in participants:
        try:
            caption = update.message.caption_html if update.message.caption_html else update.message.caption
            
            if update.message.photo:
                await context.bot.send_photo(user_id, update.message.photo[-1].file_id, caption=caption, parse_mode=ParseMode.HTML)
            elif update.message.video:
                await context.bot.send_video(user_id, update.message.video.file_id, caption=caption, parse_mode=ParseMode.HTML)
            elif update.message.animation:
                await context.bot.send_animation(user_id, update.message.animation.file_id, caption=caption, parse_mode=ParseMode.HTML)
            elif update.message.text:
                await context.bot.send_message(user_id, update.message.text_html, parse_mode=ParseMode.HTML)
            
            success_count += 1
        except TelegramError as e:
            logger.warning(f"Broadcast failed for user {user_id} (ID: {giveaway_id}): {e.message}")
            
    await update.message.reply_text(
        f"✅ **BROADCAST COMPLETE!**\n\n"
        f"Sent successfully to **{success_count}** users out of {total_users}."
    )
    return ConversationHandler.END

# ----------------------------------------------------------------------
# 8. Main Runner (Webhook Setup)
# ----------------------------------------------------------------------

async def post_init(application: Application):
    """Runs after the bot is initialized, to set up the DB."""
    await init_db()

def main() -> None:
    """Starts the bot with Webhook for cloud deployment (Render)."""
    
    url_path = TELEGRAM_BOT_TOKEN
    webhook_url = f"{WEBHOOK_URL.rstrip('/')}/{url_path}"

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    # Conversation Handlers
    giveaway_handler = ConversationHandler(
        entry_points=[CommandHandler("giveaway", start_giveaway)],
        states={
            SELECT_CHANNEL: [MessageHandler(filters.ChatType.PRIVATE & (filters.FORWARDED_FROM_CHAT | filters.Regex(r'(@[a-zA-Z0-9_]+|-100\d+)')), handle_channel_share)],
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
        fallbacks=[CommandHandler("cancel", lambda update, context: (update.message.reply_text("Broadcast cancelled."), ConversationHandler.END))]
    )

    # Core & Utility Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("active_polls", active_polls))
    application.add_handler(giveaway_handler)
    application.add_handler(broadcast_handler)
    
    # Callback Query Handlers
    application.add_handler(CallbackQueryHandler(close_poll_handler, pattern=r"^close_poll\|"))
    application.add_handler(CallbackQueryHandler(show_top_participants, pattern=r"show_top10\|"))
    
    # Direct Command for closing poll (FIXED REGEX)
    # This now specifically looks for a command starting with /close_poll_ followed by alphanumeric characters
    application.add_handler(CommandHandler("close_poll", close_poll_handler, filters=filters.Regex(r'^/close_poll_[a-zA-Z0-9]+$')))

    # Add Error Handler (Improved)
    application.add_error_handler(error_handler)

    logger.info(f"Starting Webhook on port {PORT} at URL {webhook_url}")
    # Run the application using Webhook
    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=url_path,
        webhook_url=webhook_url
    )

if __name__ == "__main__":
    main()
