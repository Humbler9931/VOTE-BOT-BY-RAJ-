import os
import re
import logging
import asyncio
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Chat
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    CallbackQueryHandler
)
from telegram.constants import ChatMemberStatus, ParseMode
from collections import defaultdict
from telegram.error import BadRequest, Forbidden, TelegramError, RetryAfter, TimedOut
from typing import Tuple, Optional, Dict, List
import traceback

load_dotenv()

# ============================================================================
# CONFIGURATION
# ============================================================================

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
IMAGE_URL = os.getenv("IMAGE_URL", "https://i.ibb.co/VJKdYpt/photo-2024-10-04-08-33-32.jpg")
LOG_CHANNEL = os.getenv("LOG_CHANNEL_USERNAME", "@teamrajweb")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not found!")

GET_CHANNEL_ID = 1

# ============================================================================
# DATA STRUCTURES
# ============================================================================

VOTES_DATA: Dict[int, Dict[int, Dict[int, datetime]]] = defaultdict(lambda: defaultdict(dict))
MEMBERSHIP_CACHE: Dict[int, Dict[int, Tuple[bool, datetime]]] = defaultdict(dict)
CHANNEL_CACHE: Dict[int, Chat] = {}
MESSAGE_LOCKS: Dict[Tuple[int, int], asyncio.Lock] = {}
RATE_LIMIT_LOCK = asyncio.Lock()
LAST_API_CALL: Dict[str, datetime] = {}

CACHE_TTL = timedelta(minutes=2)
API_DELAY = 0.05  # 50ms between API calls

# ============================================================================
# RATE LIMITING
# ============================================================================

async def rate_limit(key: str = "default"):
    """Enforce rate limiting for API calls."""
    async with RATE_LIMIT_LOCK:
        now = datetime.now()
        if key in LAST_API_CALL:
            elapsed = (now - LAST_API_CALL[key]).total_seconds()
            if elapsed < API_DELAY:
                await asyncio.sleep(API_DELAY - elapsed)
        LAST_API_CALL[key] = datetime.now()

def get_message_lock(channel_id: int, message_id: int) -> asyncio.Lock:
    """Get or create lock for specific message."""
    key = (channel_id, message_id)
    if key not in MESSAGE_LOCKS:
        MESSAGE_LOCKS[key] = asyncio.Lock()
    return MESSAGE_LOCKS[key]

# ============================================================================
# SAFE API CALLS
# ============================================================================

async def safe_api_call(func, *args, max_retries=3, **kwargs):
    """Execute API call with retry logic."""
    for attempt in range(max_retries):
        try:
            await rate_limit(f"{func.__name__}")
            return await func(*args, **kwargs)
        except RetryAfter as e:
            if attempt < max_retries - 1:
                logger.warning(f"Rate limited, waiting {e.retry_after}s")
                await asyncio.sleep(e.retry_after)
            else:
                raise
        except TimedOut:
            if attempt < max_retries - 1:
                logger.warning(f"Timeout, retrying... ({attempt + 1}/{max_retries})")
                await asyncio.sleep(1)
            else:
                raise
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            await asyncio.sleep(0.5)

# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def parse_channel_id(payload: str) -> Optional[int]:
    """Extract channel ID from deep link."""
    try:
        match = re.match(r'link_(\d+)', payload)
        if match:
            return int(f"-100{match.group(1)}")
    except:
        pass
    return None

def create_share_link(bot_username: str, channel_id: int) -> str:
    """Generate share link."""
    raw = str(channel_id)
    link_id = raw[4:] if raw.startswith('-100') else raw.replace('-', '')
    return f"https://t.me/{bot_username}?start=link_{link_id}"

async def get_channel_safe(context: ContextTypes.DEFAULT_TYPE, channel_id: int) -> Optional[Chat]:
    """Get channel info with caching."""
    if channel_id in CHANNEL_CACHE:
        return CHANNEL_CACHE[channel_id]
    
    try:
        chat = await safe_api_call(context.bot.get_chat, chat_id=channel_id)
        CHANNEL_CACHE[channel_id] = chat
        return chat
    except Exception as e:
        logger.error(f"Failed to get channel {channel_id}: {e}")
        return None

# ============================================================================
# MEMBERSHIP CHECK
# ============================================================================

async def check_membership(context: ContextTypes.DEFAULT_TYPE, channel_id: int, user_id: int, force: bool = False) -> bool:
    """Check membership with caching."""
    now = datetime.now()
    
    if not force and user_id in MEMBERSHIP_CACHE[channel_id]:
        is_member, last_check = MEMBERSHIP_CACHE[channel_id][user_id]
        if now - last_check < CACHE_TTL:
            return is_member
    
    try:
        member = await safe_api_call(
            context.bot.get_chat_member,
            chat_id=channel_id,
            user_id=user_id
        )
        is_member = member.status in [
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER
        ]
        MEMBERSHIP_CACHE[channel_id][user_id] = (is_member, now)
        return is_member
    except Exception as e:
        logger.warning(f"Membership check failed: {e}")
        MEMBERSHIP_CACHE[channel_id][user_id] = (False, now)
        return False

# ============================================================================
# VOTE MANAGEMENT
# ============================================================================

def get_vote_count(channel_id: int, message_id: int) -> int:
    """Get vote count."""
    return len(VOTES_DATA[channel_id][message_id])

def has_voted(channel_id: int, message_id: int, user_id: int) -> bool:
    """Check if voted."""
    return user_id in VOTES_DATA[channel_id][message_id]

def add_vote(channel_id: int, message_id: int, user_id: int) -> int:
    """Add vote."""
    VOTES_DATA[channel_id][message_id][user_id] = datetime.now()
    return get_vote_count(channel_id, message_id)

def remove_vote(channel_id: int, message_id: int, user_id: int) -> int:
    """Remove vote."""
    if user_id in VOTES_DATA[channel_id][message_id]:
        del VOTES_DATA[channel_id][message_id][user_id]
    return get_vote_count(channel_id, message_id)

# ============================================================================
# MARKUP FUNCTIONS
# ============================================================================

def create_vote_markup(channel_id: int, message_id: int, count: int, url: Optional[str]) -> InlineKeyboardMarkup:
    """Create vote button."""
    keyboard = [[
        InlineKeyboardButton(f"✅ Vote ({count})", callback_data=f"v_{channel_id}_{message_id}")
    ]]
    if url:
        keyboard.append([InlineKeyboardButton("➡️ Visit Channel", url=url)])
    return InlineKeyboardMarkup(keyboard)

async def update_markup_safe(context: ContextTypes.DEFAULT_TYPE, channel_id: int, message_id: int, count: int):
    """Update markup safely with lock."""
    lock = get_message_lock(channel_id, message_id)
    
    async with lock:
        try:
            chat = await get_channel_safe(context, channel_id)
            if not chat:
                return
            
            url = None
            if chat.username:
                url = f"https://t.me/{chat.username}"
            elif chat.invite_link:
                url = chat.invite_link
            
            markup = create_vote_markup(channel_id, message_id, count, url)
            
            await safe_api_call(
                context.bot.edit_message_reply_markup,
                chat_id=channel_id,
                message_id=message_id,
                reply_markup=markup
            )
            logger.info(f"Updated markup: msg={message_id}, count={count}")
            
        except BadRequest as e:
            if "message is not modified" not in str(e).lower():
                logger.debug(f"Markup update skipped: {e}")
        except Exception as e:
            logger.error(f"Markup update failed: {e}")

# ============================================================================
# PERMISSION CHECK
# ============================================================================

async def verify_bot_admin(context: ContextTypes.DEFAULT_TYPE, channel_id: int) -> Tuple[bool, str]:
    """Verify bot admin status."""
    try:
        bot = await context.bot.get_me()
        member = await safe_api_call(
            context.bot.get_chat_member,
            chat_id=channel_id,
            user_id=bot.id
        )
        
        if member.status not in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
            return False, "Bot is not admin"
        
        if not member.can_post_messages:
            return False, "Missing 'Post Messages' permission"
        
        if not member.can_restrict_members:
            return False, "Missing 'Manage Users' permission"
        
        return True, ""
    except Exception as e:
        return False, str(e)

# ============================================================================
# COMMAND HANDLERS
# ============================================================================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start."""
    user = update.effective_user
    bot = await context.bot.get_me()
    
    if context.args:
        channel_id = parse_channel_id(context.args[0])
        if channel_id:
            await handle_deep_link(update, context, channel_id, bot.username)
            return
    
    keyboard = [
        [InlineKeyboardButton("🔗 Create Link", callback_data='create')],
        [InlineKeyboardButton("📊 My Votes", callback_data='votes'),
         InlineKeyboardButton("❓ Help", callback_data='help')]
    ]
    
    text = (
        "**👑 Advanced Voting Bot**\n\n"
        "✅ Instant channel links\n"
        "✅ Auto subscription check\n"
        "✅ One vote per user\n"
        "✅ Auto removal on leave\n\n"
        "Click **Create Link** to start!"
    )
    
    try:
        await update.message.reply_photo(
            photo=IMAGE_URL,
            caption=text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except:
        await update.message.reply_text(
            text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def handle_deep_link(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: int, bot_username: str):
    """Handle deep link click."""
    user = update.effective_user
    
    try:
        chat = await get_channel_safe(context, channel_id)
        if not chat:
            await update.message.reply_text("❌ Channel not found.")
            return
        
        await update.message.reply_text(
            f"✨ **Welcome!**\n\n"
            f"Connected to: **{chat.title}**\n\n"
            f"To vote:\n"
            f"1. Join the channel\n"
            f"2. Click 'Vote' on posts\n"
            f"3. Stay subscribed!\n\n"
            f"*Note: Votes auto-remove if you leave*",
            parse_mode=ParseMode.MARKDOWN
        )
        
        url = None
        if chat.username:
            url = f"https://t.me/{chat.username}"
        elif chat.invite_link:
            url = chat.invite_link
        
        notification = (
            f"**🎉 New Participant**\n\n"
            f"👤 User: [{user.first_name}](tg://user?id={user.id})\n"
            f"🆔 ID: `{user.id}`\n"
            f"📅 {datetime.now().strftime('%d %b, %I:%M %p')}"
        )
        
        markup = create_vote_markup(channel_id, 0, 0, url)
        
        msg = await safe_api_call(
            context.bot.send_photo,
            chat_id=channel_id,
            photo=IMAGE_URL,
            caption=notification,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=markup
        )
        
        # Update with correct message ID
        await asyncio.sleep(0.1)
        final_markup = create_vote_markup(channel_id, msg.message_id, 0, url)
        await safe_api_call(
            context.bot.edit_message_reply_markup,
            chat_id=channel_id,
            message_id=msg.message_id,
            reply_markup=final_markup
        )
        
    except Exception as e:
        logger.error(f"Deep link error: {e}\n{traceback.format_exc()}")
        await update.message.reply_text("❌ Error occurred. Please try again.")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show status."""
    total_votes = sum(len(v) for m in VOTES_DATA.values() for v in m.values())
    total_users = len(set(u for m in VOTES_DATA.values() for v in m.values() for u in v.keys()))
    
    text = (
        f"**🤖 Bot Status**\n\n"
        f"📺 Channels: {len(CHANNEL_CACHE)}\n"
        f"🗳️ Votes: {total_votes}\n"
        f"👥 Users: {total_users}\n"
        f"💾 Cache: {sum(len(c) for c in MEMBERSHIP_CACHE.values())}\n\n"
        f"Status: 🟢 Online"
    )
    
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

# ============================================================================
# CALLBACK HANDLERS
# ============================================================================

async def vote_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle vote clicks."""
    query = update.callback_query
    user_id = query.from_user.id
    
    try:
        parts = query.data.split('_')
        channel_id = int(parts[1])
        message_id = int(parts[2])
    except:
        await query.answer("❌ Invalid vote", show_alert=True)
        return
    
    if has_voted(channel_id, message_id, user_id):
        await query.answer("🗳️ Already voted!", show_alert=True)
        return
    
    is_member = await check_membership(context, channel_id, user_id)
    
    if not is_member:
        # Double check
        is_member = await check_membership(context, channel_id, user_id, force=True)
        if not is_member:
            await query.answer("❌ Join channel first!", show_alert=True)
            return
    
    count = add_vote(channel_id, message_id, user_id)
    await update_markup_safe(context, channel_id, message_id, count)
    await query.answer(f"✅ Vote #{count} registered!", show_alert=True)
    
    # Schedule verification
    asyncio.create_task(verify_vote_later(context, channel_id, message_id, user_id))

async def verify_vote_later(context: ContextTypes.DEFAULT_TYPE, channel_id: int, message_id: int, user_id: int):
    """Verify vote after delay."""
    await asyncio.sleep(120)  # Check after 2 minutes
    
    is_member = await check_membership(context, channel_id, user_id, force=True)
    
    if not is_member and has_voted(channel_id, message_id, user_id):
        count = remove_vote(channel_id, message_id, user_id)
        await update_markup_safe(context, channel_id, message_id, count)
        logger.info(f"Removed vote: user {user_id} left channel")

async def votes_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user votes."""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    votes = [(c, m) for c in VOTES_DATA for m in VOTES_DATA[c] if user_id in VOTES_DATA[c][m]]
    
    if not votes:
        text = "📊 **Your Votes**\n\nNo votes yet!"
    else:
        text = f"📊 **Your Votes**\n\nTotal: {len(votes)}\n\n"
        for c, m in votes[:5]:
            chat = await get_channel_safe(context, c)
            if chat:
                text += f"• {chat.title}\n"
    
    await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)

async def help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show help."""
    query = update.callback_query
    await query.answer()
    
    text = (
        "**📚 How to Use**\n\n"
        "**Admins:**\n"
        "1. Click 'Create Link'\n"
        "2. Send channel @username/ID\n"
        "3. Bot must be admin with:\n"
        "   • Post Messages\n"
        "   • Manage Users\n"
        "4. Share link!\n\n"
        "**Users:**\n"
        "1. Click shared link\n"
        "2. Join channel\n"
        "3. Vote on posts\n\n"
        "*Votes auto-remove if you leave*"
    )
    
    await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)

# ============================================================================
# CONVERSATION
# ============================================================================

async def create_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start link creation."""
    query = update.callback_query
    await query.answer()
    
    text = (
        "**🔗 Channel Setup**\n\n"
        "Send your channel @username or ID\n\n"
        "**Requirements:**\n"
        "✅ Bot must be admin\n"
        "✅ Post Messages permission\n"
        "✅ Manage Users permission\n\n"
        "Send /cancel to abort"
    )
    
    await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)
    return GET_CHANNEL_ID

async def create_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process channel input."""
    channel_input = update.message.text.strip()
    
    if re.match(r'^-?\d+$', channel_input):
        channel_id = int(channel_input)
    elif channel_input.startswith('@'):
        channel_id = channel_input
    else:
        channel_id = f"@{channel_input}"
    
    is_admin, error = await verify_bot_admin(context, channel_id)
    
    if not is_admin:
        await update.message.reply_text(
            f"❌ **Error**\n\n{error}\n\n"
            "Add bot as admin with required permissions.",
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END
    
    chat = await get_channel_safe(context, channel_id)
    if not chat:
        await update.message.reply_text("❌ Cannot access channel.")
        return ConversationHandler.END
    
    bot = await context.bot.get_me()
    link = create_share_link(bot.username, chat.id)
    
    text = (
        f"✅ **Connected!**\n\n"
        f"📺 {chat.title}\n\n"
        f"🔗 **Share Link:**\n"
        f"`{link}`\n\n"
        f"Share this link to get votes!"
    )
    
    keyboard = [[InlineKeyboardButton("📤 Share", url=link)]]
    
    await update.message.reply_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    
    return ConversationHandler.END

async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel operation."""
    await update.message.reply_text("❌ Cancelled. Use /start")
    return ConversationHandler.END

# ============================================================================
# ERROR HANDLER
# ============================================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors."""
    logger.error(f"Error: {context.error}\n{traceback.format_exc()}")
    
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ Error occurred. Please try again.\nContact: @teamrajweb"
            )
        except:
            pass

# ============================================================================
# BACKGROUND TASKS
# ============================================================================

async def cleanup_task(app: Application):
    """Cleanup old cache."""
    while True:
        try:
            await asyncio.sleep(300)
            now = datetime.now()
            cleaned = 0
            
            for c in list(MEMBERSHIP_CACHE.keys()):
                for u in list(MEMBERSHIP_CACHE[c].keys()):
                    _, t = MEMBERSHIP_CACHE[c][u]
                    if now - t > CACHE_TTL * 2:
                        del MEMBERSHIP_CACHE[c][u]
                        cleaned += 1
            
            if cleaned > 0:
                logger.info(f"Cleaned {cleaned} cache entries")
        except Exception as e:
            logger.error(f"Cleanup error: {e}")

# ============================================================================
# MAIN
# ============================================================================

def main():
    """Start bot."""
    logger.info("=" * 50)
    logger.info("ADVANCED VOTING BOT STARTING")
    logger.info("=" * 50)
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    
    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(create_start, pattern='^create$')],
        states={GET_CHANNEL_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_process)]},
        fallbacks=[CommandHandler('cancel', cancel_cmd)]
    )
    app.add_handler(conv)
    
    app.add_handler(CallbackQueryHandler(vote_callback, pattern=r'^v_-?\d+_\d+$'))
    app.add_handler(CallbackQueryHandler(votes_callback, pattern='^votes$'))
    app.add_handler(CallbackQueryHandler(help_callback, pattern='^help$'))
    
    app.add_error_handler(error_handler)
    
    # Start cleanup task
    loop = asyncio.get_event_loop()
    loop.create_task(cleanup_task(app))
    
    logger.info("✅ Bot started successfully!")
    logger.info("=" * 50)
    
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == '__main__':
    main()
