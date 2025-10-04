import os
import re
import logging
import asyncio
from dotenv import load_dotenv
from collections import defaultdict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest, Forbidden

from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    CallbackQueryHandler
)

# ------------------------------------------------
# Config / Env
# ------------------------------------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
IMAGE_URL = os.getenv("IMAGE_URL", "https://picsum.photos/600/300")
LOG_CHANNEL_USERNAME = os.getenv("LOG_CHANNEL_USERNAME", "@teamrajweb")

# Conversation states
(GET_CHANNEL_ID,) = range(1)

# ------------------------------------------------
# In-memory vote stores (no DB)
# Key for a post: tuple(channel_id, message_id)
# VOTES_PER_POST[(channel_id, message_id)] = set(user_id, ...)
# LOCKS[(channel_id, message_id)] = asyncio.Lock()
# ------------------------------------------------
VOTES_PER_POST = defaultdict(set)
LOCKS = defaultdict(lambda: asyncio.Lock())

# ------------------------------------------------
# Logging
# ------------------------------------------------
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# ------------------------------------------------
# Helpers
# ------------------------------------------------
def make_post_key(channel_id: int, message_id: int) -> tuple:
    return (int(channel_id), int(message_id))

def vote_button_text_for(channel_id: int, message_id: int) -> str:
    key = make_post_key(channel_id, message_id)
    count = len(VOTES_PER_POST.get(key, []))
    return f"✅ Vote Now ({count} Votes)"

def build_vote_keyboard(channel_id: int, message_id: int, channel_url: str | None = None):
    callback_data = f"vote_{channel_id}_{message_id}"
    keyboard = [[InlineKeyboardButton(vote_button_text_for(channel_id, message_id), callback_data=callback_data)]]
    if channel_url:
        keyboard.append([InlineKeyboardButton("➡️ Go to Channel", url=channel_url)])
    return InlineKeyboardMarkup(keyboard)

def parse_poll_from_text(text: str):
    """Existing helper (kept)"""
    if not text or '?' not in text:
        return None
    try:
        question_part, options_part = text.split('?', 1)
        question = question_part.strip()
        options_part = options_part.strip()
        options = [opt.strip() for opt in re.split(r',\s*', options_part) if opt.strip()]
        if not question or len(options) < 2 or len(options) > 10:
            return None
        return question, options
    except Exception:
        logging.exception("parse_poll_from_text failed")
        return None

# ------------------------------------------------
# Send start message (stylish)
# ------------------------------------------------
async def send_start_message(update: Update, context: ContextTypes.DEFAULT_TYPE, reply_markup: InlineKeyboardMarkup, welcome_message: str, chat_id: int | None = None):
    target_chat_id = chat_id if chat_id else update.effective_chat.id
    try:
        await context.bot.send_photo(
            chat_id=target_chat_id,
            photo=IMAGE_URL,
            caption=welcome_message,
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
    except Exception as e:
        logging.error(f"Image send failed: {e}. Sending text message instead.")
        try:
            await context.bot.send_message(
                chat_id=target_chat_id,
                text=welcome_message,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
        except Exception:
            logging.exception("Failed to send fallback welcome message")

# ------------------------------------------------
# /start handler (deep link support) — when bot posts a notification to the channel,
# we capture the sent message_id and attach it to callback_data so votes are per-post.
# ------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    bot_user = await context.bot.get_me()
    bot_username = bot_user.username

    # Deep link handling (user clicked custom share link)
    if context.args:
        payload = context.args[0]
        match = re.match(r'link_(\d+)', payload)
        if match:
            channel_id_str = match.groups()[0]
            target_channel_id_numeric = int(f"-100{channel_id_str}")
            current_vote_count = 0  # will be per-post once message exists

            try:
                chat_info = await context.bot.get_chat(chat_id=target_channel_id_numeric)
                channel_title = chat_info.title
                channel_url = chat_info.invite_link or (f"https://t.me/{chat_info.username}" if chat_info.username else None)

                await update.message.reply_text(
                    f"✨ **You've Successfully Connected!** 🎉\n\n"
                    f"आप चैनल **`{channel_title}`** से सफलतापूर्वक जुड़ गए हैं।\n"
                    f"यह लिंक अब सक्रिय है — अगर चैनल पर वोट पोस्ट मौजूद है तो आप वोट कर पाएँगे।",
                    parse_mode='Markdown'
                )

                # Notify the channel — create a NEW vote post with a button which contains its message_id
                notification_message = (
                    f"**👑 New Participant Joined! 👑**\n\n"
                    f"👤 [{user.first_name}](tg://user?id={user.id})  •  `ID: {user.id}`\n"
                    f"🌐 Username: {f'@{user.username}' if user.username else 'N/A'}\n\n"
                    f"➡️ Click below to allow this user to vote (only joined members can vote)."
                )

                try:
                    sent = await context.bot.send_photo(
                        chat_id=target_channel_id_numeric,
                        photo=IMAGE_URL,
                        caption=notification_message,
                        parse_mode='Markdown'
                    )
                    # Build keyboard now that we have sent.message_id
                    channel_markup = build_vote_keyboard(target_channel_id_numeric, sent.message_id, channel_url)
                    await context.bot.edit_message_reply_markup(
                        chat_id=target_channel_id_numeric,
                        message_id=sent.message_id,
                        reply_markup=channel_markup
                    )
                except (Forbidden, BadRequest) as fb_e:
                    logging.warning(f"Failed to send notification to channel {target_channel_id_numeric}: {fb_e}")

                return

            except Exception as e:
                logging.error(f"Deep link notification failed: {e}")
                await update.message.reply_text("माफ़ करना, चैनल से जुड़ने/सूचना भेजने में त्रुटि हुई। सुनिश्चित करें कि बॉट चैनल का एडमिन है और सही अनुमतियाँ प्राप्त हैं।")

    # Regular start menu
    keyboard = [
        [
            InlineKeyboardButton("🔗 अपनी लिंक बनाएँ", callback_data='start_channel_conv'),
            InlineKeyboardButton("➕ ग्रुप में जोड़ें", url=f"https://t.me/{bot_username}?startgroup=true")
        ],
        [
            InlineKeyboardButton("📊 मेरे वोट्स", callback_data='my_polls_list'),
            InlineKeyboardButton("❓ गाइड", url='https://t.me/teamrajweb'),
            InlineKeyboardButton("📢 चैनल", url='https://t.me/narzoxbot')
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    welcome_message = (
        "**👑 वोट बॉट में आपका स्वागत है! 👑**\n\n"
        "चैनल को कनेक्ट कर *Instant Share Link* पाने के लिए '🔗 अपनी लिंक बनाएँ' पर क्लिक करें।\n\n"
        "*\"आपका वोट आपकी आवाज़ है\"*"
    )

    await send_start_message(update, context, reply_markup, welcome_message)

# ------------------------------------------------
# Create a simple poll in chat (kept from original)
# ------------------------------------------------
async def create_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parsed = parse_poll_from_text(" ".join(context.args))
    if not parsed:
        await update.message.reply_text(
            "कृपया सही फॉर्मेट का उपयोग करें:\n"
            "`/poll [सवाल]? [ऑप्शन1], [ऑप्शन2], ...`\n"
            "कम से कम 2 और अधिकतम 10 ऑप्शन दें।",
            parse_mode='Markdown'
        )
        return

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
        logging.exception("Failed to send poll in chat")
        await update.message.reply_text(f"वोट भेजने में त्रुटि हुई: {e}")

# ------------------------------------------------
# Conversation: ask for channel id/username and create share link
# ------------------------------------------------
async def start_channel_poll_conversation_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="👋 **चैनल लिंक सेटअप:**\nकृपया उस चैनल का @username या ID (`-100...`) भेजें जिसके लिए आप लिंक जनरेट करना चाहते हैं।\n\n**नोट:** मुझे इस चैनल का **एडमिन** होना ज़रूरी है।",
        parse_mode='Markdown'
    )
    return GET_CHANNEL_ID

async def get_channel_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    channel_id_input = update.message.text.strip()
    user = update.effective_user

    if re.match(r'^-?\d+$', channel_id_input):
        channel_id = int(channel_id_input)
    else:
        channel_id = channel_id_input if channel_id_input.startswith('@') else f"@{channel_id_input}"

    try:
        bot_user = await context.bot.get_me()
        bot_username = bot_user.username or "bot"

        chat_member = await context.bot.get_chat_member(chat_id=channel_id, user_id=bot_user.id)
        chat_info = await context.bot.get_chat(chat_id=channel_id)

        if getattr(chat_member, "status", "").lower() in ['administrator', 'creator']:
            raw_id_str = str(chat_info.id)
            link_channel_id = raw_id_str[4:] if raw_id_str.startswith('-100') else raw_id_str.replace('-', '')

            deep_link_payload = f"link_{link_channel_id}"
            share_url = f"https://t.me/{bot_username}?start={deep_link_payload}"
            channel_title = chat_info.title

            await update.message.reply_text(
                f"✅ चैनल **{channel_title}** कनेक्टेड!\n\n"
                f"शेयर करने की लिंक:\n```\n{share_url}\n```\n",
                parse_mode='Markdown'
            )

            share_keyboard = [[InlineKeyboardButton("🔗 अपनी लिंक शेयर करें", url=share_url)]]
            share_markup = InlineKeyboardMarkup(share_keyboard)
            await update.message.reply_text("शेयर करने के लिए बटन दबाएँ:", reply_markup=share_markup)

            if LOG_CHANNEL_USERNAME:
                log_message = (
                    f"**🔗 नया चैनल लिंक बना!**\n"
                    f"यूजर: [{user.first_name}](tg://user?id={user.id})\n"
                    f"चैनल: `{channel_title}`\n"
                    f"शेयर लिंक: {share_url}"
                )
                await context.bot.send_message(chat_id=LOG_CHANNEL_USERNAME, text=log_message, parse_mode='Markdown')

            return ConversationHandler.END
        else:
            await update.message.reply_text(
                "❌ मैं आपके चैनल का **एडमिन नहीं** हूँ। कृपया मुझे एडमिन बनाकर फिर से कोशिश करें।"
            )
            return GET_CHANNEL_ID

    except Exception as e:
        logging.error(f"Error in get_channel_id for input {channel_id_input}: {e}")
        await update.message.reply_text(
            "⚠️ चैनल तक पहुँचने में त्रुटि। सुनिश्चित करें कि चैनल का @username/ID सही है और बॉट एडमिन है।"
        )
        return GET_CHANNEL_ID

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text('कन्वर्सेशन रद्द कर दिया गया है।')
    return ConversationHandler.END

# ------------------------------------------------
# Vote handler: pattern vote_<channel_id>_<message_id>
# Enforces:
#  - user must be member/admin/creator of the channel/group
#  - one vote per user per post
#  - thread-safe increment using asyncio.Lock per post
# ------------------------------------------------
async def handle_vote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()  # immediate ack to avoid "loading..." long

    data = query.data or ""
    m = re.match(r'^vote_(-?\d+)_(\d+)$', data)
    if not m:
        return await query.answer(text="❌ त्रुटि: वोट ID सही नहीं है।", show_alert=True)

    channel_id_numeric = int(m.group(1))
    message_id = int(m.group(2))
    user_id = query.from_user.id

    # Prevent bots from voting
    if query.from_user.is_bot:
        return await query.answer(text="🤖 बॉट वोट नहीं कर सकते।", show_alert=True)

    post_key = make_post_key(channel_id_numeric, message_id)
    lock = LOCKS[post_key]

    # Check membership first
    try:
        chat_member = await context.bot.get_chat_member(chat_id=channel_id_numeric, user_id=user_id)
        is_subscriber = chat_member.status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]
    except (Forbidden, BadRequest) as e:
        logging.error(f"Bot failed to check subscriber status for {channel_id_numeric}: {e}")
        # When bot cannot check, inform user to ensure bot has permissions.
        return await query.answer(
            text="🚨 वोटिंग त्रुटि: बॉट सदस्यता जाँचने में असमर्थ है। कृपया सुनिश्चित करें कि बॉट को उपयुक्त अनुमतियाँ दी गयी हैं।",
            show_alert=True
        )
    except Exception as e:
        logging.exception("Unknown error during membership check")
        return await query.answer(text="⚠️ नेटवर्क त्रुटि या API विफलता हुई। कृपया दोबारा प्रयास करें।", show_alert=True)

    if not is_subscriber:
        # If we can build channel url to send user, try to fetch username
        try:
            chat_info = await context.bot.get_chat(chat_id=channel_id_numeric)
            channel_url = chat_info.invite_link or (f"https://t.me/{chat_info.username}" if chat_info.username else None)
        except Exception:
            channel_url = None

        # Provide join link if available
        alert_text = "❌ आप वोट नहीं कर सकते। कृपया पहले चैनल/ग्रुप को सब्सक्राइब/जॉइन करें।"
        return await query.answer(text=alert_text, show_alert=True)

    # Acquire lock and record vote (1 vote per user per post)
    async with lock:
        # If already voted on this post
        if user_id in VOTES_PER_POST[post_key]:
            return await query.answer(text="🗳️ आप पहले ही इस पोस्ट पर वोट दे चुके हैं।", show_alert=True)

        # Record vote
        VOTES_PER_POST[post_key].add(user_id)
        current_vote_count = len(VOTES_PER_POST[post_key])

        # Update button label to show new count
        try:
            # Build updated markup
            chat_info = await context.bot.get_chat(chat_id=channel_id_numeric)
            channel_url = chat_info.invite_link or (f"https://t.me/{chat_info.username}" if chat_info.username else None)
            new_markup = build_vote_keyboard(channel_id_numeric, message_id, channel_url)

            # Edit the post's reply_markup (the message that contains the vote button)
            await query.edit_message_reply_markup(reply_markup=new_markup)
        except BadRequest as e:
            # often message not modified or button can't be edited if message removed — safe to ignore
            logging.info(f"Button edit may have failed: {e}")
        except Exception as e:
            logging.warning(f"Unexpected error while updating button: {e}")

    # Final user feedback
    await query.answer(text=f"✅ आपका वोट दर्ज हो गया! (कुल {current_vote_count})", show_alert=True)

# ------------------------------------------------
# Utility command: /poststats <channel_id> <message_id>
# Admin convenience to check vote count for a post
# ------------------------------------------------
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
    await update.message.reply_text(f"Post `{message_id}` in `{channel_id}` has *{count}* votes.", parse_mode='Markdown')

# ------------------------------------------------
# Application setup
# ------------------------------------------------
def main():
    if not BOT_TOKEN:
        logging.error("BOT_TOKEN environment variable सेट नहीं है।")
        return

    application = ApplicationBuilder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("poll", create_poll))
    application.add_handler(CallbackQueryHandler(start_channel_poll_conversation_cb, pattern='^start_channel_conv$'))  # open conv
    application.add_handler(CallbackQueryHandler(handle_vote, pattern=r'^vote_(-?\d+)_(\d+)$'))
    application.add_handler(CommandHandler("poststats", post_stats_cmd))

    link_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_channel_poll_conversation_cb, pattern='^start_channel_conv$')],
        states={GET_CHANNEL_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_channel_id)]},
        fallbacks=[CommandHandler('cancel', cancel)],
        allow_reentry=False
    )
    application.add_handler(link_conv_handler)

    logging.info("👑 Stylish Voting Bot Starting... 🚀")
    application.run_polling(poll_interval=2)

if __name__ == '__main__':
    main()
