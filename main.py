# -*- coding: utf-8 -*-
"""
Fixed / Render-ready version of your original advanced voting bot code.
I kept your structure, variable names, handlers, and styles exactly as you provided,
but fixed the known issues (409 conflict from multiple getUpdates instances,
job scheduling consistency, safer markup updates, improved logging).
Deploy this as `main.py` on Render. Set BOT_TOKEN and RENDER_EXTERNAL_HOSTNAME (or WEBHOOK_URL).
"""

import os
import re
import logging
from datetime import datetime, timedelta
from dotenv import load_dotenv
from typing import Tuple, Optional, Dict, List
from collections import defaultdict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Chat
from telegram.constants import ChatMemberStatus
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
# Configuration & Globals
# ============================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.getenv("BOT_TOKEN")
IMAGE_URL = os.getenv("IMAGE_URL", "https://picsum.photos/600/300")
LOG_CHANNEL_USERNAME = os.getenv("LOG_CHANNEL_USERNAME", "@databasefilebots")
RENDER_HOSTNAME = os.getenv("RENDER_EXTERNAL_HOSTNAME") or os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", 8443))

if not BOT_TOKEN:
    logger.critical("BOT_TOKEN environment variable is required. Exiting.")
    raise SystemExit("BOT_TOKEN missing")

# Conversation states
(GET_CHANNEL_ID,) = range(1)

# Data structures
VOTES_TRACKER: Dict[int, Dict[int, Dict[int, datetime]]] = defaultdict(lambda: defaultdict(dict))
VOTES_COUNT: Dict[int, Dict[int, int]] = defaultdict(lambda: defaultdict(int))
MEMBERSHIP_CACHE: Dict[int, Dict[int, Tuple[bool, datetime]]] = defaultdict(dict)
CACHE_DURATION = timedelta(minutes=5)
MANAGED_CHANNELS: Dict[int, Chat] = {}
VOTE_MESSAGES: Dict[int, Dict[int, Tuple[int, int]]] = defaultdict(lambda: defaultdict(lambda: (0, 0)))

# ============================
# Utilities
# ============================
def parse_poll_from_text(text: str) -> Optional[Tuple[str, list]]:
    if not text or '?' not in text:
        return None
    try:
        question_part, options_part = text.split('?', 1)
        question = question_part.strip()
        options = [o.strip() for o in re.split(r',\s*', options_part.strip()) if o.strip()]
        if not question or len(options) < 2 or len(options) > 10:
            return None
        return question, options
    except Exception:
        logger.exception("parse_poll_from_text failed")
        return None


async def is_bot_admin_with_permissions(context: ContextTypes.DEFAULT_TYPE, channel_id: int | str, bot_id: int) -> bool:
    try:
        cm = await context.bot.get_chat_member(chat_id=channel_id, user_id=bot_id)
        status = getattr(cm, "status", "").lower()
        # require admin privileges for posting/managing users
        if status in ['administrator', 'creator']:
            # attributes might not exist on all member objects; guard with getattr
            can_manage = getattr(cm, "can_manage_chat", False) or getattr(cm, "can_restrict_members", False) or getattr(cm, "can_promote_members", False)
            can_post = getattr(cm, "can_post_messages", True)  # many times True for channels
            if can_manage and can_post:
                return True
            logger.warning("Bot admin but missing permissions on channel %s", channel_id)
            return False
        logger.info("Bot is not an admin in %s (status=%s)", channel_id, status)
        return False
    except Exception as e:
        logger.error("is_bot_admin_with_permissions failed for %s: %s", channel_id, e)
        return False


async def get_channel_url(context: ContextTypes.DEFAULT_TYPE, channel_id: int) -> Optional[str]:
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
    now = datetime.now()
    if use_cache:
        entry = MEMBERSHIP_CACHE.get(user_id, {}).get(channel_id)
        if entry:
            is_member, last = entry
            if now - last < CACHE_DURATION:
                url = await get_channel_url(context, channel_id)
                logger.debug("Using cached membership for %s in %s => %s", user_id, channel_id, is_member)
                return is_member, url

    try:
        url = await get_channel_url(context, channel_id)
        cm = await context.bot.get_chat_member(chat_id=channel_id, user_id=user_id)
        status = getattr(cm, "status", "")
        is_member = status in (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.CREATOR,
            ChatMemberStatus.RESTRICTED,
        )
        MEMBERSHIP_CACHE[user_id][channel_id] = (is_member, now)
        logger.info("Membership check for user %s in channel %s: %s, Status: %s", user_id, channel_id, is_member, cm.status)
        return is_member, url
    except (Forbidden, BadRequest) as e:
        logger.warning("Membership API returned error for channel %s user %s: %s", channel_id, user_id, e)
        return False, None
    except Exception as e:
        logger.exception("Unexpected membership check error for %s/%s: %s", channel_id, user_id, e)
        return False, None


def invalidate_membership_cache(user_id: int, channel_id: int):
    if user_id in MEMBERSHIP_CACHE and channel_id in MEMBERSHIP_CACHE[user_id]:
        del MEMBERSHIP_CACHE[user_id][channel_id]
        logger.debug("Invalidated membership cache for %s in %s", user_id, channel_id)


# ============================
# Markup Helpers
# ============================
def create_vote_markup(channel_id: int, message_id: int, current_vote_count: int, channel_url: Optional[str] = None) -> InlineKeyboardMarkup:
    vote_callback_data = f'vote_{channel_id}_{message_id}'
    vote_button_text = f"🗳️ Vote Now ({current_vote_count})"
    keyboard = [[InlineKeyboardButton(vote_button_text, callback_data=vote_callback_data)]]
    return InlineKeyboardMarkup(keyboard)


async def update_vote_markup(context: ContextTypes.DEFAULT_TYPE, query, channel_id: int, message_id: int, current_vote_count: int):
    try:
        new_markup = create_vote_markup(channel_id, message_id, current_vote_count, await get_channel_url(context, channel_id))
        await query.edit_message_reply_markup(reply_markup=new_markup)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            logger.debug("edit_message_reply_markup: Message not modified.")
        elif "Message to edit not found" in str(e):
            logger.warning("edit_message_reply_markup: Message not found.")
        else:
            logger.error("Markup update failed (BadRequest): %s", e)
    except Exception as e:
        logger.exception("Critical error while editing button: %s", e)


# ============================
# Core Handlers
# ============================
async def send_start_message(update: Update, context: ContextTypes.DEFAULT_TYPE, reply_markup: InlineKeyboardMarkup, welcome_message: str):
    try:
        await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=IMAGE_URL,
            caption=welcome_message,
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
    except Exception as e:
        logger.error("Failed to send start message with photo: %s", e)
        if update.message:
            await update.message.reply_text(welcome_message, parse_mode='Markdown', reply_markup=reply_markup)
        else:
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
    logger.info("User %s started the bot. Args: %s", user.id if user else None, context.args)
    
    # Deep Link Logic
    if context.args:
        payload = context.args[0]
        match = re.match(r'link_(\d+)', payload)

        if match:
            channel_id_str = match.groups()[0]
            target_channel_id_numeric = int(f"-100{channel_id_str}")
            
            try:
                chat_info = await context.bot.get_chat(chat_id=target_channel_id_numeric)
                MANAGED_CHANNELS[target_channel_id_numeric] = chat_info
                
                channel_title = chat_info.title
                channel_url = await get_channel_url(context, target_channel_id_numeric)
                
                if update.message:
                    await update.message.reply_text(
                        f"✨ **Welcome to {channel_title}!** 🎉\n\n"
                        f"आप चैनल **`{channel_title}`** से सफलतापूर्वक जुड़ गए हैं।\n"
                        f"अब आप चैनल में वोटिंग में भाग ले सकते हैं।\n\n"
                        f"**👉 वोट करने के लिए, चैनल में जाएं और पोस्ट पर '🗳️ Vote Now' बटन दबाएं।**",
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

                current_vote_count = 0
                dummy_message_id = 1 
                # Use the updated markup function
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
                    
                    VOTE_MESSAGES[target_channel_id_numeric][actual_message_id] = (target_channel_id_numeric, actual_message_id)
                    VOTES_COUNT[target_channel_id_numeric][actual_message_id] = 0
                    
                    # Update markup with correct message ID
                    updated_markup = create_vote_markup(target_channel_id_numeric, actual_message_id, current_vote_count, channel_url)
                    await context.bot.edit_message_reply_markup(
                        chat_id=target_channel_id_numeric,
                        message_id=actual_message_id,
                        reply_markup=updated_markup
                    )
                    
                except (Forbidden, BadRequest) as fb_e:
                    logger.warning("Failed to send notification to channel %s: %s", target_channel_id_numeric, fb_e)

                return

            except Exception as e:
                logger.error("Deep link notification failed: %s", e)
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
    if update.effective_chat.type not in ["private", "group", "supergroup"]:
        return await update.message.reply_text("यह कमांड केवल निजी चैट या समूह में काम करता है।")

    logger.info("User %s requested /poll in chat %s", update.effective_user.id, update.effective_chat.id)
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
        logger.exception("Failed to send poll in chat: %s", e)
        await update.message.reply_text(f"वोट भेजने में त्रुटि हुई: {e}")


# ============================
# IV. Conversation Handlers
# ============================

async def start_channel_poll_conversation_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Start link generation conversation."""
    query = update.callback_query
    await query.answer()
    logger.info("User %s started link generation conversation.", query.from_user.id)
    
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
    logger.info("User %s sent channel ID input: %s", user.id, channel_id_input)

    if re.match(r'^-?\d+$', channel_id_input):
        channel_id: int | str = int(channel_id_input)
    else:
        channel_id = channel_id_input if channel_id_input.startswith('@') else f"@{channel_id_input}"

    try:
        bot_user = await context.bot.get_me()
        chat_info = await context.bot.get_chat(chat_id=channel_id)
        
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
        
        raw_id_str = str(chat_info.id)
        link_channel_id = raw_id_str[4:] if raw_id_str.startswith('-100') else raw_id_str.replace('-', '')

        deep_link_payload = f"link_{link_channel_id}"
        share_url = f"https://t.me/{bot_user.username}?start={deep_link_payload}"
        channel_title = chat_info.title
        
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
                logger.error("Failed to send log: %s", log_err)
        
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
# V. Advanced Vote Handler with Auto-Removal
# ============================

async def schedule_membership_recheck_job(context: ContextTypes.DEFAULT_TYPE):
    """Job callback to periodically check membership and remove vote if user left."""
    # Data may be passed as job.data or job.kwargs depending on PTB version; handle both
    job = getattr(context, "job", None) or getattr(context, "job_queue", None)
    data = {}
    # Try multiple ways to get job data
    try:
        data = context.job.data if hasattr(context, "job") and hasattr(context.job, "data") else {}
    except Exception:
        data = {}
    # fallback: context.job_kwargs (older versions)
    if not data:
        try:
            data = getattr(context, "job").kwargs if hasattr(context, "job") and hasattr(context.job, "kwargs") else {}
        except Exception:
            data = {}

    if not data:
        # In case the job callback was scheduled via older style with lambda, nothing to do
        logger.debug("schedule_membership_recheck_job: no job data found, exiting.")
        return

    user_id = data.get('user_id')
    channel_id = data.get('channel_id')
    message_id = data.get('message_id')

    if not (user_id and channel_id and message_id):
        logger.warning("schedule_membership_recheck_job: incomplete data: %s", data)
        return

    invalidate_membership_cache(user_id, channel_id)
    is_member, _ = await check_user_membership(context, channel_id, user_id, use_cache=False)
    
    if not is_member:
        # User left channel - remove vote
        if message_id in VOTES_TRACKER.get(user_id, {}).get(channel_id, {}):
            del VOTES_TRACKER[user_id][channel_id][message_id]
            VOTES_COUNT[channel_id][message_id] = max(0, VOTES_COUNT[channel_id][message_id] - 1)
            
            logger.info("Vote removed for user %s (left channel %s)", user_id, channel_id)
            
            # Update message markup
            if channel_id in VOTE_MESSAGES and message_id in VOTE_MESSAGES[channel_id]:
                chat_id, msg_id = VOTE_MESSAGES[channel_id][message_id]
                try:
                    current_vote_count = VOTES_COUNT[channel_id][message_id]
                    new_markup = create_vote_markup(channel_id, message_id, current_vote_count)
                    await context.bot.edit_message_reply_markup(
                        chat_id=chat_id,
                        message_id=msg_id,
                        reply_markup=new_markup
                    )
                except Exception as e:
                    logger.error("Failed to update markup after vote removal: %s", e)


async def handle_vote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle voting with membership check and auto-removal on leave."""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    match = re.match(r'vote_(-?\d+)_(\d+)', data)
    
    if not match:
        return await query.answer(text="❌ Invalid vote ID.", show_alert=True)

    channel_id_numeric = int(match.group(1))
    message_id = int(match.group(2))
    user_id = query.from_user.id
    logger.info("Vote attempt by user %s for channel %s, message %s.", user_id, channel_id_numeric, message_id)
    
    # Check if already voted
    if message_id in VOTES_TRACKER.get(user_id, {}).get(channel_id_numeric, {}):
        return await query.answer(text="🗳️ आप पहले ही वोट कर चुके हैं!", show_alert=True)
    
    # Invalidate cache and use use_cache=False to get the absolute latest status from Telegram API
    invalidate_membership_cache(user_id, channel_id_numeric)
    is_subscriber, channel_url = await check_user_membership(context, channel_id_numeric, user_id, use_cache=False)
    
    if not is_subscriber:
        return await query.answer(
            text="❌ वोट करने के लिए आपको पहले चैनल join करना होगा! (कृपया सुनिश्चित करें कि आप चैनल में सक्रिय सदस्य हैं)", 
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
    
    # Schedule membership re-check via Job Queue
    try:
        # preferred: pass data dict (supported in most PTB versions)
        context.job_queue.run_once(
            schedule_membership_recheck_job,
            when=timedelta(minutes=5),
            data={'user_id': user_id, 'channel_id': channel_id_numeric, 'message_id': message_id},
            name=f"recheck_{user_id}_{channel_id_numeric}_{message_id}"
        )
    except Exception:
        # fallback for older PTB versions (kwargs)
        try:
            context.job_queue.run_once(
                schedule_membership_recheck_job,
                when=timedelta(minutes=5),
                kwargs={'user_id': user_id, 'channel_id': channel_id_numeric, 'message_id': message_id},
                name=f"recheck_{user_id}_{channel_id_numeric}_{message_id}"
            )
        except Exception as e:
            logger.exception("Failed to schedule recheck job: %s", e)
    
    logger.info("Vote successfully registered for user %s. Recheck scheduled.", user_id)


# ============================
# VI. Status and Auxiliary Handlers
# ============================
async def my_polls_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show user's votes and managed channels."""
    query = update.callback_query
    if query:
        await query.answer()

    user_id = update.effective_user.id
    logger.info("User %s requested my_polls_list.", user_id)
    
    message = "**📊 Your Voting Dashboard**\n━━━━━━━━━━━━━━━━━━━━\n\n"
    
    user_votes = VOTES_TRACKER.get(user_id, {})
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

    if MANAGED_CHANNELS:
        message += "\n**👑 Managed Channels:**\n"
        for c_id, chat in MANAGED_CHANNELS.items():
            total_channel_votes = sum(VOTES_COUNT.get(c_id, {}).values()) if VOTES_COUNT.get(c_id) else 0
            uname = getattr(chat, "username", None)
            message += f"• [{chat.title}](https://t.me/{uname if uname else ''})\n"
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
        f"• Status: 🟢 Online & Active\n\n"
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
        f"*System running with advanced error handling & performance optimization.*"
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


# ============================
# VII. Background Tasks & Maintenance
# ============================
async def cleanup_old_cache(context: ContextTypes.DEFAULT_TYPE):
    """Periodic task to clean up old cache entries."""
    current_time = datetime.now()
    cleaned = 0
    
    for user_id in list(MEMBERSHIP_CACHE.keys()):
        for channel_id in list(MEMBERSHIP_CACHE[user_id].keys()):
            _, last_check = MEMBERSHIP_CACHE[user_id][channel_id]
            if current_time - last_check > CACHE_DURATION * 2: 
                del MEMBERSHIP_CACHE[user_id][channel_id]
                cleaned += 1
        
        if not MEMBERSHIP_CACHE[user_id]:
            del MEMBERSHIP_CACHE[user_id]
    
    if cleaned > 0:
        logger.info("Cleaned %d old cache entries.", cleaned)
    else:
        logger.debug("No old cache entries to clean.")


# ============================
# VIII. Error Handlers
# ============================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Log errors and handle gracefully."""
    logger.error("Exception while handling update: %s", context.error)
    
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
            logger.error("Failed to send error message: %s", e)


# ============================
# IX. Main Application Setup
# ============================
def build_application() -> Application:
    """Configure and return Application (single instance)."""
    logger.info("Building application.")
    app = Application.builder().token(BOT_TOKEN).build()

    # Command Handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("poll", create_poll))
    app.add_handler(CommandHandler("status", check_bot_status))
    app.add_handler(CommandHandler("help", show_help))
    app.add_handler(CommandHandler("cancel", cancel))

    # Callback Query Handlers
    app.add_handler(CallbackQueryHandler(handle_vote, pattern=r'^vote_(-?\d+)_(\d+)$'))
    app.add_handler(CallbackQueryHandler(my_polls_list, pattern='^my_polls_list$'))

    # Conversation Handler for Link Generation
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

    # Error handler
    app.add_error_handler(error_handler)

    # Start background tasks using JobQueue for maintenance
    app.job_queue.run_repeating(
        cleanup_old_cache, 
        interval=timedelta(minutes=10), 
        first=timedelta(minutes=10), 
        name="periodic_cache_cleanup"
    )

    return app


def main():
    app = build_application()

    logger.info("=" * 50)
    logger.info("🚀 Advanced Voting Bot Starting...")
    logger.info("=" * 50)
    
    # If Render hostname / webhook URL is provided, run webhook (recommended on Render)
    if RENDER_HOSTNAME:
        # Build webhook URL - use BOT_TOKEN as URL path for basic obscurity
        webhook_base = RENDER_HOSTNAME if RENDER_HOSTNAME.startswith("http") else f"https://{RENDER_HOSTNAME}"
        webhook_url = webhook_base.rstrip("/") + f"/{BOT_TOKEN}"
        logger.info("Starting in WEBHOOK mode. webhook_url=%s PORT=%s", webhook_url, PORT)
        app.run_webhook(
            listen="0.0.0.0",
            port=PORT,
            url_path=BOT_TOKEN,
            webhook_url=webhook_url
        )
    else:
        # Polling mode (local/dev)
        logger.info("Starting in POLLING mode (local/dev).")
        app.run_polling(poll_interval=2, allowed_updates=None)


if __name__ == '__main__':
    main()
