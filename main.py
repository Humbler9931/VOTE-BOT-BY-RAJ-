# -*- coding: utf-8 -*-
"""
Advanced Voting Bot - Refactored and Stylized Version.
Uses modern python-telegram-bot (PTB) features, improved type hinting, 
and refined data structures for better readability and performance.
"""

import os
import re
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
from typing import Tuple, Optional, Dict, List, Final
from collections import defaultdict
from dataclasses import dataclass, field

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Chat
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# Load .env if present (local dev)
load_dotenv()

# ============================
# 1. Configuration & Globals
# ============================

# --- Logging Setup ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Environment Variables (Final Constants) ---
BOT_TOKEN: Final[str | None] = os.getenv("BOT_TOKEN")
IMAGE_URL: Final[str] = os.getenv("IMAGE_URL", "https://picsum.photos/600/300")
LOG_CHANNEL_USERNAME: Final[str | None] = os.getenv("LOG_CHANNEL_USERNAME")
RENDER_HOSTNAME: Final[str | None] = os.getenv("RENDER_EXTERNAL_HOSTNAME") or os.getenv("WEBHOOK_URL")
PORT: Final[int] = int(os.getenv("PORT", 8443))
CACHE_DURATION: Final[timedelta] = timedelta(minutes=5)

if not BOT_TOKEN:
    logger.critical("BOT_TOKEN environment variable is required. Exiting.")
    raise SystemExit("BOT_TOKEN missing")

# --- Conversation States ---
GET_CHANNEL_ID: Final[int] = 1

# --- Data Structures (Using dataclasses for clarity) ---

@dataclass
class VoteState:
    """Stores the time of the vote for a specific message by a user."""
    timestamp: datetime = field(default_factory=datetime.now)

# VOTES_TRACKER: {user_id: {channel_id: {message_id: VoteState}}}
VOTES_TRACKER: Dict[int, Dict[int, Dict[int, VoteState]]] = defaultdict(lambda: defaultdict(dict))

# VOTES_COUNT: {channel_id: {message_id: count}}
VOTES_COUNT: Dict[int, Dict[int, int]] = defaultdict(lambda: defaultdict(int))

# MEMBERSHIP_CACHE: {user_id: {channel_id: (is_member, last_check_time)}}
MEMBERSHIP_CACHE: Dict[int, Dict[int, Tuple[bool, datetime]]] = defaultdict(dict)

# MANAGED_CHANNELS: {channel_id: Chat object} - Stores chat info to avoid redundant API calls
MANAGED_CHANNELS: Dict[int, Chat] = {}

# VOTE_MESSAGES: {channel_id: {message_id: (chat_id, message_id)}} - Used for edit_message_reply_markup
# The original structure was a bit redundant, simplifying the value to just hold the necessary
# chat_id (which is the channel_id itself) and message_id for safe markup updates.
# For simplicity, we can use the original message_id as key, and its chat_id (channel_id) as part of the value.
# VOTE_MESSAGES will not be strictly needed if we only rely on the callback query's message data,
# but keeping it for robust update logic in schedule_membership_recheck_job.
# Stored as: {channel_id: {message_id: (channel_id, message_id)}}
# Note: The original code's deep link logic incorrectly assumed the message_id from the deep-link-sent-message
# needed to be stored in VOTE_MESSAGES. It's only needed for messages with the vote button.
VOTE_MESSAGES: Dict[int, Dict[int, Tuple[int, int]]] = defaultdict(lambda: defaultdict(lambda: (0, 0)))

# ============================
# 2. Utilities (Refined)
# ============================

def parse_poll_from_text(text: str) -> Optional[Tuple[str, List[str]]]:
    """Parses a poll question and options from a text string."""
    if not text or '?' not in text:
        return None
    try:
        # Improved regex split to handle cases where '?' is not separated by space
        parts = re.split(r'\?+\s*', text, 1)
        if len(parts) < 2:
            return None
        
        question = parts[0].strip()
        options = [o.strip() for o in re.split(r',\s*', parts[1].strip()) if o.strip()]
        
        # Enforce minimum and maximum options
        if not question or not (2 <= len(options) <= 10):
            return None
            
        return question, options
    except Exception:
        logger.exception("parse_poll_from_text failed")
        return None


async def is_bot_admin_with_permissions(context: ContextTypes.DEFAULT_TYPE, channel_id: int | str, bot_id: int) -> bool:
    """Checks if the bot is an admin with required permissions (manage users, post messages)."""
    try:
        cm = await context.bot.get_chat_member(chat_id=channel_id, user_id=bot_id)
        status = getattr(cm, "status", "").lower()
        
        if status in ['administrator', 'creator']:
            # Essential permissions for the bot's functionality
            can_manage = getattr(cm, "can_manage_chat", False) or getattr(cm, "can_restrict_members", False)
            can_post = getattr(cm, "can_post_messages", True) # Default True for channels if not explicitly set

            if can_manage and can_post:
                return True
            logger.warning("Bot admin but missing permissions on channel %s (manage: %s, post: %s)", 
                           channel_id, can_manage, can_post)
            return False
            
        logger.info("Bot is not an admin in %s (status=%s)", channel_id, status)
        return False
    except Exception as e:
        logger.error("is_bot_admin_with_permissions failed for %s: %s", channel_id, e)
        return False


async def get_channel_url(context: ContextTypes.DEFAULT_TYPE, channel_id: int) -> Optional[str]:
    """Retrieves the channel's invite link or public URL, caching the Chat object."""
    chat_info = MANAGED_CHANNELS.get(channel_id)
    if not chat_info:
        try:
            chat_info = await context.bot.get_chat(chat_id=channel_id)
            MANAGED_CHANNELS[channel_id] = chat_info
        except Exception as e:
            logger.error("get_chat failed for %s: %s", channel_id, e)
            return None
            
    if getattr(chat_info, "invite_link", None):
        return chat_info.invite_link
    if getattr(chat_info, "username", None):
        return f"https://t.me/{chat_info.username}"
        
    return None


async def check_user_membership(context: ContextTypes.DEFAULT_TYPE, channel_id: int, user_id: int, use_cache: bool = True) -> Tuple[bool, Optional[str]]:
    """Checks user's membership status in a channel, utilizing a cache."""
    now = datetime.now()
    url = await get_channel_url(context, channel_id) # Pre-fetch URL for immediate use
    
    # Check cache
    if use_cache:
        entry = MEMBERSHIP_CACHE.get(user_id, {}).get(channel_id)
        if entry:
            is_member, last = entry
            if now - last < CACHE_DURATION:
                logger.debug("Using cached membership for %s in %s => %s", user_id, channel_id, is_member)
                return is_member, url

    # Check via Telegram API
    try:
        cm = await context.bot.get_chat_member(chat_id=channel_id, user_id=user_id)
        status = getattr(cm, "status", "")
        is_member = status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
            ChatMemberStatus.RESTRICTED, # Restricted members are still considered 'joined'
        )
        
        # Update cache
        MEMBERSHIP_CACHE[user_id][channel_id] = (is_member, now)
        logger.info("Membership check for user %s in channel %s: %s, Status: %s", user_id, channel_id, is_member, status)
        return is_member, url
    except (Forbidden, BadRequest) as e:
        logger.warning("Membership API returned error for channel %s user %s: %s", channel_id, user_id, e)
        return False, url # Keep the URL even if check failed
    except Exception as e:
        logger.exception("Unexpected membership check error for %s/%s", channel_id, user_id)
        return False, url


def invalidate_membership_cache(user_id: int, channel_id: int):
    """Explicitly removes a user's membership status for a channel from the cache."""
    if user_id in MEMBERSHIP_CACHE and channel_id in MEMBERSHIP_CACHE[user_id]:
        del MEMBERSHIP_CACHE[user_id][channel_id]
        logger.debug("Invalidated membership cache for %s in %s", user_id, channel_id)


# ============================
# 3. Markup Helpers
# ============================

def create_vote_markup(channel_id: int, message_id: int, current_vote_count: int, channel_url: Optional[str] = None) -> InlineKeyboardMarkup:
    """Creates the inline keyboard markup for the vote button."""
    vote_callback_data = f'vote_{channel_id}_{message_id}'
    vote_button_text = f"🗳️ Vote Now ({current_vote_count})"
    
    keyboard = [[InlineKeyboardButton(vote_button_text, callback_data=vote_callback_data)]]
    
    if channel_url:
        # Add a secondary button to easily join the channel
        keyboard.append([InlineKeyboardButton("📢 Join Channel", url=channel_url)])
        
    return InlineKeyboardMarkup(keyboard)


async def update_vote_markup(context: ContextTypes.DEFAULT_TYPE, channel_id: int, message_id: int, new_vote_count: int):
    """Safely updates the vote count button on the channel post."""
    try:
        channel_chat_id = channel_id # Channel ID is also the chat ID for editing
        
        new_markup = create_vote_markup(channel_id, message_id, new_vote_count, await get_channel_url(context, channel_id))
        
        await context.bot.edit_message_reply_markup(
            chat_id=channel_chat_id,
            message_id=message_id,
            reply_markup=new_markup
        )
    except BadRequest as e:
        if "Message is not modified" in str(e):
            logger.debug("edit_message_reply_markup: Message not modified.")
        elif "Message to edit not found" in str(e):
            logger.warning("edit_message_reply_markup: Message not found.")
        else:
            logger.error("Markup update failed (BadRequest) for channel %s, message %s: %s", channel_id, message_id, e)
    except Exception as e:
        logger.exception("Critical error while editing button: %s", e)


# ============================
# 4. Core Handlers
# ============================

async def send_start_message(update: Update, context: ContextTypes.DEFAULT_TYPE, reply_markup: InlineKeyboardMarkup, welcome_message: str):
    """Helper to consistently send the welcome message, prioritizing photo."""
    chat_id = update.effective_chat.id
    try:
        await context.bot.send_photo(
            chat_id=chat_id,
            photo=IMAGE_URL,
            caption=welcome_message,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup
        )
    except Exception as e:
        logger.error("Failed to send start message with photo: %s. Falling back to text.", e)
        # Fallback to text message if photo fails
        await context.bot.send_message(
            chat_id=chat_id,
            text=welcome_message,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=reply_markup
        )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Main /start handler with deep link handling for channel joining."""
    user = update.effective_user
    bot_user = await context.bot.get_me()
    bot_username = bot_user.username
    
    if not user:
        return
        
    logger.info("User %s started the bot. Args: %s", user.id, context.args)
    
    # --- Deep Link Logic ---
    if context.args:
        payload = context.args[0]
        match = re.match(r'link_(-?\d+)', payload)
        
        if match:
            # Reconstruct the channel ID. Deep link payloads are often numeric parts.
            channel_id_part = match.groups()[0]
            # Telegram channel IDs are typically in the format -100XXXXXXX
            target_channel_id_numeric = int(f"-100{channel_id_part}") if len(channel_id_part) < 15 and not channel_id_part.startswith('-100') else int(channel_id_part)
            
            try:
                chat_info = await context.bot.get_chat(chat_id=target_channel_id_numeric)
                MANAGED_CHANNELS[target_channel_id_numeric] = chat_info
                
                channel_title = chat_info.title
                channel_url = await get_channel_url(context, target_channel_id_numeric)
                
                await update.effective_chat.send_message(
                    f"✨ **Welcome to {channel_title}!** 🎉\n\n"
                    f"आप चैनल **`{channel_title}`** से सफलतापूर्वक जुड़ गए हैं।\n"
                    f"अब आप चैनल में वोटिंग में भाग ले सकते हैं।\n\n"
                    f"**👉 वोट करने के लिए, चैनल में जाएं और पोस्ट पर '🗳️ Vote Now' बटन दबाएं।**",
                    parse_mode=ParseMode.MARKDOWN
                )

                # Send a 'Welcome' vote post to the channel
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

                # The "initial" vote post logic is a bit unusual but kept for feature parity.
                # It's used as a "trackable" message.
                initial_vote_count = 0 
                # Create markup using a dummy message_id first (0) to allow sending the message
                dummy_message_id = 0
                initial_markup = create_vote_markup(target_channel_id_numeric, dummy_message_id, initial_vote_count, channel_url)

                sent_message = await context.bot.send_photo(
                    chat_id=target_channel_id_numeric,
                    photo=IMAGE_URL,
                    caption=notification_message,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=initial_markup
                )
                
                actual_message_id = sent_message.message_id
                
                # Store the actual message ID and update the vote count tracker
                VOTE_MESSAGES[target_channel_id_numeric][actual_message_id] = (target_channel_id_numeric, actual_message_id)
                VOTES_COUNT[target_channel_id_numeric][actual_message_id] = initial_vote_count
                
                # Update markup with the correct, actual message ID
                updated_markup = create_vote_markup(target_channel_id_numeric, actual_message_id, initial_vote_count, channel_url)
                await context.bot.edit_message_reply_markup(
                    chat_id=target_channel_id_numeric,
                    message_id=actual_message_id,
                    reply_markup=updated_markup
                )
                
            except (Forbidden, BadRequest) as fb_e:
                logger.warning("Failed to process deep link/send notification to channel %s: %s", target_channel_id_numeric, fb_e)
                await update.effective_chat.send_message(
                    "⚠️ चैनल से जुड़ने में त्रुटि हुई। सुनिश्चित करें कि:\n"
                    "1. बॉट चैनल का एडमिन है\n"
                    "2. बॉट को सही अनुमतियाँ प्राप्त हैं"
                )
            except Exception as e:
                logger.error("Deep link notification failed: %s", e)
                await update.effective_chat.send_message("⚠️ एक अज्ञात त्रुटि हुई।")

            return

    # --- Regular Start Menu ---
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
        "**👑 Welcome to Advanced Vote Bot! 👑**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
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
    if update.effective_chat.type not in [Chat.PRIVATE, Chat.GROUP, Chat.SUPERGROUP]:
        return await update.message.reply_text("यह कमांड केवल निजी चैट या समूह में काम करता है।")

    logger.info("User %s requested /poll in chat %s", update.effective_user.id, update.effective_chat.id)
    parsed = parse_poll_from_text(" ".join(context.args))

    if not parsed:
        return await update.message.reply_text(
            "कृपया सही फॉर्मेट का उपयोग करें:\n"
            "`/poll [सवाल]? [ऑप्शन1], [ऑप्शन2], ...`\n"
            "कम से कम 2 और अधिकतम 10 ऑप्शन दें।",
            parse_mode=ParseMode.MARKDOWN
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
        logger.exception("Failed to send poll in chat: %s", e)
        await update.message.reply_text(f"वोट भेजने में त्रुटि हुई: {e}")


# ============================
# 5. Conversation Handlers
# ============================

async def start_channel_poll_conversation_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start link generation conversation."""
    query = update.callback_query
    if query:
        await query.answer()
        
    logger.info("User %s started link generation conversation.", update.effective_user.id)
    
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="👋 **चैनल लिंक सेटअप:**\n\n"
             "कृपया उस **चैनल का @username या ID** (`-100...`) भेजें जिसके लिए आप लिंक जनरेट करना चाहते हैं।\n\n"
             "**Important Requirements:**\n"
             "• मुझे चैनल का **Administrator** होना आवश्यक है\n"
             "• मुझे **'Manage Users'** की अनुमति चाहिए (membership check के लिए)\n"
             "• मुझे **'Post Messages'** की अनुमति चाहिए\n\n"
             "कन्वर्सेशन रद्द करने के लिए /cancel भेजें।",
        parse_mode=ParseMode.MARKDOWN
    )
    return GET_CHANNEL_ID


async def get_channel_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Process channel ID input and create deep link."""
    channel_id_input = update.message.text.strip()
    user = update.effective_user
    logger.info("User %s sent channel ID input: %s", user.id, channel_id_input)

    # Determine if input is numeric ID or username
    if re.match(r'^-?\d+$', channel_id_input):
        # Already a numeric ID (e.g., -10012345)
        channel_id: int | str = int(channel_id_input)
    else:
        # Assume username, ensure it starts with @ for get_chat API call
        channel_id = channel_id_input if channel_id_input.startswith('@') else f"@{channel_id_input}"

    try:
        bot_user = await context.bot.get_me()
        chat_info = await context.bot.get_chat(chat_id=channel_id)
        
        # Security and functionality check
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
        
        # Prepare Deep Link Payload
        raw_id_str = str(chat_info.id)
        # Remove the -100 prefix for a cleaner deep link payload
        link_channel_id = raw_id_str[4:] if raw_id_str.startswith('-100') else raw_id_str.replace('-', '')
        
        deep_link_payload = f"link_{link_channel_id}"
        share_url = f"https://t.me/{bot_user.username}?start={deep_link_payload}"
        channel_title = chat_info.title
        
        # Success Messages
        await update.message.reply_text(
            f"✅ **चैनल Successfully Connected!**\n"
            f"━━━━━━━━━━━━━━━━━━━━\n\n"
            f"📺 **Channel:** `{channel_title}`\n"
            f"🔗 **Your Unique Share Link:**\n"
            f"```\n{share_url}\n```\n\n"
            f"**How it works:**\n"
            f"1. जब कोई यूजर इस लिंक से बॉट स्टार्ट करेगा\n"
            f"2. चैनल में उनकी जानकारी के साथ वोटिंग पोस्ट आएगी\n"
            f"3. वे वोट तभी कर पाएंगे जब चैनल के मेंबर होंगे\n"
            f"4. अगर चैनल छोड़ेंगे तो वोट हट जाएगा\n\n"
            f"अब इस लिंक को शेयर करें! 🚀",
            parse_mode=ParseMode.MARKDOWN
        )
        
        share_keyboard = [[InlineKeyboardButton("🔗 Share This Link", url=share_url)]]
        share_markup = InlineKeyboardMarkup(share_keyboard)
        
        await update.message.reply_text(
            "शेयर करने के लिए बटन दबाएँ:",
            reply_markup=share_markup
        )
        
        # Logging to a dedicated channel (if configured)
        if LOG_CHANNEL_USERNAME:
            log_message = (
                f"**🔗 New Channel Linked!**\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 User: [{user.first_name}](tg://user?id={user.id})\n"
                f"📺 Channel: `{channel_title}`\n"
                f"🔗 Link: {share_url}\n"
                f"📅 Time: {datetime.now().strftime('%d %b %Y, %I:%M %p')}"
            )
            try:
                await context.bot.send_message(
                    chat_id=LOG_CHANNEL_USERNAME,
                    text=log_message,
                    parse_mode=ParseMode.MARKDOWN
                )
            except Exception as log_err:
                logger.error("Failed to send log to channel %s: %s", LOG_CHANNEL_USERNAME, log_err)
        
        MANAGED_CHANNELS[chat_info.id] = chat_info

        logger.info("Link generation successful for channel %s.", chat_info.id)
        return ConversationHandler.END

    except Exception as e:
        logger.error("Error in get_channel_id for input %s: %s", channel_id_input, e)
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


# ============================
# 6. Advanced Vote Handler & Job
# ============================

async def schedule_membership_recheck_job(context: ContextTypes.DEFAULT_TYPE):
    """Job callback to periodically check membership and remove vote if user left."""
    
    job_data = context.job.data if hasattr(context, "job") else {}
    
    user_id = job_data.get('user_id')
    channel_id = job_data.get('channel_id')
    message_id = job_data.get('message_id')

    if not (user_id and channel_id and message_id):
        logger.warning("schedule_membership_recheck_job: incomplete data: %s", job_data)
        return

    # Invalidate cache before check to force an API call
    invalidate_membership_cache(user_id, channel_id)
    is_member, _ = await check_user_membership(context, channel_id, user_id, use_cache=False)
    
    if not is_member:
        # User left channel - remove vote
        if message_id in VOTES_TRACKER.get(user_id, {}).get(channel_id, {}):
            del VOTES_TRACKER[user_id][channel_id][message_id]
            VOTES_COUNT[channel_id][message_id] = max(0, VOTES_COUNT[channel_id][message_id] - 1)
            
            logger.info("Vote removed for user %s (left channel %s) from message %s", user_id, channel_id, message_id)
            
            current_vote_count = VOTES_COUNT[channel_id][message_id]
            
            # Update message markup
            await update_vote_markup(context, channel_id, message_id, current_vote_count)
            
        else:
            logger.debug("User %s left channel %s, but no active vote found to remove.", user_id, channel_id)


async def handle_vote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle voting with membership check and auto-removal on leave."""
    query = update.callback_query
    if not query:
        return

    # Decode callback data: vote_[channel_id]_[message_id]
    data = query.data
    match = re.match(r'vote_(-?\d+)_(\d+)', data)
    
    if not match:
        await query.answer(text="❌ Invalid vote ID.", show_alert=True)
        return

    channel_id_numeric = int(match.group(1))
    message_id = int(match.group(2))
    user_id = query.from_user.id
    logger.info("Vote attempt by user %s for channel %s, message %s.", user_id, channel_id_numeric, message_id)
    
    # Check if already voted (Anti-cheat/One-vote-per-post)
    if message_id in VOTES_TRACKER.get(user_id, {}).get(channel_id_numeric, {}):
        await query.answer(text="🗳️ आप पहले ही वोट कर चुके हैं!", show_alert=True)
        return
    
    # Membership Check: Force check to ensure latest status before registering vote
    is_subscriber, channel_url = await check_user_membership(context, channel_id_numeric, user_id, use_cache=False)
    
    if not is_subscriber:
        # Construct the join button for the alert text
        join_button = f"\n\n**👉 [Join Channel Now]({channel_url})**" if channel_url else ""
        
        await query.answer(
            text=f"❌ वोट करने के लिए आपको पहले चैनल join करना होगा!{join_button} (कृपया सुनिश्चित करें कि आप चैनल में सक्रिय सदस्य हैं)", 
            show_alert=True,
            url=channel_url if channel_url else None
        )
        return
    
    # Register vote
    VOTES_TRACKER[user_id][channel_id_numeric][message_id] = VoteState() # Store vote time
    VOTES_COUNT[channel_id_numeric][message_id] += 1
    current_vote_count = VOTES_COUNT[channel_id_numeric][message_id]
    
    # Success alert
    await query.answer(text=f"✅ Vote #{current_vote_count} registered! धन्यवाद!", show_alert=True)
    
    # Update button (Use the utility function for safety)
    await update_vote_markup(context, channel_id_numeric, message_id, current_vote_count)
    
    # Schedule membership re-check (Auto-removal mechanism)
    job_name = f"recheck_{user_id}_{channel_id_numeric}_{message_id}"
    
    # Remove existing job if it exists (e.g., in case of rapid votes or re-attempts, though not expected here)
    current_jobs = context.job_queue.get_jobs_by_name(job_name)
    for job in current_jobs:
        job.schedule_removal()
    
    # Schedule the new job
    context.job_queue.run_once(
        schedule_membership_recheck_job,
        when=timedelta(minutes=5),
        data={'user_id': user_id, 'channel_id': channel_id_numeric, 'message_id': message_id},
        name=job_name
    )
    
    logger.info("Vote successfully registered for user %s. Recheck scheduled.", user_id)


# ============================
# 7. Status and Auxiliary Handlers
# ============================

async def my_polls_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user's votes and managed channels."""
    query = update.callback_query
    if query:
        await query.answer()

    user_id = update.effective_user.id
    logger.info("User %s requested my_polls_list.", user_id)
    
    message = "**📊 Your Voting Dashboard**\n━━━━━━━━━━━━━━━━━━━━\n\n"
    
    # --- User Votes ---
    user_votes = VOTES_TRACKER.get(user_id, {})
    total_votes = sum(len(messages) for messages in user_votes.values())
    
    if total_votes > 0:
        message += f"**🗳️ Total Votes Cast:** {total_votes}\n"
        
        for channel_id, messages in user_votes.items():
            channel_title = "Unknown Channel"
            channel_username = None
            if channel_id in MANAGED_CHANNELS:
                channel = MANAGED_CHANNELS[channel_id]
                channel_title = channel.title
                channel_username = getattr(channel, "username", None)
                
            channel_link = f"[{channel_title}](https://t.me/{channel_username})" if channel_username else f"`{channel_title}`"
            
            message += f"• **{channel_link}:** {len(messages)} vote(s)\n"
    else:
        message += "**🗳️ आपने अभी तक कोई वोट नहीं किया है।**\n"

    # --- Managed Channels ---
    if MANAGED_CHANNELS:
        message += "\n**👑 Managed Channels (Owned):**\n"
        for c_id, chat in MANAGED_CHANNELS.items():
            total_channel_votes = sum(VOTES_COUNT.get(c_id, {}).values())
            
            # Using the Chat object's properties for a cleaner display
            uname = getattr(chat, "username", None)
            channel_link = f"[{chat.title}](https://t.me/{uname})" if uname else chat.title
            
            message += f"• {channel_link}\n"
            message += f"  └─ Total tracked votes: **{total_channel_votes}**\n"
    
    message += "\n*🔄 वोट ऑटोमैटिक हट जाएगा अगर आप चैनल छोड़ देते हैं।*"
    
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=message,
        parse_mode=ParseMode.MARKDOWN
    )


async def check_bot_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Check bot's current health and configuration."""
    if not update.message:
        return
        
    bot_info = await context.bot.get_me()
    
    total_votes = sum(sum(messages.values()) for messages in VOTES_COUNT.values())
    total_users = len(VOTES_TRACKER)
    total_cache_entries = sum(len(v) for v in MEMBERSHIP_CACHE.values())
    
    # Count of active jobs (membership rechecks)
    active_jobs = len(context.job_queue.get_jobs_by_name(re.compile(r'^recheck_')))
    
    status_message = (
        f"**🤖 Bot Health Status**\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"**✅ General Info:**\n"
        f"• Bot: @{bot_info.username}\n"
        f"• Status: 🟢 Online & Active\n\n"
        f"**📊 Statistics:**\n"
        f"• Managed Channels: **{len(MANAGED_CHANNELS)}**\n"
        f"• Total Tracked Votes: **{total_votes}**\n"
        f"• Active Voters: **{total_users}**\n\n"
        f"**⚙️ System Metrics:**\n"
        f"• Membership Cache Entries: {total_cache_entries}\n"
        f"• Active Recheck Jobs: {active_jobs}\n"
        f"• Cache Duration: {int(CACHE_DURATION.total_seconds() / 60)} minutes\n"
        f"• Host: {'Render (Webhook)' if RENDER_HOSTNAME else 'Polling (Local)'}\n\n"
        f"*System running with advanced error handling & performance optimization.*"
    )
    
    await update.message.reply_text(status_message, parse_mode=ParseMode.MARKDOWN)


async def show_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Provide help guide for users."""
    if not update.message:
        return
        
    help_message = (
        "**📚 Advanced Vote Bot - Complete Guide**\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        "**🔗 1. Create Channel Link:**\n"
        "• `/start` → Click '🔗 Create My Link'\n"
        "• Send your channel @username or ID\n"
        "• **Requirements:** Bot must be Admin with **'Manage Users'** and **'Post Messages'** permissions.\n\n"
        "**🗳️ 2. How Voting Works:**\n"
        "• Users click your link → Start bot\n"
        "• Bot posts a unique tracking message in channel\n"
        "• Users can vote **only if subscribed**\n"
        "• Vote **auto-removes** if user leaves the channel!\n\n"
        "**⚙️ 3. Commands:**\n"
        "• `/start` - Main menu & deep links\n"
        "• `/status` - Bot health check\n"
        "• `/help` - This guide\n"
        "• `/poll [question]? opt1, opt2` - Create a simple poll\n"
        "• `/cancel` - Cancel conversation\n\n"
        "**❓ Need Support?**\n"
        "• Guide: @teamrajweb\n"
        "• Updates: @narzoxbot\n\n"
        "*Built with advanced error handling & performance optimization.*"
    )
    await update.message.reply_text(help_message, parse_mode=ParseMode.MARKDOWN)


# ============================
# 8. Background Tasks & Maintenance
# ============================

async def cleanup_old_cache(context: ContextTypes.DEFAULT_TYPE):
    """Periodic task to clean up old cache entries based on CACHE_DURATION * 2."""
    current_time = datetime.now()
    cleaned = 0
    inactivity_threshold = CACHE_DURATION * 2
    
    # Iterate over a copy of the outer dictionary keys to allow modification
    for user_id in list(MEMBERSHIP_CACHE.keys()):
        # Iterate over a copy of the inner dictionary keys
        for channel_id in list(MEMBERSHIP_CACHE[user_id].keys()):
            _, last_check = MEMBERSHIP_CACHE[user_id][channel_id]
            if current_time - last_check > inactivity_threshold: 
                del MEMBERSHIP_CACHE[user_id][channel_id]
                cleaned += 1
        
        # Remove the user entry if no channels are left
        if not MEMBERSHIP_CACHE[user_id]:
            del MEMBERSHIP_CACHE[user_id]
    
    if cleaned > 0:
        logger.info("Cleaned %d old cache entries. Total users in cache: %d", cleaned, len(MEMBERSHIP_CACHE))
    else:
        logger.debug("No old cache entries to clean.")


# ============================
# 9. Error Handlers
# ============================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Log errors and handle gracefully."""
    logger.error("Exception while handling update: %s", context.error)
    
    # Ignore "Message is not modified" errors which are common with edit_message_reply_markup
    if isinstance(context.error, BadRequest) and "Message is not modified" in str(context.error):
        return

    # Graceful error reply to the user if a message/query context exists
    if isinstance(update, Update):
        effective_chat = update.effective_chat
        
        if effective_chat:
            error_message = (
                "⚠️ **An unexpected error occurred!**\n\n"
                "Please try again. If the problem persists, please contact support: @teamrajweb"
            )
            try:
                # Use send_message or answer callback query depending on context
                if update.callback_query:
                    await update.callback_query.answer(text="⚠️ An error occurred.", show_alert=True)
                elif effective_chat.type == Chat.PRIVATE:
                    await effective_chat.send_message(error_message, parse_mode=ParseMode.MARKDOWN)
            except Exception as e:
                logger.error("Failed to send error message to user: %s", e)


# ============================
# 10. Main Application Setup
# ============================

def build_application() -> Application:
    """Configure and return Application."""
    logger.info("Building application and handlers.")
    
    # Set the parse mode globally for consistent messaging
    app = Application.builder().token(BOT_TOKEN).build()

    # --- Command Handlers ---
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("poll", create_poll))
    app.add_handler(CommandHandler("status", check_bot_status))
    app.add_handler(CommandHandler("help", show_help))
    app.add_handler(CommandHandler("cancel", cancel))

    # --- Callback Query Handlers ---
    app.add_handler(CallbackQueryHandler(handle_vote, pattern=r'^vote_(-?\d+)_(\d+)$'))
    app.add_handler(CallbackQueryHandler(my_polls_list, pattern='^my_polls_list$'))

    # --- Conversation Handler for Link Generation ---
    link_conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(start_channel_poll_conversation_cb, pattern='^start_channel_conv$'),
        ],
        states={
            GET_CHANNEL_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_channel_id)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        allow_reentry=False
    )
    app.add_handler(link_conv_handler)

    # --- Error Handler ---
    app.add_error_handler(error_handler)

    # --- Background Tasks (JobQueue) ---
    app.job_queue.run_repeating(
        cleanup_old_cache, 
        interval=timedelta(minutes=10), 
        first=timedelta(minutes=1), # Start cleanup shortly after startup
        name="periodic_cache_cleanup"
    )

    return app


def main():
    """Main function to run the bot in webhook or polling mode."""
    app = build_application()

    logger.info("=" * 50)
    logger.info("🚀 Advanced Voting Bot Starting...")
    logger.info("=" * 50)
    
    if RENDER_HOSTNAME:
        # Webhook mode for cloud deployment (e.g., Render)
        webhook_base = RENDER_HOSTNAME if RENDER_HOSTNAME.startswith("http") else f"https://{RENDER_HOSTNAME}"
        # Use a unique path component for the webhook URL (like BOT_TOKEN)
        webhook_url = webhook_base.rstrip("/") + f"/{BOT_TOKEN}"
        logger.info("Starting in WEBHOOK mode. URL_PATH=%s PORT=%s", BOT_TOKEN, PORT)
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=webhook_url
        )
    else:
        # Polling mode (local development)
        logger.info("Starting in POLLING mode (local/dev).")
        app.run_polling(poll_interval=2, allowed_updates=None)


if __name__ == '__main__':
    main()
