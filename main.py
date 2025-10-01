import os
import logging
import json
import uuid
import aiosqlite
import asyncio # For stylish delays
from datetime import datetime
from dotenv import load_dotenv

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
from telegram.constants import ParseMode
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

# State constants for ConversationHandler (New State Added: GET_IMAGE_URL)
SELECT_CHANNEL, GET_IMAGE_URL, GET_DETAILS, FINISH_GIVEAWAY = range(4)
BROADCAST_MESSAGE = 99 # Separate state for broadcast
DB_FILE = "advanced_giveaway_bot.db"

# In-memory storage for giveaway creation state
GIVEAWAY_CREATION_DATA = {} 

# Global Image URL (Aapki maang ke anusaar, lekin ise har poll ke liye DB mein save karna better hai)
DEFAULT_GIVEAWAY_IMAGE = "https://envs.sh/GhJ.jpg/IMG20250925634.jpg" 

# ----------------------------------------------------------------------
# 2. Database Functions (Asynchronous)
# ----------------------------------------------------------------------

# (init_db, save_giveaway, get_giveaway_by_id, log_participant, get_top_participants functions are kept the same)

async def init_db():
    """Initializes the database and creates necessary tables (Updated to save image_url)."""
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS giveaways (
                giveaway_id TEXT PRIMARY KEY,
                channel_id TEXT NOT NULL,
                creator_id INTEGER NOT NULL,
                image_url TEXT,  -- New column for image URL
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
    """Saves a new active giveaway (Updated with image_url)."""
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO giveaways (giveaway_id, channel_id, creator_id, image_url) VALUES (?, ?, ?, ?)",
            (giveaway_id, channel_id, creator_id, image_url)
        )
        await db.commit()

async def get_giveaway_by_id(giveaway_id):
    """Retrieves an active giveaway by its ID (Updated to fetch image_url)."""
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
    """Fetches all user_ids for a specific giveaway (for broadcast)."""
    async with aiosqlite.connect(DB_FILE) as db:
        cursor = await db.execute("SELECT user_id FROM participants WHERE giveaway_id = ?", (giveaway_id,))
        return [row[0] for row in await cursor.fetchall()]

async def close_giveaway_db(giveaway_id: str):
    """Sets a giveaway to inactive."""
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("UPDATE giveaways SET is_active = 0 WHERE giveaway_id = ?", (giveaway_id,))
        await db.commit()
        
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
    
    # Use a photo for the start message for extra flair
    await update.message.reply_photo(
        photo=DEFAULT_GIVEAWAY_IMAGE, 
        caption=(
            f"🚀 <b>Welcome to @{BOT_USERNAME}: The Ultimate Vote Bot!</b>\n\n"
            "<i>Automate vote-based giveaways & content contests in your Telegram channels with **Advanced Subscriber Verification**</i>.\n\n"
            "<b>» How to Get Started:</b>\n"
            "• Use /giveaway to launch a new vote-poll.\n"
            "• Use /help to see all features."
        ),
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
    
    await update.message.reply_text(
        "🎁 <b>STEP 1/4: Channel Selection</b>\n\n"
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
    elif chat_message.text and (chat_message.text.startswith('@') or chat_message.text.startswith('-100')):
        try:
            channel = await context.bot.get_chat(chat_message.text)
        except TelegramError:
            pass

    if not channel:
        await chat_message.reply_text("❌ Invalid channel format. Please **forward a message from the channel** or use a correct @username / -100 ID.")
        return SELECT_CHANNEL
        
    channel_id = channel.id
    channel_title = channel.title
    
    message = await update.message.reply_text(f"⏳ **Verifying admin status in {channel_title}...**", parse_mode='Markdown')
    await asyncio.sleep(1)
    
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

    # New Step: Image URL
    await update.message.reply_text(
        f"🖼️ <b>STEP 2/4: Image URL</b>\n\n"
        f"Please send the **Public URL** for the image you want to use in the giveaway post.\n"
        f"<i>(e.g., {DEFAULT_GIVEAWAY_IMAGE})</i>",
        parse_mode=ParseMode.HTML
    )
    return GET_IMAGE_URL

async def get_image_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Stores the image URL and proceeds."""
    user_id = update.effective_user.id
    image_url = update.message.text.strip()
    
    # Basic URL validation (In a real scenario, you'd check for image headers)
    if not image_url.startswith('http'):
        await update.message.reply_text("❌ Invalid URL. Please provide a full public URL starting with 'http' or 'https'.")
        return GET_IMAGE_URL

    GIVEAWAY_CREATION_DATA[user_id]['image_url'] = image_url

    # Proceed to final step
    await update.message.reply_text(
        f"🎉 **Image URL Saved!**\n\n"
        "**STEP 3/4: Launch!**\n"
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
    image_url = data['image_url'] # Use the newly captured image URL

    # 1. Save giveaway to DB
    await save_giveaway(giveaway_id, channel_id, user_id, image_url)

    # 2. Generate Deep Link
    participation_link = f"https://t.me/{BOT_USERNAME}?start={giveaway_id}"
    
    # 3. Create stylish success message with buttons
    keyboard = [
        [
            InlineKeyboardButton("✨ Channel", url=f"https://t.me/{channel_title.strip('@')}"),
            InlineKeyboardButton("🏆 View Top 10", callback_data=f"show_top10|{giveaway_id}")
        ],
        [InlineKeyboardButton("🛑 CLOSE POLL", callback_data=f"close_poll|{giveaway_id}")]
    ]
    
    # The message to be sent to the ADMIN
    admin_success_caption = (
        "✅ **VOTE-POLL CREATED SUCCESSFULLY!**\n\n"
        f"Channel: <b>{channel_title}</b>\n"
        f"Participation Link: <code>{participation_link}</code>\n\n"
        "<i>Share the link to start accepting participants!</i>"
    )
    
    # Send the final message with photo and link to the ADMIN
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
    
    # 1. CRITICAL: Check Channel Subscription
    is_subscriber = await check_user_membership(context.bot, channel_id, user.id)
    
    if not is_subscriber:
        # Send a stylish message with the image to encourage joining
        caption_text = (
            f"⚠️ <b>PARTICIPATION DENIED!</b>\n\n"
            f"You must be a <b>subscriber</b> of the giveaway channel to participate.\n"
            f"Please <b>Join Channel</b>, then click the link again."
        )
        
        await update.message.reply_photo(
            photo=image_url,
            caption=caption_text,
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
            # Send message to the channel where the giveaway is happening
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
# 7. Advanced Admin Features
# ----------------------------------------------------------------------

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
        message_text += f"{i+1}. Channel ID: <code>{channel_id}</code>\n"
        message_text += f"   ID: <code>{giveaway_id}</code>\n"
        message_text += f"   Start: <i>{start_time.split('.')[0]}</i>\n"
        message_text += f"   /close_poll_{giveaway_id}\n\n"
        
    await update.message.reply_text(message_text, parse_mode=ParseMode.HTML)

async def close_poll_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles the /close_poll command (either from inline button or direct command)."""
    if not is_admin(update.effective_user.id):
        return
        
    # Extract giveaway ID from command args or inline data
    if update.callback_query:
        query = update.callback_query
        await query.answer("Closing poll...")
        giveaway_id = query.data.split('|')[1]
        source_chat = query.message.chat
    else:
        # Command format: /close_poll_GIVEAWAYID
        if not context.args and '_' not in update.message.text:
            await update.message.reply_text("❌ Invalid command format. Use /close_poll_GIVEAWAYID.")
            return
            
        giveaway_id = update.message.text.split('_')[1]
        source_chat = update.message.chat

    # Check if poll is active
    giveaway_data = await get_giveaway_by_id(giveaway_id)
    if not giveaway_data or not giveaway_data['is_active']:
        await context.bot.send_message(source_chat.id, f"❌ Poll <code>{giveaway_id}</code> is already closed or does not exist.", parse_mode=ParseMode.HTML)
        return

    await close_giveaway_db(giveaway_id)
    
    # Optional: Announce closure in the channel (requires getting channel title)
    channel_id = giveaway_data['channel_id']
    try:
        channel_info = await context.bot.get_chat(channel_id)
        await context.bot.send_message(channel_id, f"🛑 <b>GIVEAWAY CLOSED!</b>\n\nVote-Poll ID <code>{giveaway_id}</code> has been officially closed by the admin.", parse_mode=ParseMode.HTML)
    except TelegramError:
        pass # Ignore if bot can't send to channel

    await context.bot.send_message(source_chat.id, f"✅ Poll <code>{giveaway_id}</code> successfully **CLOSED**.", parse_mode=ParseMode.HTML)

async def start_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Starts the /broadcast conversation (Admin Only)."""
    if not is_admin(update.effective_user.id):
        return
        
    # Admin must specify giveaway ID to broadcast to its participants
    if not context.args:
        await update.message.reply_text("❌ Please specify the giveaway ID: `/broadcast GIVEAWAY_ID`", parse_mode='Markdown')
        return ConversationHandler.END
        
    giveaway_id = context.args[0]
    giveaway_data = await get_giveaway_by_id(giveaway_id)
    
    if not giveaway_data:
        await update.message.reply_text(f"❌ Giveaway ID <code>{giveaway_id}</code> not found.", parse_mode=ParseMode.HTML)
        return ConversationHandler.END

    context.user_data['broadcast_id'] = giveaway_id
    
    await update.message.reply_text(
        f"📣 **BROADCAST MODE ACTIVATED** for Poll ID: <code>{giveaway_id}</code>\n\n"
        "Please send the message (text or photo/video with caption) you want to broadcast to all participants.",
        parse_mode=ParseMode.HTML
    )
    return BROADCAST_MESSAGE

async def perform_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Sends the message to all participants of a specific giveaway."""
    giveaway_id = context.user_data.pop('broadcast_id', None)
    if not giveaway_id:
        await update.message.reply_text("❌ Broadcast session expired.")
        return ConversationHandler.END

    participants = await get_all_participants_for_giveaway(giveaway_id)
    total_users = len(participants)
    success_count = 0
    
    await update.message.reply_text(f"🚀 Starting broadcast to **{total_users}** participants of <code>{giveaway_id}</code>...", parse_mode=ParseMode.HTML)

    for user_id in participants:
        try:
            if update.message.photo or update.message.video or update.message.animation:
                # Send media (photo, video, gif)
                if update.message.photo:
                    file_id = update.message.photo[-1].file_id # Highest resolution
                    await context.bot.send_photo(user_id, file_id, caption=update.message.caption, parse_mode=ParseMode.HTML)
                elif update.message.video:
                    await context.bot.send_video(user_id, update.message.video.file_id, caption=update.message.caption, parse_mode=ParseMode.HTML)
                elif update.message.animation:
                    await context.bot.send_animation(user_id, update.message.animation.file_id, caption=update.message.caption, parse_mode=ParseMode.HTML)
            else:
                # Send text message
                await context.bot.send_message(user_id, update.message.text, parse_mode=ParseMode.HTML)
            
            success_count += 1
            await asyncio.sleep(0.1) # Small delay to avoid API limits
        except TelegramError as e:
            # User might have blocked the bot, log and continue
            logger.warning(f"Broadcast failed for user {user_id}: {e}")
            
    await update.message.reply_text(
        f"✅ **BROADCAST COMPLETE!**\n\n"
        f"Sent to **{success_count}** users out of {total_users}."
    )
    return ConversationHandler.END

# ----------------------------------------------------------------------
# 8. Main Runner
# ----------------------------------------------------------------------

async def post_init(application: Application):
    """Runs after the bot is initialized, to set up the DB."""
    await init_db()

def main() -> None:
    """Starts the bot."""
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    # 1. Giveaway Conversation Handler
    giveaway_handler = ConversationHandler(
        entry_points=[CommandHandler("giveaway", start_giveaway)],
        states={
            SELECT_CHANNEL: [MessageHandler(filters.ChatType.PRIVATE & (filters.FORWARDED_FROM_CHAT | filters.Regex(r'@[a-zA-Z0-9_]+|-100\d+')), handle_channel_share)],
            GET_IMAGE_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_image_url)], # New State
            GET_DETAILS: [MessageHandler(filters.TEXT & filters.Regex(r'^(LAUNCH|launch)$'), handle_details_and_publish)],
        },
        fallbacks=[CommandHandler("cancel", cancel_giveaway)]
    )
    
    # 2. Broadcast Conversation Handler
    broadcast_handler = ConversationHandler(
        entry_points=[CommandHandler("broadcast", start_broadcast)],
        states={
            BROADCAST_MESSAGE: [MessageHandler(filters.ALL & ~filters.COMMAND, perform_broadcast)],
        },
        fallbacks=[CommandHandler("cancel", lambda update, context: (update.message.reply_text("Broadcast cancelled."), ConversationHandler.END))]
    )

    # 3. Command Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(CommandHandler("active_polls", active_polls))
    
    # 4. Add Giveaway Handlers
    application.add_handler(giveaway_handler)
    application.add_handler(broadcast_handler)
    
    # 5. Callback Query Handler (Handles Top 10, Close Poll, Tutorial)
    # The pattern handles show_top10|ID and close_poll|ID
    application.add_handler(CallbackQueryHandler(close_poll_handler, pattern=r"^close_poll\|"))
    application.add_handler(CallbackQueryHandler(lambda update, context: update.callback_query.answer("Feature not implemented in this version!"), pattern=r"show_top10\|"))
    application.add_handler(CallbackQueryHandler(lambda update, context: update.callback_query.answer("Watch the tutorial here!", url="https://youtube.com/your_bot_tutorial"), pattern="tutorial_video"))

    # 6. Command for /close_poll_GIVEAWAYID
    application.add_handler(CommandHandler("close_poll", close_poll_handler, filters=filters.Regex(r'/close_poll_[a-zA-Z0-9]+')))


    # Start the Bot
    logger.info(f"Bot @{BOT_USERNAME} started successfully. Polling for updates...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
