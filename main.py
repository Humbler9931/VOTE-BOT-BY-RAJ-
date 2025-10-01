import os
import logging
import json
import uuid
import aiosqlite
import asyncio # For stylish delays
from datetime import datetime
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, ParseMode
from telegram.ext import (
    Application, CommandHandler, ContextTypes, 
    ConversationHandler, MessageHandler, filters, CallbackQueryHandler
)
from telegram.error import TelegramError

# ----------------------------------------------------------------------
# 1. Configuration & Setup
# ----------------------------------------------------------------------

# Load environment variables
load_dotenv()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME")
ADMIN_IDS = [int(i.strip()) for i in os.getenv("ADMIN_IDS", "").split(',') if i.strip()]

if not TELEGRAM_BOT_TOKEN or not BOT_USERNAME:
    raise ValueError("TELEGRAM_BOT_TOKEN and BOT_USERNAME must be set in .env")

# Logging setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# State constants for ConversationHandler
SELECT_CHANNEL, GET_DETAILS, FINISH_GIVEAWAY = range(3)
DB_FILE = "advanced_giveaway_bot.db"

# In-memory storage for giveaway creation state: {user_id: {channel_id, giveaway_id, ...}}
GIVEAWAY_CREATION_DATA = {} 

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

async def save_giveaway(giveaway_id, channel_id, creator_id):
    """Saves a new active giveaway."""
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO giveaways (giveaway_id, channel_id, creator_id) VALUES (?, ?, ?)",
            (giveaway_id, channel_id, creator_id)
        )
        await db.commit()

async def get_giveaway_by_id(giveaway_id):
    """Retrieves an active giveaway by its ID."""
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("SELECT channel_id, is_active FROM giveaways WHERE giveaway_id = ?", (giveaway_id,))
        row = await cursor.fetchone()
        if row:
            return {"channel_id": row[0], "is_active": bool(row[1])}
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
        # Note: In a real voting bot, this query would be complex (e.g., GROUP BY and COUNT votes)
        # Here, we fetch the latest participants as a placeholder for complexity.
        cursor = await db.execute("""
            SELECT full_name, username, user_id, participation_time 
            FROM participants 
            WHERE giveaway_id = ? 
            ORDER BY participation_time DESC 
            LIMIT ?
        """, (giveaway_id, limit))
        return await cursor.fetchall()


# ----------------------------------------------------------------------
# 3. Utility Functions & Formatters (Stylish)
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
        f"<b>[⚡] PARTICIPANT DETAILS [⚡]</b>\n\n"
        f"► 👤 USER: <a href='tg://user?id={user_id}'>{full_name}</a>\n"
        f"► 🆔 USER-ID: <code>{user_id}</code>\n"
        f"► 📛 USERNAME: @{username}\n"
        f"► 🕰️ TIME: <i>{timestamp}</i>\n\n"
        f"<b>NOTE: ONLY CHANNEL SUBSCRIBERS CAN VOTE.</b>\n\n"
        f"CREATED BY USING @{BOT_USERNAME}"
    )
    return message_text

async def check_bot_admin_status(bot_instance, channel_id: int) -> bool:
    """Checks if the bot is an admin in the channel and has required permissions."""
    try:
        member = await bot_instance.get_chat_member(channel_id, bot_instance.id)
        # Needs to be admin and have Post Messages permission
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
# 4. Command Handlers
# ----------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles /start command, including Deep Links for participation."""
    user = update.effective_user
    
    if context.args:
        giveaway_id = context.args[0]
        await handle_deep_link_participation(update, context, giveaway_id)
        return

    # Stylish Standard /start message with buttons
    keyboard = [
        [InlineKeyboardButton("➕ Add Me To Your Channel", url=f"https://t.me/{BOT_USERNAME}?startgroup=start")],
        [InlineKeyboardButton("📚 Tutorial Video", callback_data="tutorial_video"),
         InlineKeyboardButton("❓ Support", url="https://t.me/your_support_group")],
    ]
    
    await update.message.reply_text(
        f"🚀 <b>Welcome to @{BOT_USERNAME}: The Ultimate Vote Bot!</b>\n\n"
        "<i>Automate vote-based giveaways & content contests in your Telegram channels with **Advanced Subscriber Verification**</i>.\n\n"
        "<b>» How to Get Started:</b>\n"
        "• Use /giveaway to launch a new vote-poll.\n"
        "• Use /help to see all features.",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML
    )

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
    
    keyboard = [
        [InlineKeyboardButton("Click to Share Channel ➡️", callback_data="select_channel_prompt")]
    ]
    
    await update.message.reply_text(
        "🎁 <b>STEP 1/3: Channel Selection</b>\n\n"
        "Please **forward a message** from the channel or **share the channel link/username** where the giveaway will run.",
        reply_markup=InlineKeyboardMarkup(keyboard),
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
    elif chat_message.text and (chat_message.text.startswith('@') or chat_message.text.startswith('-100')):
        try:
            # Get chat info using the text (username or ID)
            channel = await context.bot.get_chat(chat_message.text)
        except TelegramError:
            pass # Channel not found, error handled below

    if not channel:
        await chat_message.reply_text("❌ Invalid channel format. Please **forward a message from the channel** or use a correct @username / -100 ID.")
        return SELECT_CHANNEL
        
    channel_id = channel.id
    channel_title = channel.title
    
    # CRITICAL: Stylish Admin Check
    message = await update.message.reply_text(f"⏳ **Verifying admin status in {channel_title}...**", parse_mode='Markdown')
    await asyncio.sleep(1) # Dramatic effect
    
    if not await check_bot_admin_status(context.bot, channel_id):
        await message.edit_text(
            f"❌ <b>ADMIN CHECK FAILED!</b>\n\n"
            f"I'm **NOT** an admin in <i>{channel_title}</i>. Please add me and grant **Post Messages** permission.",
            parse_mode=ParseMode.HTML
        )
        return SELECT_CHANNEL

    await message.edit_text(f"✅ **Admin Status Verified!** Proceeding to next step.", parse_mode='Markdown')

    # Success: Save data and proceed
    giveaway_id = str(uuid.uuid4()).replace('-', '')[:10]
    GIVEAWAY_CREATION_DATA[user_id]['channel_id'] = str(channel_id)
    GIVEAWAY_CREATION_DATA[user_id]['channel_title'] = channel_title
    GIVEAWAY_CREATION_DATA[user_id]['giveaway_id'] = giveaway_id

    # Stylish next prompt
    await update.message.reply_text(
        f"🎉 **Channel: {channel_title}**\n\n"
        "**STEP 2/3: Launch!**\n"
        "The bot will automatically generate the Participation Link. Just send **'LAUNCH'** to finalize the vote-poll creation.",
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

    channel_id = data['channel_id']
    channel_title = data['channel_title']
    giveaway_id = data['giveaway_id']

    # 1. Save giveaway to DB
    await save_giveaway(giveaway_id, channel_id, user_id)

    # 2. Generate Deep Link
    participation_link = f"https://t.me/{BOT_USERNAME}?start={giveaway_id}"

    # 3. Create stylish success message with buttons
    keyboard = [
        [
            InlineKeyboardButton("✨ Channel", url=f"https://t.me/{channel_title.strip('@')}"),
            InlineKeyboardButton("🏆 View Top 10", callback_data=f"show_top10|{giveaway_id}")
        ]
    ]

    await update.message.reply_text(
        "✅ **VOTE-POLL CREATED SUCCESSFULLY!**\n\n"
        f"Channel: <b>{channel_title}</b>\n"
        f"Participation Link: <code>{participation_link}</code>\n\n"
        "<i>Share the link to start accepting participants!</i>",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True
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
    
    # 1. CRITICAL: Check Channel Subscription
    is_subscriber = await check_user_membership(context.bot, channel_id, user.id)
    
    if not is_subscriber:
        await update.message.reply_text(
            f"⚠️ <b>PARTICIPATION DENIED!</b>\n\n"
            f"You must be a <b>subscriber</b> of the giveaway channel to participate.\n"
            f"Please <b>Join Channel</b>, then click the link again.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🚀 Join Channel", url=f"https://t.me/{giveaway_data['channel_id']}")],
                [InlineKeyboardButton("✅ I have Joined, Try Again", url=f"https://t.me/{BOT_USERNAME}?start={giveaway_id}")]
            ])
        )
        return

    # 2. Log Participant in DB
    user_full_name = user.full_name
    user_username = user.username if user.username else f"id{user.id}"
    
    success = await log_participant(giveaway_id, user.id, user_username, user_full_name)

    if success:
        # 3. Post PARTICIPANT DETAILS to the channel
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
            
            # Send stylish success message to the participant in private chat
            await update.message.reply_text(
                f"🎉 **CONGRATULATIONS!**\n\n"
                f"You are now a registered participant for this vote-poll (ID: <code>{giveaway_id}</code>).\n"
                "Your details have been **securely logged** in the channel.",
                parse_mode=ParseMode.HTML
            )
            
        except Exception as e:
             logger.error(f"Failed to post to channel {channel_id}: {e}")
             await update.message.reply_text("❌ Participation logged, but failed to post details to the channel. Check bot permissions.")
             
    else:
        await update.message.reply_text(
            f"💡 **ALREADY PARTICIPATED**\n\n"
            "You have already been registered for this vote-poll.",
            parse_mode='Markdown'
        )

# ----------------------------------------------------------------------
# 7. Callback Query Handler (Top 10 Feature)
# ----------------------------------------------------------------------

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):pass
