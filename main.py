import os
import re
import logging
import asyncio
from datetime import datetime, timedelta
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Chat
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    CallbackQueryHandler,
    Application
)
from telegram.constants import ChatMemberStatus, ParseMode
from collections import defaultdict
from telegram.error import BadRequest, Forbidden, TelegramError
from typing import Tuple, Optional, Dict, List, Any
import json

# Load environment variables
load_dotenv()

# ============================================================================
# CONFIGURATION & LOGGING
# ============================================================================

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Environment Variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
IMAGE_URL = os.getenv("IMAGE_URL", "https://picsum.photos/600/300")
LOG_CHANNEL_USERNAME = os.getenv("LOG_CHANNEL_USERNAME", "@teamrajweb")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN not found in environment variables!")

# Conversation States
GET_CHANNEL_ID = 1

# ============================================================================
# GLOBAL DATA STRUCTURES
# ============================================================================

# Vote tracking: {channel_id: {message_id: {user_id: timestamp}}}
VOTES_DATA: Dict[int, Dict[int, Dict[int, datetime]]] = defaultdict(lambda: defaultdict(dict))

# Membership cache: {channel_id: {user_id: (is_member, last_check)}}
MEMBERSHIP_CACHE: Dict[int, Dict[int, Tuple[bool, datetime]]] = defaultdict(dict)
CACHE_DURATION = timedelta(minutes=3)

# Channel info cache: {channel_id: Chat}
CHANNEL_CACHE: Dict[int, Chat] = {}

# Active links: {channel_id: share_url}
ACTIVE_LINKS: Dict[int, str] = {}

# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def format_user_link(user) -> str:
    """Create formatted user mention link."""
    name = user.first_name or "User"
    return f"[{name}](tg://user?id={user.id})"

def get_channel_id_from_payload(payload: str) -> Optional[int]:
    """Extract numeric channel ID from deep link payload."""
    try:
        match = re.match(r'link_(\d+)', payload)
        if match:
            channel_id_str = match.group(1)
            return int(f"-100{channel_id_str}")
    except Exception as e:
        logger.error(f"Error parsing payload {payload}: {e}")
    return None

def create_share_link(bot_username: str, channel_id: int) -> str:
    """Generate shareable deep link for channel."""
    raw_id = str(channel_id)
    link_id = raw_id[4:] if raw_id.startswith('-100') else raw_id.replace('-', '')
    return f"https://t.me/{bot_username}?start=link_{link_id}"

async def get_channel_info(context: ContextTypes.DEFAULT_TYPE, channel_id: int) -> Optional[Chat]:
    """Get channel info with caching."""
    if channel_id in CHANNEL_CACHE:
        return CHANNEL_CACHE[channel_id]
    
    try:
        chat = await context.bot.get_chat(chat_id=channel_id)
        CHANNEL_CACHE[channel_id] = chat
        return chat
    except Exception as e:
        logger.error(f"Failed to get channel info for {channel_id}: {e}")
        return None

def get_channel_url(chat: Chat) -> Optional[str]:
    """Get channel URL from Chat object."""
    if chat.username:
        return f"https://t.me/{chat.username}"
    return chat.invite_link

# ============================================================================
# MEMBERSHIP VERIFICATION
# ============================================================================

async def check_membership(context: ContextTypes.DEFAULT_TYPE, channel_id: int, user_id: int, force_refresh: bool = False) -> bool:
    """
    Check if user is member of channel with intelligent caching.
    Returns True if user is member, False otherwise.
    """
    current_time = datetime.now()
    
    # Check cache first
    if not force_refresh and user_id in MEMBERSHIP_CACHE[channel_id]:
        is_member, last_check = MEMBERSHIP_CACHE[channel_id][user_id]
        if current_time - last_check < CACHE_DURATION:
            logger.debug(f"Cache hit: User {user_id} in channel {channel_id} = {is_member}")
            return is_member
    
    # Perform actual API check
    try:
        member = await context.bot.get_chat_member(chat_id=channel_id, user_id=user_id)
        is_member = member.status in [
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER
        ]
        
        # Update cache
        MEMBERSHIP_CACHE[channel_id][user_id] = (is_member, current_time)
        logger.info(f"Membership check: User {user_id} in channel {channel_id} = {is_member}")
        return is_member
        
    except (Forbidden, BadRequest) as e:
        logger.warning(f"Cannot check membership for user {user_id} in {channel_id}: {e}")
        # Cache as not member
        MEMBERSHIP_CACHE[channel_id][user_id] = (False, current_time)
        return False
    except Exception as e:
        logger.error(f"Unexpected error checking membership: {e}")
        return False

def invalidate_cache(channel_id: int, user_id: int):
    """Remove user from membership cache."""
    if user_id in MEMBERSHIP_CACHE[channel_id]:
        del MEMBERSHIP_CACHE[channel_id][user_id]
        logger.debug(f"Invalidated cache for user {user_id} in channel {channel_id}")

# ============================================================================
# VOTE MANAGEMENT
# ============================================================================

def get_vote_count(channel_id: int, message_id: int) -> int:
    """Get total votes for a message."""
    return len(VOTES_DATA[channel_id][message_id])

def has_voted(channel_id: int, message_id: int, user_id: int) -> bool:
    """Check if user has already voted."""
    return user_id in VOTES_DATA[channel_id][message_id]

def add_vote(channel_id: int, message_id: int, user_id: int) -> int:
    """Add vote and return new count."""
    VOTES_DATA[channel_id][message_id][user_id] = datetime.now()
    return get_vote_count(channel_id, message_id)

def remove_vote(channel_id: int, message_id: int, user_id: int) -> int:
    """Remove vote and return new count."""
    if user_id in VOTES_DATA[channel_id][message_id]:
        del VOTES_DATA[channel_id][message_id][user_id]
    return get_vote_count(channel_id, message_id)

def get_user_votes(user_id: int) -> List[Tuple[int, int]]:
    """Get all votes by user as list of (channel_id, message_id)."""
    votes = []
    for channel_id, messages in VOTES_DATA.items():
        for message_id, voters in messages.items():
            if user_id in voters:
                votes.append((channel_id, message_id))
    return votes

# ============================================================================
# MARKUP CREATION
# ============================================================================

def create_vote_button(channel_id: int, message_id: int, vote_count: int, channel_url: Optional[str] = None) -> InlineKeyboardMarkup:
    """Create vote button markup."""
    keyboard = []
    
    # Vote button
    vote_text = f"✅ Vote Now ({vote_count})"
    vote_callback = f"vote_{channel_id}_{message_id}"
    keyboard.append([InlineKeyboardButton(vote_text, callback_data=vote_callback)])
    
    # Channel button
    if channel_url:
        keyboard.append([InlineKeyboardButton("➡️ Visit Channel", url=channel_url)])
    
    return InlineKeyboardMarkup(keyboard)

async def update_vote_button(context: ContextTypes.DEFAULT_TYPE, channel_id: int, message_id: int, new_count: int):
    """Update vote button with new count."""
    try:
        # Get channel info
        chat = await get_channel_info(context, channel_id)
        if not chat:
            return
        
        channel_url = get_channel_url(chat)
        new_markup = create_vote_button(channel_id, message_id, new_count, channel_url)
        
        # Update message
        await context.bot.edit_message_reply_markup(
            chat_id=channel_id,
            message_id=message_id,
            reply_markup=new_markup
        )
        logger.info(f"Updated vote button for message {message_id} to {new_count} votes")
        
    except BadRequest as e:
        if "message is not modified" in str(e).lower():
            logger.debug("Message markup unchanged")
        elif "message to edit not found" in str(e).lower():
            logger.warning(f"Message {message_id} not found")
        else:
            logger.error(f"Failed to update markup: {e}")
    except Exception as e:
        logger.error(f"Error updating vote button: {e}")

# ============================================================================
# PERMISSION CHECKS
# ============================================================================

async def verify_bot_admin(context: ContextTypes.DEFAULT_TYPE, channel_id: int) -> Tuple[bool, str]:
    """
    Verify bot has admin permissions in channel.
    Returns (is_admin, error_message)
    """
    try:
        bot = await context.bot.get_me()
        member = await context.bot.get_chat_member(chat_id=channel_id, user_id=bot.id)
        
        if member.status not in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
            return False, "Bot is not an administrator in the channel"
        
        # Check required permissions
        if not (member.can_post_messages and member.can_restrict_members):
            return False, "Bot needs 'Post Messages' and 'Manage Users' permissions"
        
        return True, ""
        
    except (Forbidden, BadRequest) as e:
        return False, f"Cannot access channel: {e}"
    except Exception as e:
        return False, f"Unexpected error: {e}"

# ============================================================================
# BACKGROUND TASKS
# ============================================================================

async def verify_votes_background(context: ContextTypes.DEFAULT_TYPE):
    """Background task to verify votes and remove invalid ones."""
    while True:
        try:
            await asyncio.sleep(180)  # Check every 3 minutes
            
            logger.info("Starting vote verification cycle...")
            removed_count = 0
            
            for channel_id, messages in list(VOTES_DATA.items()):
                for message_id, voters in list(messages.items()):
                    for user_id in list(voters.keys()):
                        # Force refresh membership
                        is_member = await check_membership(context, channel_id, user_id, force_refresh=True)
                        
                        if not is_member:
                            # Remove vote
                            new_count = remove_vote(channel_id, message_id, user_id)
                            await update_vote_button(context, channel_id, message_id, new_count)
                            removed_count += 1
                            logger.info(f"Removed vote: user {user_id} left channel {channel_id}")
                        
                        # Small delay to avoid rate limits
                        await asyncio.sleep(0.1)
            
            if removed_count > 0:
                logger.info(f"Vote verification complete: {removed_count} votes removed")
            
        except Exception as e:
            logger.error(f"Error in vote verification: {e}")

async def cleanup_cache_background(context: ContextTypes.DEFAULT_TYPE):
    """Background task to clean old cache entries."""
    while True:
        try:
            await asyncio.sleep(600)  # Clean every 10 minutes
            
            current_time = datetime.now()
            cleaned = 0
            
            for channel_id in list(MEMBERSHIP_CACHE.keys()):
                for user_id in list(MEMBERSHIP_CACHE[channel_id].keys()):
                    _, last_check = MEMBERSHIP_CACHE[channel_id][user_id]
                    if current_time - last_check > CACHE_DURATION * 2:
                        del MEMBERSHIP_CACHE[channel_id][user_id]
                        cleaned += 1
            
            if cleaned > 0:
                logger.info(f"Cache cleanup: removed {cleaned} old entries")
                
        except Exception as e:
            logger.error(f"Error in cache cleanup: {e}")

# ============================================================================
# COMMAND HANDLERS
# ============================================================================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command and deep links."""
    user = update.effective_user
    bot = await context.bot.get_me()
    
    logger.info(f"User {user.id} ({user.username}) started bot with args: {context.args}")
    
    # Handle deep link
    if context.args:
        payload = context.args[0]
        channel_id = get_channel_id_from_payload(payload)
        
        if channel_id:
            await handle_deep_link(update, context, channel_id, bot.username)
            return
    
    # Regular start menu
    keyboard = [
        [
            InlineKeyboardButton("🔗 Create Channel Link", callback_data='create_link'),
            InlineKeyboardButton("➕ Add to Group", url=f"https://t.me/{bot.username}?startgroup=true")
        ],
        [
            InlineKeyboardButton("📊 My Votes", callback_data='my_votes'),
            InlineKeyboardButton("❓ Help", callback_data='help')
        ],
        [
            InlineKeyboardButton("📢 Updates", url='https://t.me/narzoxbot')
        ]
    ]
    
    welcome_text = (
        "**👑 Advanced Voting Bot**\n\n"
        "**Features:**\n"
        "✅ Instant channel links\n"
        "✅ Auto subscription check\n"
        "✅ One vote per user\n"
        "✅ Auto vote removal on leave\n"
        "✅ Real-time tracking\n\n"
        "Click **Create Channel Link** to get started!"
    )
    
    try:
        await update.message.reply_photo(
            photo=IMAGE_URL,
            caption=welcome_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    except Exception as e:
        logger.error(f"Failed to send photo: {e}")
        await update.message.reply_text(
            welcome_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

async def handle_deep_link(update: Update, context: ContextTypes.DEFAULT_TYPE, channel_id: int, bot_username: str):
    """Handle user clicking deep link."""
    user = update.effective_user
    
    try:
        # Get channel info
        chat = await get_channel_info(context, channel_id)
        if not chat:
            await update.message.reply_text("❌ Channel not found or bot removed from channel.")
            return
        
        channel_url = get_channel_url(chat)
        
        # Welcome message to user
        await update.message.reply_text(
            f"✨ **Welcome!**\n\n"
            f"You've connected to **{chat.title}**\n\n"
            f"To vote on posts:\n"
            f"1. Join the channel\n"
            f"2. Find posts with 'Vote Now' button\n"
            f"3. Click to cast your vote\n\n"
            f"**Note:** You can only vote if you're a channel member!",
            parse_mode=ParseMode.MARKDOWN
        )
        
        # Create notification post in channel
        notification = (
            f"**🎉 New Participant Joined!**\n\n"
            f"👤 User: {format_user_link(user)}\n"
            f"🆔 ID: `{user.id}`\n"
            f"👥 Username: {f'@{user.username}' if user.username else 'None'}\n"
            f"📅 Time: {datetime.now().strftime('%d %b %Y, %I:%M %p')}\n\n"
            f"🤖 Via: @{bot_username}"
        )
        
        # Send notification with vote button
        initial_count = 0
        markup = create_vote_button(channel_id, 0, initial_count, channel_url)
        
        try:
            msg = await context.bot.send_photo(
                chat_id=channel_id,
                photo=IMAGE_URL,
                caption=notification,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=markup
            )
            
            # Update markup with correct message ID
            new_markup = create_vote_button(channel_id, msg.message_id, initial_count, channel_url)
            await context.bot.edit_message_reply_markup(
                chat_id=channel_id,
                message_id=msg.message_id,
                reply_markup=new_markup
            )
            
            logger.info(f"Posted notification in channel {channel_id} for user {user.id}")
            
        except (Forbidden, BadRequest) as e:
            logger.error(f"Failed to post in channel {channel_id}: {e}")
            await update.message.reply_text(
                "⚠️ Could not post notification in channel. "
                "Bot may not have posting permissions."
            )
        
    except Exception as e:
        logger.error(f"Deep link error: {e}")
        await update.message.reply_text(
            "❌ An error occurred. Please try again or contact support."
        )

async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show bot status."""
    total_channels = len(CHANNEL_CACHE)
    total_votes = sum(
        len(voters)
        for messages in VOTES_DATA.values()
        for voters in messages.values()
    )
    total_users = len(set(
        user_id
        for messages in VOTES_DATA.values()
        for voters in messages.values()
        for user_id in voters.keys()
    ))
    cache_size = sum(len(users) for users in MEMBERSHIP_CACHE.values())
    
    status_text = (
        f"**🤖 Bot Status**\n\n"
        f"**Statistics:**\n"
        f"📺 Channels: {total_channels}\n"
        f"🗳️ Total Votes: {total_votes}\n"
        f"👥 Active Users: {total_users}\n"
        f"💾 Cache Entries: {cache_size}\n\n"
        f"**Status:** 🟢 Online\n"
        f"**Health:** ✅ All systems operational"
    )
    
    await update.message.reply_text(status_text, parse_mode=ParseMode.MARKDOWN)

# ============================================================================
# CALLBACK HANDLERS
# ============================================================================

async def handle_vote_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle vote button clicks."""
    query = update.callback_query
    user_id = query.from_user.id
    
    # Parse callback data
    try:
        parts = query.data.split('_')
        if len(parts) != 3:
            await query.answer("❌ Invalid vote data", show_alert=True)
            return
        
        channel_id = int(parts[1])
        message_id = int(parts[2])
        
    except (ValueError, IndexError) as e:
        logger.error(f"Failed to parse vote callback: {query.data}, error: {e}")
        await query.answer("❌ Invalid vote format", show_alert=True)
        return
    
    logger.info(f"Vote attempt: user={user_id}, channel={channel_id}, message={message_id}")
    
    # Check if already voted
    if has_voted(channel_id, message_id, user_id):
        await query.answer("🗳️ You've already voted on this post!", show_alert=True)
        return
    
    # Check membership
    is_member = await check_membership(context, channel_id, user_id, force_refresh=False)
    
    if not is_member:
        # Double check with fresh API call
        invalidate_cache(channel_id, user_id)
        is_member = await check_membership(context, channel_id, user_id, force_refresh=True)
        
        if not is_member:
            await query.answer(
                "❌ You must join the channel first to vote!",
                show_alert=True
            )
            return
    
    # Register vote
    new_count = add_vote(channel_id, message_id, user_id)
    
    # Update button
    await update_vote_button(context, channel_id, message_id, new_count)
    
    # Success feedback
    await query.answer(f"✅ Vote #{new_count} registered! Thank you!", show_alert=True)
    logger.info(f"Vote registered: user={user_id}, new_count={new_count}")

async def my_votes_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user's votes."""
    query = update.callback_query
    await query.answer()
    
    user_id = query.from_user.id
    votes = get_user_votes(user_id)
    
    if not votes:
        text = "📊 **Your Votes**\n\nYou haven't voted on any posts yet!"
    else:
        text = f"📊 **Your Votes**\n\nTotal: {len(votes)} vote(s)\n\n"
        
        for channel_id, message_id in votes[:10]:  # Show max 10
            chat = await get_channel_info(context, channel_id)
            if chat:
                text += f"• {chat.title} (ID: {message_id})\n"
        
        if len(votes) > 10:
            text += f"\n... and {len(votes) - 10} more"
    
    await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)

async def help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show help message."""
    query = update.callback_query
    await query.answer()
    
    help_text = (
        "**📚 How to Use**\n\n"
        "**For Channel Admins:**\n"
        "1. Click 'Create Channel Link'\n"
        "2. Send your channel @username or ID\n"
        "3. Bot must be admin with these permissions:\n"
        "   • Post Messages\n"
        "   • Manage Users\n"
        "4. Share the generated link!\n\n"
        "**For Users:**\n"
        "1. Click shared link\n"
        "2. Join the channel\n"
        "3. Vote on posts\n"
        "4. Stay subscribed (votes auto-remove if you leave)\n\n"
        "**Commands:**\n"
        "/start - Main menu\n"
        "/status - Bot statistics\n"
        "/cancel - Cancel operation"
    )
    
    await query.edit_message_text(help_text, parse_mode=ParseMode.MARKDOWN)

# ============================================================================
# CONVERSATION HANDLER
# ============================================================================

async def create_link_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start link creation conversation."""
    query = update.callback_query
    await query.answer()
    
    text = (
        "**🔗 Channel Link Setup**\n\n"
        "Send me your channel's @username or ID (e.g., `-1001234567890`)\n\n"
        "**Requirements:**\n"
        "✅ Bot must be channel admin\n"
        "✅ 'Post Messages' permission\n"
        "✅ 'Manage Users' permission\n\n"
        "Send /cancel to abort"
    )
    
    await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN)
    return GET_CHANNEL_ID

async def create_link_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process channel ID and create link."""
    channel_input = update.message.text.strip()
    user = update.effective_user
    
    # Parse channel ID
    if re.match(r'^-?\d+$', channel_input):
        channel_id = int(channel_input)
    elif channel_input.startswith('@'):
        channel_id = channel_input
    else:
        channel_id = f"@{channel_input}"
    
    logger.info(f"Link creation attempt for {channel_id} by user {user.id}")
    
    # Verify bot permissions
    is_admin, error_msg = await verify_bot_admin(context, channel_id)
    
    if not is_admin:
        await update.message.reply_text(
            f"❌ **Permission Error**\n\n{error_msg}\n\n"
            "Please:\n"
            "1. Make bot an admin\n"
            "2. Grant required permissions\n"
            "3. Try again with /start",
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END
    
    # Get channel info
    chat = await get_channel_info(context, channel_id)
    if not chat:
        await update.message.reply_text(
            "❌ Could not access channel. Please check:\n"
            "• Channel ID/username is correct\n"
            "• Bot is admin in the channel\n"
            "• Channel exists and is accessible"
        )
        return ConversationHandler.END
    
    # Generate link
    bot = await context.bot.get_me()
    share_url = create_share_link(bot.username, chat.id)
    ACTIVE_LINKS[chat.id] = share_url
    
    # Success message
    success_text = (
        f"✅ **Channel Connected!**\n\n"
        f"📺 Channel: **{chat.title}**\n\n"
        f"🔗 **Your Share Link:**\n"
        f"`{share_url}`\n\n"
        f"**How it works:**\n"
        f"• Users click link → start bot\n"
        f"• Notification posted in channel\n"
        f"• Users can vote (must be members)\n"
        f"• Votes auto-remove if they leave\n\n"
        f"Share this link now! 🚀"
    )
    
    keyboard = [[InlineKeyboardButton("📤 Share Link", url=share_url)]]
    
    await update.message.reply_text(
        success_text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    
    # Log success
    if LOG_CHANNEL_USERNAME:
        try:
            log_text = (
                f"**🔗 New Link Created**\n\n"
                f"User: {format_user_link(user)}\n"
                f"Channel: {chat.title}\n"
                f"Link: {share_url}"
            )
            await context.bot.send_message(
                chat_id=LOG_CHANNEL_USERNAME,
                text=log_text,
                parse_mode=ParseMode.MARKDOWN
            )
        except Exception as e:
            logger.error(f"Failed to log: {e}")
    
    logger.info(f"Link created successfully for channel {chat.id}")
    return ConversationHandler.END

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel conversation."""
    await update.message.reply_text(
        "❌ Operation cancelled. Use /start to begin again."
    )
    return ConversationHandler.END

# ============================================================================
# ERROR HANDLER
# ============================================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Handle errors."""
    logger.error(f"Exception occurred: {context.error}")
    
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ An error occurred. Please try again.\n"
                "If the problem persists, contact @teamrajweb"
            )
        except Exception:
            pass

# ============================================================================
# MAIN APPLICATION
# ============================================================================

def main():
    """Start the bot."""
    logger.info("=" * 60)
    logger.info("ADVANCED VOTING BOT - STARTING")
    logger.info("=" * 60)
    
    # Create application
    application = Application.builder().token(BOT_TOKEN).build()
    
    # Add handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    
    # Conversation handler for link creation
    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(create_link_start, pattern='^create_link$')],
        states={
            GET_CHANNEL_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_link_process)]
        },
        fallbacks=[CommandHandler('cancel', cancel_command)],
        allow_reentry=True
    )
    application.add_handler(conv_handler)
    
    # Callback handlers
    application.add_handler(CallbackQueryHandler(handle_vote_callback, pattern=r'^vote_-?\d+_\d+$'))
    application.add_handler(CallbackQueryHandler(my_votes_callback, pattern='^my_votes$'))
    application.add_handler(CallbackQueryHandler(help_callback, pattern='^help$'))
    
    # Error handler
    application.add_error_handler(error_handler)
    
    # Start background tasks
    loop = asyncio.get_event_loop()
    loop.create_task(verify_votes_background(application))
    loop.create_task(cleanup_cache_background(application))
    
    logger.info("✅ All handlers registered")
    logger.info("✅ Background tasks started")
    logger.info("🚀 Bot is now running...")
    logger.info("=" * 60)
    
    # Run bot
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True
    )

if __name__ == '__main__':
    main()
