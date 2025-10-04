# main.py
import os
import re
import logging
import asyncio
from collections import defaultdict
from dotenv import load_dotenv
from typing import Optional, Tuple

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest, Forbidden

from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# -------------------------
# Load env & logging
# -------------------------
load_dotenv()
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
IMAGE_URL: str = os.getenv("IMAGE_URL", "https://picsum.photos/800/400")
LOG_CHANNEL_USERNAME: str = os.getenv("LOG_CHANNEL_USERNAME", "")  # e.g. @yourlogchannel (optional)

if not BOT_TOKEN:
    raise SystemExit("Error: BOT_TOKEN environment variable is required.")

PORT = int(os.environ.get("PORT", 8443))
RENDER_HOSTNAME = os.environ.get("RENDER_EXTERNAL_HOSTNAME")  # Render sets this variable automatically

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# -------------------------
# Conversation states
# -------------------------
(GET_CHANNEL_ID,) = range(1)

# -------------------------
# In-memory storage
# key = (channel_id, message_id) -> set(user_id)
VOTES_PER_POST: dict = defaultdict(set)
# locks per post to avoid race conditions
LOCKS: dict = defaultdict(lambda: asyncio.Lock())
# -------------------------

# -------------------------
# Helpers
# -------------------------
def make_post_key(channel_id: int, message_id: int) -> tuple:
    return (int(channel_id), int(message_id))

def vote_button_text_for(channel_id: int, message_id: int) -> str:
    key = make_post_key(channel_id, message_id)
    count = len(VOTES_PER_POST.get(key, set()))
    return f"✅ Vote Now ({count} Votes)"

def build_vote_keyboard(channel_id: int, message_id: int, channel_url: Optional[str] = None) -> InlineKeyboardMarkup:
    callback_data = f"vote_{channel_id}_{message_id}"
    keyboard = [[InlineKeyboardButton(vote_button_text_for(channel_id, message_id), callback_data=callback_data)]]
    if channel_url:
        keyboard.append([InlineKeyboardButton("➡️ Go to Channel", url=channel_url)])
    return InlineKeyboardMarkup(keyboard)

def parse_poll_from_text(text: str) -> Optional[Tuple[str, list]]:
    """(Optional) parse a /poll command like: /poll Question? opt1, opt2, opt3"""
    if not text or "?" not in text:
        return None
    try:
        question_part, options_part = text.split("?", 1)
        question = question_part.strip()
        options = [opt.strip() for opt in re.split(r",\s*", options_part) if opt.strip()]
        if not question or len(options) < 2 or len(options) > 10:
            return None
        return question, options
    except Exception:
        logger.exception("parse_poll_from_text failed")
        return None

# -------------------------
# Send start message helper
# -------------------------
async def send_start_message(update: Update, context: ContextTypes.DEFAULT_TYPE, reply_markup: InlineKeyboardMarkup, welcome_message: str, chat_id: Optional[int] = None):
    target_chat = chat_id if chat_id is not None else update.effective_chat.id
    try:
        await context.bot.send_photo(chat_id=target_chat, photo=IMAGE_URL, caption=welcome_message, parse_mode="Markdown", reply_markup=reply_markup)
    except Exception as e:
        logger.warning("Sending photo failed, sending text fallback: %s", e)
        try:
            await context.bot.send_message(chat_id=target_chat, text=welcome_message, parse_mode="Markdown", reply_markup=reply_markup)
        except Exception:
            logger.exception("Failed to send fallback welcome message")

# -------------------------
# /start handler (deep-link support)
# -------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    bot_user = await context.bot.get_me()
    bot_username = bot_user.username or "bot"

    # Deep link handling: if user clicked t.me/<bot>?start=link_<channelId>
    if context.args:
        payload = context.args[0]
        match = re.match(r"link_(\d+)", payload)
        if match:
            channel_id_str = match.group(1)
            target_channel_id_numeric = int(f"-100{channel_id_str}")
            try:
                chat_info = await context.bot.get_chat(chat_id=target_channel_id_numeric)
                channel_title = chat_info.title
                channel_url = chat_info.invite_link or (f"https://t.me/{chat_info.username}" if chat_info.username else None)

                await update.message.reply_text(
                    f"✨ **Connected to `{channel_title}`**\n"
                    "यदि चैनल पर वोट पोस्ट मौजूद है तो आप वोट कर पाएँगे।",
                    parse_mode="Markdown",
                )

                # Send a join-notification post to the channel and attach a vote button which references this message_id
                notify_caption = (
                    f"**👑 नया सदस्य जुड़ा!**\n\n"
                    f"👤 [{user.first_name}](tg://user?id={user.id})  •  `ID: {user.id}`\n\n"
                    "नीचे क्लिक कर के वोट दें (केवल चैनल के सदस्य वोट कर सकते हैं)।"
                )

                sent_msg = await context.bot.send_photo(chat_id=target_channel_id_numeric, photo=IMAGE_URL, caption=notify_caption, parse_mode="Markdown")
                # Edit to add keyboard referencing this message id
                markup = build_vote_keyboard(target_channel_id_numeric, sent_msg.message_id, channel_url)
                try:
                    await context.bot.edit_message_reply_markup(chat_id=target_channel_id_numeric, message_id=sent_msg.message_id, reply_markup=markup)
                except Exception as e:
                    logger.warning("Failed to attach markup to notification: %s", e)
                return
            except Exception as e:
                logger.exception("Deep link notify failed")
                await update.message.reply_text("चैनल से कनेक्ट करने में त्रुटि हुई। सुनिश्चित करें कि बॉट चैनल का एडमिन है।")

    # Regular start menu
    keyboard = [
        [InlineKeyboardButton("🔗 अपनी लिंक बनाएँ", callback_data="start_channel_conv"),
         InlineKeyboardButton("➕ ग्रुप में जोड़ें", url=f"https://t.me/{bot_username}?startgroup=true")],
        [InlineKeyboardButton("📊 मेरे वोट्स", callback_data="my_polls_list"),
         InlineKeyboardButton("❓ गाइड", url="https://t.me/teamrajweb")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    welcome_message = (
        "**👑 वोट बॉट में आपका स्वागत है!**\n\n"
        "चैनल जोड़ें और यूनिक लिंक बनाएं ताकि उपयोगकर्ता उस चैनल से जुड़कर वोट कर सकें।"
    )
    await send_start_message(update, context, reply_markup, welcome_message)

# -------------------------
# /poll - optional simple poll creator
# -------------------------
async def create_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parsed = parse_poll_from_text(" ".join(context.args))
    if not parsed:
        await update.message.reply_text("Use: `/poll Question? opt1, opt2` (2-10 options)", parse_mode="Markdown")
        return
    q, options = parsed
    try:
        await context.bot.send_poll(chat_id=update.effective_chat.id, question=q, options=options, is_anonymous=False)
        await update.message.reply_text("✅ Poll created")
    except Exception:
        logger.exception("Failed to create poll")
        await update.message.reply_text("Failed to create poll.")

# -------------------------
# Conversation: ask channel id/username and give share link
# -------------------------
async def start_channel_poll_conversation_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await context.bot.send_message(chat_id=update.effective_chat.id, text="कृपया चैनल का @username या numeric ID (-100...) भेजें जहाँ मैं एडमिन हूँ।")
    return GET_CHANNEL_ID

async def get_channel_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    channel_id_input = update.message.text.strip()
    user = update.effective_user

    if re.match(r"^-?\d+$", channel_id_input):
        channel_id = int(channel_id_input)
    else:
        channel_id = channel_id_input if channel_id_input.startswith("@") else f"@{channel_id_input}"

    try:
        bot_user = await context.bot.get_me()
        chat_member = await context.bot.get_chat_member(chat_id=channel_id, user_id=bot_user.id)
        chat_info = await context.bot.get_chat(chat_id=channel_id)

        if getattr(chat_member, "status", "").lower() in ["administrator", "creator"]:
            raw_id_str = str(chat_info.id)
            link_channel_id = raw_id_str[4:] if raw_id_str.startswith("-100") else raw_id_str.replace("-", "")
            deep_link_payload = f"link_{link_channel_id}"
            share_url = f"https://t.me/{bot_user.username}?start={deep_link_payload}"
            await update.message.reply_text(f"✅ चैनल कनेक्ट हो गया!\n\nShare link:\n`{share_url}`", parse_mode="Markdown")

            if LOG_CHANNEL_USERNAME:
                log_message = f"🔗 New channel link created by [{user.first_name}](tg://user?id={user.id}) for `{chat_info.title}`\n{share_url}"
                try:
                    await context.bot.send_message(chat_id=LOG_CHANNEL_USERNAME, text=log_message, parse_mode="Markdown")
                except Exception:
                    logger.warning("Failed to send log message to LOG_CHANNEL_USERNAME")
            return ConversationHandler.END
        else:
            await update.message.reply_text("मैं आपके चैनल का एडमिन नहीं हूँ। मुझे एडमिन बनाकर फिर कोशिश करें।")
            return GET_CHANNEL_ID
    except Exception:
        logger.exception("get_channel_id failed")
        await update.message.reply_text("चैनल एक्सेस त्रुटि। सुनिश्चित करें कि चैनल सही है और बॉट एडमिन है।")
        return GET_CHANNEL_ID

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("कन्वर्सेशन रद्द कर दिया गया।")
    return ConversationHandler.END

# -------------------------
# Vote callback handler (pattern: vote_<channel_id>_<message_id>)
# -------------------------
async def handle_vote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()  # quick ack

    data = query.data or ""
    m = re.match(r"^vote_(-?\d+)_(\d+)$", data)
    if not m:
        return await query.answer(text="❌ त्रुटि: वोट ID सही नहीं है।", show_alert=True)

    channel_id_numeric = int(m.group(1))
    message_id = int(m.group(2))
    user = query.from_user
    user_id = user.id

    # Ignore bots
    if user.is_bot:
        return await query.answer(text="🤖 बॉट वोट नहीं कर सकते।", show_alert=True)

    # 1) Membership check
    try:
        chat_member = await context.bot.get_chat_member(chat_id=channel_id_numeric, user_id=user_id)
        is_subscriber = chat_member.status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]
    except (Forbidden, BadRequest) as e:
        logger.error("Membership check failed: %s", e)
        return await query.answer(text="🚨 वोट जाँच असमर्थ है। बॉट को आवश्यक अनुमतियाँ दें।", show_alert=True)
    except Exception:
        logger.exception("Unknown error during membership check")
        return await query.answer(text="⚠️ API त्रुटि। दोबारा कोशिश करें।", show_alert=True)

    if not is_subscriber:
        # optional: provide invite link if available
        try:
            chat_info = await context.bot.get_chat(chat_id=channel_id_numeric)
            channel_url = chat_info.invite_link or (f"https://t.me/{chat_info.username}" if chat_info.username else None)
        except Exception:
            channel_url = None
        return await query.answer(text="❌ आप वोट नहीं कर सकते — कृपया पहले चैनल/ग्रुप जॉइन करें।", show_alert=True)

    # 2) Lock and record vote (one-per-user per post)
    post_key = make_post_key(channel_id_numeric, message_id)
    lock = LOCKS[post_key]

    async with lock:
        if user_id in VOTES_PER_POST[post_key]:
            return await query.answer(text="🗳️ आप पहले ही इस पोस्ट पर वोट दे चुके हैं।", show_alert=True)

        # record vote
        VOTES_PER_POST[post_key].add(user_id)
        current_vote_count = len(VOTES_PER_POST[post_key])

        # update button label on the message (best-effort)
        try:
            chat_info = await context.bot.get_chat(chat_id=channel_id_numeric)
            channel_url = chat_info.invite_link or (f"https://t.me/{chat_info.username}" if chat_info.username else None)
            new_markup = build_vote_keyboard(channel_id_numeric, message_id, channel_url)
            await query.edit_message_reply_markup(reply_markup=new_markup)
        except BadRequest as e:
            # Message may be deleted or not editable in some contexts; safe to ignore
            logger.info("Failed to edit reply_markup (may be removed): %s", e)
        except Exception:
            logger.exception("Unexpected error updating button")

    await query.answer(text=f"✅ आपका वोट दर्ज हो गया! (कुल {current_vote_count})", show_alert=True)

# -------------------------
# Admin helper: /poststats <channel_id> <message_id>
# -------------------------
async def post_stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("उपयोग: /poststats <channel_id> <message_id>")
        return
    try:
        channel_id = int(args[0])
        message_id = int(args[1])
    except ValueError:
        await update.message.reply_text("channel_id और message_id दोनों संख्याएँ होनी चाहिए।")
        return
    key = make_post_key(channel_id, message_id)
    count = len(VOTES_PER_POST.get(key, set()))
    await update.message.reply_text(f"Post `{message_id}` in `{channel_id}` has *{count}* votes.", parse_mode="Markdown")

# -------------------------
# App setup & webhook start
# -------------------------
def main():
    application = Application.builder().token(BOT_TOKEN).build()

    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("poll", create_poll))
    application.add_handler(CommandHandler("poststats", post_stats_cmd))

    application.add_handler(CallbackQueryHandler(start_channel_poll_conversation_cb, pattern="^start_channel_conv$"))
    application.add_handler(CallbackQueryHandler(handle_vote, pattern=r"^vote_(-?\d+)_(\d+)$"))

    # Conversation for creating channel link
    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_channel_poll_conversation_cb, pattern="^start_channel_conv$")],
        states={GET_CHANNEL_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_channel_id)]},
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=False,
    )
    application.add_handler(conv)

    # run webhook (Render)
    if not RENDER_HOSTNAME:
        logger.error("RENDER_EXTERNAL_HOSTNAME not found in env. Render will normally set this. Running may fail if not present.")
    webhook_url = f"https://{RENDER_HOSTNAME}/{BOT_TOKEN}" if RENDER_HOSTNAME else None

    logger.info("Starting webhook listener. PORT=%s, WEBHOOK_URL=%s", PORT, webhook_url or "<none-provided>")

    # Use url_path=BOT_TOKEN for security (Telegram will POST to this path)
    application.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path=BOT_TOKEN,
        webhook_url=webhook_url,  # Render external hostname + token
    )

if __name__ == "__main__":
    main()
