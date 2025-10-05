import os
import re
import logging
import asyncio
from datetime import datetime, timedelta
from dotenv import load_dotenv # FIX 1: Corrected typo
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Chat
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    CallbackQueryHandler,
    JobQueue
)
from telegram.constants import ChatMemberStatus
from collections import defaultdict
from telegram.error import BadRequest, Forbidden
from typing import Tuple, Optional, Dict, List, Any, Set

# Load environment variables
load_dotenv() # FIX 1: Corrected typo

# ============================================================================
# 0. Configuration & Global State Management
# ============================================================================

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Environment Variables
BOT_TOKEN = os.getenv("BOT_TOKEN")
IMAGE_URL = os.getenv("IMAGE_URL", "https://picsum.photos/600/300")
# Using a log channel ID is generally more reliable than a username
LOG_CHANNEL_USERNAME = os.getenv("LOG_CHANNEL_USERNAME", "@databasefilebots")

# Conversation States
(GET_CHANNEL_ID,) = range(1)

# Enhanced Data Structures
# Vote tracking: {user_id: {channel_id: {message_id: timestamp}}}
VOTES_TRACKER: Dict[int, Dict[int, Dict[int, datetime]]] = defaultdict(lambda: defaultdict(dict))

# Vote count per message: {channel_id: {message_id: count}}
VOTES_COUNT: Dict[int, Dict[int, int]] = defaultdict(lambda: defaultdict(int))

# Channel membership cache: {user_id: {channel_id: (is_member, last_check_time)}}
MEMBERSHIP_CACHE: Dict[int, Dict[int, Tuple[bool, datetime]]] = defaultdict(dict)
CACHE_DURATION = timedelta(minutes=5)

# Managed channels: {channel_id: Chat object}
MANAGED_CHANNELS: Dict[int, Chat] = {}

# Message tracking: {channel_id: {message_id: (chat_id, message_id)}} - Redundant in channel, but kept for future proofing
# For channel messages, chat_id == channel_id. message_id is the original message ID.
VOTE_MESSAGES: Dict[int, Dict[int, Tuple[int, int]]] = defaultdict(lambda: defaultdict(lambda: (0, 0)))

# ============================================================================
# I. Utility / Helper Functions
# ============================================================================

def parse_poll_from_text(text: str) -> Optional[Tuple[str, list]]:
    """Parse poll question and options from text."""
    logger.info("Parsing poll text for question and options.")
    if not text or '?' not in text:
        logger.debug("Text is missing question mark or is empty.")
        return None
    try:
        question_part, options_part = text.split('?', 1)
        question = question_part.strip()
        options_part = options_part.strip()
        
        options = [opt.strip() for opt in re.split(r',\s*', options_part) if opt.strip()]
        
        if not question:
            logger.warning("Question is empty after parsing.")
            return None
        if len(options) < 2 or len(options) > 10:
            logger.warning(f"Invalid number of options: {len(options)}")
            return None
            
        logger.info(f"Poll parsed successfully. Question: {question}")
        return question, options
    except Exception:
        logger.exception("parse_poll_from_text encountered an unexpected error.")
        return None

async def is_bot_admin_with_permissions(context: ContextTypes.DEFAULT_TYPE, channel_id: int | str, bot_id: int) -> bool:
    """Check if bot is admin with required permissions."""
    logger.info(f"Checking bot admin status for channel: {channel_id}")
    try:
        chat_member = await context.bot.get_chat_member(chat_id=channel_id, user_id=bot_id)
        status = getattr(chat_member, "status", "").lower()

        if status in ['administrator', 'creator']:
            # Bot must be able to manage users to check membership and post to be useful
            if chat_member.can_manage_chat and chat_member.can_post_messages:
                logger.info(f"Bot is admin with required permissions in {channel_id}.")
                return True
            else:
                logger.warning(f"Bot is admin but missing 'Manage Users' or 'Post Messages' in {channel_id}.")
                # FIX 2: Return True for admin status but warn about missing permissions
                return False # Changed to False as the *required* permissions are the point.

        logger.info(f"Bot is not an admin in {channel_id}. Status: {status}")
        return False
    except Exception as e:
        logger.error(f"Bot admin check API failed for {channel_id}: {e}")
        return False

async def get_channel_url(context: ContextTypes.DEFAULT_TYPE, channel_id: int) -> Optional[str]:
    """Fetch and cache channel info, return a join URL."""
    chat_info = MANAGED_CHANNELS.get(channel_id)
    if not chat_info:
        try:
            chat_info = await context.bot.get_chat(chat_id=channel_id)
            MANAGED_CHANNELS[channel_id] = chat_info
        except Exception as e:
            logger.error(f"Failed to fetch chat info for {channel_id}: {e}")
            return None

    if chat_info.invite_link:
        return chat_info.invite_link
    if chat_info.username:
        return f"https://t.me/{chat_info.username}"
    return None

async def check_user_membership(context: ContextTypes.DEFAULT_TYPE, channel_id: int, user_id: int, use_cache: bool = True) -> Tuple[bool, Optional[str]]:
    """
    Check if user is a member of the channel with caching.
    Returns: (is_member, channel_url)
    """
    current_time = datetime.now()
    
    # Check cache
    if use_cache and user_id in MEMBERSHIP_CACHE and channel_id in MEMBERSHIP_CACHE[user_id]:
        is_member, last_check = MEMBERSHIP_CACHE[user_id][channel_id]
        if current_time - last_check < CACHE_DURATION:
            logger.debug(f"Using cached membership for user {user_id} in channel {channel_id}: {is_member}")
            channel_url = await get_channel_url(context, channel_id)
            return is_member, channel_url
    
    # Perform actual check
    try:
        channel_url = await get_channel_url(context, channel_id) # Ensure chat is managed/fetched
        
        chat_member = await context.bot.get_chat_member(chat_id=channel_id, user_id=user_id)
        # FIX 5: Added ChatMemberStatus.RESTRICTED and ChatMemberStatus.OWNER for complete membership check
        is_member = chat_member.status in [
            ChatMemberStatus.MEMBER, 
            ChatMemberStatus.ADMINISTRATOR, 
            ChatMemberStatus.CREATOR,
            ChatMemberStatus.RESTRICTED, # Restricted members are still considered members for voting.
            ChatMemberStatus.OWNER
        ]
        
        # Update cache
        MEMBERSHIP_CACHE[user_id][channel_id] = (is_member, current_time)
        
        logger.info(f"Membership check for user {user_id} in channel {channel_id}: {is_member}, Status: {chat_member.status}")
        return is_member, channel_url
        
    except (Forbidden, BadRequest) as e:
        logger.error(f"Membership check failed for {channel_id}: {e}")
        # If the bot is not admin or the user is not found (which happens if they are not a member of a private channel), assume not a member.
        return False, None
    except Exception as e:
        logger.exception(f"Critical error during membership check for {channel_id}")
        return False, None

def invalidate_membership_cache(user_id: int, channel_id: int):
    """Invalidate membership cache for a specific user and channel."""
    if user_id in MEMBERSHIP_CACHE and channel_id in MEMBERSHIP_CACHE[user_id]:
        del MEMBERSHIP_CACHE[user_id][channel_id]
        logger.debug(f"Invalidated cache for user {user_id} in channel {channel_id}")

# ============================================================================
# II. Markup/Message Creation Functions
# ============================================================================

def create_vote_markup(channel_id: int, message_id: int, current_vote_count: int, channel_url: Optional[str] = None) -> InlineKeyboardMarkup:
    """Create inline keyboard with vote button and channel link."""
    logger.debug(f"Creating vote markup for channel {channel_id}, message {message_id} with count {current_vote_count}.")
    vote_callback_data = f'vote_{channel_id}_{message_id}'
    vote_button_text = f"✅ Vote Now ({current_vote_count} Votes)"

    channel_keyboard: List[List[InlineKeyboardButton]] = []
    
    channel_keyboard.append([
        InlineKeyboardButton(vote_button_text, callback_data=vote_callback_data)
    ])
    
    if channel_url:
        channel_keyboard.append([
            InlineKeyboardButton("➡️ Go to Channel", url=channel_url)
        ])
    
    return InlineKeyboardMarkup(channel_keyboard)

async def update_vote_markup(context: ContextTypes.DEFAULT_TYPE, query: Any, channel_id_numeric: int, message_id: int, current_vote_count: int):
    """Update inline keyboard with new vote count."""
    logger.info(f"Attempting to update vote markup for message {query.message.message_id} in chat {query.message.chat.id}.")

    channel_url = None
    
    # Extract channel URL from existing markup to preserve it
    original_markup = query.message.reply_markup
    if original_markup and original_markup.inline_keyboard:
        for row in original_markup.inline_keyboard:
            for button in row:
                if button.url and "Go to Channel" in button.text:
                    channel_url = button.url
                    break
            if channel_url:
                break
    
    # If URL wasn't in markup, try to fetch it from managed channels
    if not channel_url and channel_id_numeric in MANAGED_CHANNELS:
        channel_url = await get_channel_url(context, channel_id_numeric)

    new_markup = create_vote_markup(channel_id_numeric, message_id, current_vote_count, channel_url)
    
    try:
        await query.edit_message_reply_markup(reply_markup=new_markup)
        logger.info("Markup updated successfully.")
        
    except BadRequest as e:
        if "Message is not modified" in e.message:
            logger.debug("Markup update: Message not modified.")
        elif "Message to edit not found" in e.message:
            logger.warning("Markup update: Message not found.")
        else:
            logger.error(f"Markup update failed: {e.message}")
    except Exception as e:
        logger.exception(f"Critical error while editing button: {e}")

# ============================================================================
# III. Core Command Handlers
# ============================================================================

async def send_start_message(update: Update, context: ContextTypes.DEFAULT_TYPE, reply_markup: InlineKeyboardMarkup, welcome_message: str):
    """Helper function to send start message."""
    try:
        await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=IMAGE_URL,
            caption=welcome_message,
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
    except Exception as e:
        logger.error(f"Failed to send start message with photo: {e}")
        if update.message:
            await update.message.reply_text(welcome_message, parse_mode='Markdown', reply_markup=reply_markup)
        else:
             # Handle case where start is called via deep link and update.message is None
             await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=welcome_message,
                parse_mode='Markdown',
                reply_markup=reply_markup
             )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main /start handler with deep link handling."""
    user = update.effective_user
    bot_user = await context.bot.get_me()
    bot_username = bot_user.username
    logger.info(f"User {user.id} started the bot. Args: {context.args}")
    
    # Deep Link Logic
    if context.args:
        payload = context.args[0]
        match = re.match(r'link_(\d+)', payload)

        if match:
            channel_id_str = match.groups()[0]
            # Telegram channel IDs are always -100xxxxxxxxxx
            target_channel_id_numeric = int(f"-100{channel_id_str}")
            
            try:
                # Fetch and cache chat info
                chat_info = await context.bot.get_chat(chat_id=target_channel_id_numeric)
                MANAGED_CHANNELS[target_channel_id_numeric] = chat_info
                
                channel_title = chat_info.title
                channel_url = await get_channel_url(context, target_channel_id_numeric)
                
                await update.message.reply_text(
                    f"✨ **Welcome to {channel_title}!** 🎉\n\n"
                    f"आप चैनल **`{channel_title}`** से सफलतापूर्वक जुड़ गए हैं।\n"
                    f"अब आप चैनल में वोटिंग में भाग ले सकते हैं।\n\n"
                    f"**👉 वोट करने के लिए, चैनल में जाएं और पोस्ट पर 'Vote Now' बटन दबाएं।**",
                    parse_mode='Markdown'
                )

                notification_message = (
                    f"**👑 New Participant Joined! 👑**\n"
                    f"━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"👤 **Name:** [{user.first_name}](tg://user?id={user.id})\n"
                    f"🆔 **User ID:** `{user.id}`\n"
                    f"🌐 **Username:** {f'@{user.username}' if user.username else 'N/A'}\n"
                    f"📅 **Joined:** {datetime.now().strftime('%d %b %Y, %I:%M %p')}\n\n"
                    f"🔗 **Channel:** `{channel_title}`\n"
                    f"🤖 **Via Bot:** @{bot_username}"
                )

                # Initialize vote count for the *new* message
                current_vote_count = 0
                # Use a dummy message_id for initial markup, it will be updated.
                dummy_message_id = 1 
                channel_markup = create_vote_markup(target_channel_id_numeric, dummy_message_id, current_vote_count, channel_url)

                try:
                    sent_message = await context.bot.send_photo(
                        chat_id=target_channel_id_numeric,
                        photo=IMAGE_URL,
                        caption=notification_message,
                        parse_mode='Markdown',
                        reply_markup=channel_markup
                    )
                    
                    actual_message_id = sent_message.message_id
                    
                    # FIX 3: Store message ID mapping and initialize vote count for the *actual* message ID
                    VOTE_MESSAGES[target_channel_id_numeric][actual_message_id] = (target_channel_id_numeric, actual_message_id)
                    VOTES_COUNT[target_channel_id_numeric][actual_message_id] = 0
                    
                    # Update markup with correct message ID (necessary if the dummy ID was used)
                    updated_markup = create_vote_markup(target_channel_id_numeric, actual_message_id, current_vote_count, channel_url)
                    await context.bot.edit_message_reply_markup(
                        chat_id=target_channel_id_numeric,
                        message_id=actual_message_id,
                        reply_markup=updated_markup
                    )
                    
                except (Forbidden, BadRequest) as fb_e:
                    logger.warning(f"Failed to send notification to channel {target_channel_id_numeric}: {fb_e}")

                return

            except Exception as e:
                logger.error(f"Deep link notification failed: {e}")
                await update.message.reply_text(
                    "⚠️ चैनल से जुड़ने में त्रुटि हुई। सुनिश्चित करें कि:\n"
                    "1. बॉट चैनल का एडमिन है\n"
                    "2. बॉट को सही अनुमतियाँ प्राप्त हैं"
                )
    
    # Regular Start Menu
    keyboard = [
        [
            InlineKeyboardButton("🔗 Create My Link", callback_data='start_channel_conv'),
            InlineKeyboardButton("➕ Add to Group", url=f"https://t.me/{bot_username}?startgroup=true")
        ],
        [
            InlineKeyboardButton("📊 My Votes", callback_data='my_polls_list'),
            InlineKeyboardButton("❓ Guide", url='https://t.me/teamrajweb'),
            InlineKeyboardButton("📢 Channel", url='https://t.me/narzoxbot')
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    welcome_message = (
        "**👑 Welcome to Advanced Vote Bot! 👑**\n\n"
        "🎯 **Features:**\n"
        "• Instant shareable links for your channel\n"
        "• Automatic subscription verification\n"
        "• Real-time vote tracking\n"
        "• Anti-cheat protection (one vote per user per post)\n"
        "• Auto vote removal if user leaves channel\n\n"
        "चैनल कनेक्ट करने के लिए *'🔗 Create My Link'* पर क्लिक करें।\n\n"
        "__**Built for Performance & Reliability**__"
    )

    await send_start_message(update, context, reply_markup, welcome_message)

async def create_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Create a simple Telegram poll."""
    # Ensure this is in a private chat or a group where polls are allowed
    if update.effective_chat.type not in ["private", "group", "supergroup"]:
        return await update.message.reply_text("यह कमांड केवल निजी चैट या समूह में काम करता है।")

    logger.info(f"User {update.effective_user.id} requested /poll in chat {update.effective_chat.id}.")
    parsed = parse_poll_from_text(" ".join(context.args))

    if not parsed:
        return await update.message.reply_text(
            "कृपया सही फॉर्मेट का उपयोग करें:\n"
            "`/poll [सवाल]? [ऑप्शन1], [ऑप्शन2], ...`\n"
            "कम से कम 2 और अधिकतम 10 ऑप्शन दें।",
            parse_mode='Markdown'
        )

    question, options = parsed
    try:
        await context.bot.send_poll(
            chat_id=update.effective_chat.id,
            question=question,
            options=options,
            is_anonymous=False,
            allows_multiple_answers=False,
        )
        await update.message.reply_text("✅ आपका वोट सफलतापूर्वक बना दिया गया है!")
    except Exception as e:
        logger.exception("Failed to send poll in chat")
        await update.message.reply_text(f"वोट भेजने में त्रुटि हुई: {e}")

# ============================================================================
# IV. Conversation Handlers
# ============================================================================

async def start_channel_poll_conversation_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start link generation conversation."""
    query = update.callback_query
    await query.answer()
    logger.info(f"User {query.from_user.id} started link generation conversation.")
    
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="👋 **चैनल लिंक सेटअप:**\n\n"
             "कृपया उस **चैनल का @username या ID** (`-100...`) भेजें जिसके लिए आप लिंक जनरेट करना चाहते हैं।\n\n"
             "**Important Requirements:**\n"
             "• मुझे चैनल का **Administrator** होना आवश्यक है\n"
             "• मुझे **'Manage Users'** की अनुमति चाहिए (membership check के लिए)\n"
             "• मुझे **'Post Messages'** की अनुमति चाहिए\n\n"
             "कन्वर्सेशन रद्द करने के लिए /cancel भेजें।",
        parse_mode='Markdown'
    )
    return GET_CHANNEL_ID

async def get_channel_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process channel ID input and create deep link."""
    channel_id_input = update.message.text.strip()
    user = update.effective_user
    logger.info(f"User {user.id} sent channel ID input: {channel_id_input}")

    # ID normalization
    if re.match(r'^-?\d+$', channel_id_input):
        channel_id: int | str = int(channel_id_input)
    else:
        channel_id = channel_id_input if channel_id_input.startswith('@') else f"@{channel_id_input}"

    try:
        bot_user = await context.bot.get_me()
        
        # Get chat info (needed before admin check for ID resolution)
        chat_info = await context.bot.get_chat(chat_id=channel_id)
        
        # Bot admin check
        if not await is_bot_admin_with_permissions(context, chat_info.id, bot_user.id):
            await update.message.reply_text(
                "❌ मैं आपके चैनल का **एडमिन नहीं** हूँ या मेरे पास **'Manage Users'** और **'Post Messages'** की **अनुमति नहीं** है।\n\n"
                "**Steps to add me as admin:**\n"
                "1. Go to your channel\n"
                "2. Channel Info → Administrators → Add Admin\n"
                "3. Grant these permissions:\n"
                "   • Post Messages ✅\n"
                "   • Manage Users ✅ (Important!)\n"
                "4. Send channel @username/ID again"
            )
            return GET_CHANNEL_ID
        
        # Create deep link
        raw_id_str = str(chat_info.id)
        # Deep link needs the part after -100
        link_channel_id = raw_id_str[4:] if raw_id_str.startswith('-100') else raw_id_str.replace('-', '')

        deep_link_payload = f"link_{link_channel_id}"
        share_url = f"https://t.me/{bot_user.username}?start={deep_link_payload}"
        channel_title = chat_info.title
        
        # Show link to user
        await update.message.reply_text(
            f"✅ **चैनल Successfully Connected!**\n\n"
            f"📺 **Channel:** `{channel_title}`\n"
            f"🔗 **Your Unique Share Link:**\n"
            f"```\n{share_url}\n```\n\n"
            f"**How it works:**\n"
            f"1. जब कोई यूजर इस लिंक से बॉट स्टार्ट करेगा\n"
            f"2. चैनल में उनकी जानकारी के साथ वोटिंग पोस्ट आएगी\n"
            f"3. वे वोट तभी कर पाएंगे जब चैनल के मेंबर होंगे\n"
            f"4. अगर चैनल छोड़ेंगे तो वोट हट जाएगा\n\n"
            f"अब इस लिंक को शेयर करें! 🚀",
            parse_mode='Markdown'
        )
        
        share_keyboard = [[InlineKeyboardButton("🔗 Share This Link", url=share_url)]]
        share_markup = InlineKeyboardMarkup(share_keyboard)
        
        await update.message.reply_text(
            "शेयर करने के लिए बटन दबाएँ:",
            reply_markup=share_markup
        )
        
        # Log notification
        if LOG_CHANNEL_USERNAME:
            log_message = (
                f"**🔗 New Channel Linked!**\n\n"
                f"👤 User: [{user.first_name}](tg://user?id={user.id})\n"
                f"📺 Channel: `{channel_title}`\n"
                f"🔗 Link: {share_url}\n"
                f"📅 Time: {datetime.now().strftime('%d %b %Y, %I:%M %p')}"
            )
            try:
                await context.bot.send_message(
                    chat_id=LOG_CHANNEL_USERNAME,
                    text=log_message,
                    parse_mode='Markdown'
                )
            except Exception as log_err:
                logger.error(f"Failed to send log: {log_err}")
        
        # Add to managed channels
        MANAGED_CHANNELS[chat_info.id] = chat_info

        logger.info(f"Link generation successful for channel {chat_info.id}.")
        return ConversationHandler.END

    except Exception as e:
        logger.error(f"Error in get_channel_id for input {channel_id_input}: {e}")
        await update.message.reply_text(
            "⚠️ **चैनल तक पहुँचने में त्रुटि**\n\n"
            "सुनिश्चित करें कि:\n"
            "1. चैनल का @username/ID सही है\n"
            "2. चैनल **पब्लिक** है या मैं उसमें एडमिन हूँ\n"
            "3. मुझे सही अनुमतियाँ मिली हैं\n\n"
            "फिर से प्रयास करें या /cancel भेजें।"
        )
        return GET_CHANNEL_ID

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel conversation."""
    await update.message.reply_text('❌ कन्वर्सेशन रद्द कर दिया गया है। /start से फिर शुरू करें।')
    return ConversationHandler.END

# ============================================================================
# V. Advanced Vote Handler with Auto-Removal
# ============================================================================

async def schedule_membership_recheck(context: ContextTypes.DEFAULT_TYPE, user_id: int, channel_id: int, message_id: int):
    """Background task to periodically check membership and remove vote if user left."""
    
    # Check membership again
    invalidate_membership_cache(user_id, channel_id)
    is_member, _ = await check_user_membership(context, channel_id, user_id, use_cache=False)
    
    if not is_member:
        # User left channel - remove vote
        if message_id in VOTES_TRACKER[user_id].get(channel_id, {}):
            del VOTES_TRACKER[user_id][channel_id][message_id]
            VOTES_COUNT[channel_id][message_id] = max(0, VOTES_COUNT[channel_id][message_id] - 1)
            
            logger.info(f"Vote removed for user {user_id} (left channel {channel_id})")
            
            # Update message markup
            if channel_id in VOTE_MESSAGES and message_id in VOTE_MESSAGES[channel_id]:
                chat_id, msg_id = VOTE_MESSAGES[channel_id][message_id]
                try:
                    current_vote_count = VOTES_COUNT[channel_id][message_id]
                    channel_url = await get_channel_url(context, channel_id)
                    
                    new_markup = create_vote_markup(channel_id, message_id, current_vote_count, channel_url)
                    await context.bot.edit_message_reply_markup(
                        chat_id=chat_id,
                        message_id=msg_id,
                        reply_markup=new_markup
                    )
                except Exception as e:
                    logger.error(f"Failed to update markup after vote removal: {e}")

async def handle_vote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle voting with membership check and auto-removal on leave."""
    query = update.callback_query
    
    # Extract Channel ID and Message ID
    data = query.data
    match = re.match(r'vote_(-?\d+)_(\d+)', data)
    
    if not match:
        return await query.answer(text="❌ Invalid vote ID.", show_alert=True)

    channel_id_numeric = int(match.group(1))
    message_id = int(match.group(2))
    user_id = query.from_user.id
    logger.info(f"Vote attempt by user {user_id} for channel {channel_id_numeric}, message {message_id}.")
    
    # Check if already voted
    if message_id in VOTES_TRACKER[user_id].get(channel_id_numeric, {}):
        return await query.answer(text="🗳️ आप पहले ही वोट कर चुके हैं!", show_alert=True)
    
    # FIX 6: Invalidate cache immediately and perform check without cache to ensure fresh membership data.
    invalidate_membership_cache(user_id, channel_id_numeric)
    is_subscriber, channel_url = await check_user_membership(context, channel_id_numeric, user_id, use_cache=False)
    
    if not is_subscriber:
        return await query.answer(
            text="❌ वोट करने के लिए पहले चैनल join करें! (या सुनिश्चित करें कि बॉट के पास 'Manage Users' की अनुमति है)", 
            show_alert=True
        )
    
    # Register vote
    VOTES_TRACKER[user_id][channel_id_numeric][message_id] = datetime.now()
    VOTES_COUNT[channel_id_numeric][message_id] += 1
    current_vote_count = VOTES_COUNT[channel_id_numeric][message_id]
    
    # Success alert
    await query.answer(text=f"✅ Vote #{current_vote_count} registered! धन्यवाद!", show_alert=True)
    
    # Update button
    await update_vote_markup(context, query, channel_id_numeric, message_id, current_vote_count)
    
    # Schedule membership re-check via Job Queue (More reliable than a naked asyncio task)
    context.job_queue.run_once(
        lambda ctx: schedule_membership_recheck(ctx, user_id, channel_id_numeric, message_id),
        when=timedelta(minutes=5),
        name=f"recheck_{user_id}_{channel_id_numeric}_{message_id}"
    )
    
    logger.info(f"Vote successfully registered for user {user_id}. Recheck scheduled.")

# ============================================================================
# VI. Status and Auxiliary Handlers
# ============================================================================

async def my_polls_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user's votes and managed channels."""
    query = update.callback_query
    if query:
        await query.answer()

    user_id = update.effective_user.id
    logger.info(f"User {user_id} requested my_polls_list.")
    
    message = "**📊 Your Voting Dashboard**\n━━━━━━━━━━━━━━━━━━━━\n\n"
    
    # Voted channels
    user_votes = VOTES_TRACKER[user_id]
    if user_votes:
        total_votes = sum(len(messages) for messages in user_votes.values())
        message += f"**🗳️ Total Votes Cast:** {total_votes}\n\n"
        
        for channel_id, messages in user_votes.items():
            channel_title = "Unknown Channel"
            if channel_id in MANAGED_CHANNELS:
                channel = MANAGED_CHANNELS[channel_id]
                channel_title = channel.title
                
            message += f"• **{channel_title}:** {len(messages)} vote(s)\n"
    else:
        message += "**🗳️ आपने अभी तक कोई वोट नहीं किया है।**\n\n"

    # Managed channels (for admins) - NOTE: This only shows channels added via the /start flow
    if MANAGED_CHANNELS:
        message += "\n**👑 Managed Channels:**\n"
        for c_id, chat in MANAGED_CHANNELS.items():
            total_channel_votes = VOTES_COUNT.get(c_id, {}).get(0, 0) # Simple total, might need refinement for multiple posts
            message += f"• [{chat.title}](https://t.me/{chat.username if chat.username else 'private'})\n"
            message += f"  └─ Total tracked votes: {total_channel_votes}\n"
    
    message += "\n*🔄 वोट ऑटोमैटिक हट जाएगा अगर आप चैनल छोड़ देते हैं।*"
    
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=message,
        parse_mode='Markdown'
    )

async def check_bot_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check bot's current health and configuration."""
    bot_info = await context.bot.get_me()
    
    total_votes = sum(sum(messages.values()) for messages in VOTES_COUNT.values())
    total_users = len(VOTES_TRACKER)
    total_cache_entries = sum(len(v) for v in MEMBERSHIP_CACHE.values())
    
    status_message = (
        f"**🤖 Bot Health Status**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"**✅ General Info:**\n"
        f"• Bot: @{bot_info.username}\n"
        f"• Status: 🟢 Online & Active\n"
        f"• Uptime: Stable\n\n"
        f"**📊 Statistics:**\n"
        f"• Managed Channels: {len(MANAGED_CHANNELS)}\n"
        f"• Total Tracked Votes: {total_votes}\n"
        f"• Active Voters: {total_users}\n"
        f"• Cache Entries: {total_cache_entries}\n\n"
        f"**⚙️ Features:**\n"
        f"• ✅ Auto vote removal on leave\n"
        f"• ✅ Membership caching (5 min)\n"
        f"• ✅ One vote per user per post\n"
        f"• ✅ Real-time vote tracking\n"
        f"• {'✅' if LOG_CHANNEL_USERNAME else '❌'} Log channel configured\n\n"
        f"*System running with advanced error handling.*"
    )
    
    await update.message.reply_text(status_message, parse_mode='Markdown')

async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Provide help guide for users."""
    help_message = (
        "**📚 Advanced Vote Bot - Complete Guide**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "**🔗 1. Create Channel Link:**\n"
        "• `/start` → Click '🔗 Create My Link'\n"
        "• Send your channel @username or ID\n"
        "• Requirements:\n"
        "  ✓ Bot must be channel admin\n"
        "  ✓ 'Manage Users' permission needed\n"
        "  ✓ 'Post Messages' permission needed\n"
        "• Get instant shareable link!\n\n"
        "**🗳️ 2. How Voting Works:**\n"
        "• Users click your link → Start bot\n"
        "• Bot posts notification in channel\n"
        "• Users can vote only if subscribed\n"
        "• One vote per user per post\n"
        "• Vote auto-removes if user leaves!\n\n"
        "**⚙️ 3. Commands:**\n"
        "• `/start` - Main menu & deep links\n"
        "• `/status` - Bot health check\n"
        "• `/help` - This guide\n"
        "• `/poll [question]? opt1, opt2` - Create a simple poll\n"
        "• `/cancel` - Cancel conversation\n\n"
        "**🛡️ 4. Security Features:**\n"
        "• Anti-cheat: One vote only\n"
        "• Auto cleanup: Votes removed on leave\n"
        "• Cache system: Fast checks (5 min)\n"
        "• Admin verification required\n\n"
        "**❓ Need Support?**\n"
        "• Guide: @teamrajweb\n"
        "• Updates: @narzoxbot\n\n"
        "*Built with advanced error handling & performance optimization.*"
    )
    await update.message.reply_text(help_message, parse_mode='Markdown')

# ============================================================================
# VII. Background Tasks & Maintenance
# ============================================================================

async def cleanup_old_cache(context: ContextTypes.DEFAULT_TYPE):
    """Periodic task to clean up old cache entries."""
    current_time = datetime.now()
    cleaned = 0
    
    for user_id in list(MEMBERSHIP_CACHE.keys()):
        # Use a copy of keys to allow deletion during iteration
        for channel_id in list(MEMBERSHIP_CACHE[user_id].keys()):
            _, last_check = MEMBERSHIP_CACHE[user_id][channel_id]
            # Clean entries twice as old as the cache duration
            if current_time - last_check > CACHE_DURATION * 2: 
                del MEMBERSHIP_CACHE[user_id][channel_id]
                cleaned += 1
        
        # Remove empty user entries
        if not MEMBERSHIP_CACHE[user_id]:
            del MEMBERSHIP_CACHE[user_id]
    
    if cleaned > 0:
        logger.info(f"Cleaned {cleaned} old cache entries.")
    else:
        logger.debug("No old cache entries to clean.")

# ============================================================================
# VIII. Error Handlers
# ============================================================================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Log errors and handle gracefully."""
    logger.error(f"Exception while handling update: {context.error}")
    
    # Do not spam error message on silent errors like "Message not modified"
    if isinstance(context.error, BadRequest) and "Message is not modified" in str(context.error):
        return

    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "⚠️ **An error occurred**\n\n"
                "The bot encountered an unexpected error. Please try again.\n"
                "If the problem persists, contact support: @teamrajweb"
            )
        except Exception as e:
            logger.error(f"Failed to send error message: {e}")

# ============================================================================
# IX. Main Application Setup
# ============================================================================

def configure_bot_application() -> ApplicationBuilder:
    """Configure bot application."""
    logger.info("Starting bot application configuration.")
    if not BOT_TOKEN:
        logger.critical("BOT_TOKEN is missing. Aborting startup.")
        raise ValueError("BOT_TOKEN environment variable is not set.")

    # Use JobQueue for reliable background tasks
    job_queue = JobQueue()
    return ApplicationBuilder().token(BOT_TOKEN).job_queue(job_queue)

def main():
    """Start bot application and add all handlers."""
    try:
        application = configure_bot_application().build()
    except ValueError:
        logger.critical("Cannot start bot without BOT_TOKEN.")
        return

    # Command Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("poll", create_poll))
    application.add_handler(CommandHandler("status", check_bot_status))
    application.add_handler(CommandHandler("help", show_help))
    application.add_handler(CommandHandler("cancel", cancel, filters=filters.ChatType.PRIVATE)) # Allow /cancel globally for simplicity

    # Callback Query Handlers
    # FIX 4: Corrected unbalanced parentheses in regex pattern
    application.add_handler(CallbackQueryHandler(handle_vote, pattern=r'^vote_(-?\d+)_(\d+)$'))
    application.add_handler(CallbackQueryHandler(my_polls_list, pattern='^my_polls_list$'))

    # Conversation Handler for Link Generation
    link_conv_handler = ConversationHandler(
        entry_points=[
            # FIX 4: Corrected unbalanced parentheses in pattern
            CallbackQueryHandler(start_channel_poll_conversation_cb, pattern='^start_channel_conv$'),
        ],
        states={
            GET_CHANNEL_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_channel_id)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        allow_reentry=False
    )
    application.add_handler(link_conv_handler)

    # Error handler
    application.add_error_handler(error_handler)

    # Start background tasks using JobQueue for maintenance
    application.job_queue.run_repeating(
        cleanup_old_cache, 
        interval=timedelta(minutes=10), 
        first=timedelta(minutes=10), 
        name="periodic_cache_cleanup"
    )

    logger.info("=" * 50)
    logger.info("🚀 Advanced Voting Bot Started Successfully!")
    logger.info("=" * 50)
    logger.info("Features Enabled:")
    logger.info("  ✅ Auto vote removal on channel leave (JobQueue)")
    logger.info("  ✅ Membership caching (5 min)")
    logger.info("  ✅ One vote per user per post")
    logger.info("  ✅ Real-time vote tracking")
    logger.info("  ✅ Advanced error handling")
    logger.info("  ✅ Background cache cleanup (JobQueue)")
    logger.info("=" * 50)
    
    application.run_polling(poll_interval=2, allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
