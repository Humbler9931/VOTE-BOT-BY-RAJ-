# filename: bot.py
import os
import re
import logging
import asyncio
from dotenv import load_dotenv
from collections import defaultdict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest, Forbidden, Conflict

from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    CallbackQueryHandler
)

# -------------------------
# Config & Env
# -------------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
IMAGE_URL = os.getenv("IMAGE_URL", "https://picsum.photos/600/300")
LOG_CHANNEL_USERNAME = os.getenv("LOG_CHANNEL_USERNAME", "")  # optional

if not BOT_TOKEN:
    raise SystemExit("BOT_TOKEN environment variable is required in .env")

# Conversation states
(GET_CHANNEL_ID,) = range(1)

# In-memory vote storage (per-post)
VOTES_PER_POST = defaultdict(set)  # key: (channel_id, message_id) -> set(user_id)
LOCKS = defaultdict(lambda: asyncio.Lock())

# Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# -------------------------
# Helpers
# -------------------------
def make_post_key(channel_id: int, message_id: int):
    return (int(channel_id), int(message_id))

def vote_button_text_for(channel_id: int, message_id: int) -> str:
    key = make_post_key(channel_id, message_id)
    count = len(VOTES_PER_POST.get(key, set()))
    return f"✅ Vote Now ({count} Votes)"

def build_vote_keyboard(channel_id: int, message_id: int, channel_url: str | None = None):
    callback_data = f"vote_{channel_id}_{message_id}"
    keyboard = [[InlineKeyboardButton(vote_button_text_for(channel_id, message_id), callback_data=callback_data)]]
    if channel_url:
        keyboard.append([InlineKeyboardButton("➡️ Go to Channel", url=channel_url)])
    return InlineKeyboardMarkup(keyboard)

def parse_poll_from_text(text: str):
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

# -------------------------
# Send start message helper
# -------------------------
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
        logging.error(f"Image send failed: {e}. Sending text fallback.")
        try:
            await context.bot.send_message(
                chat_id=target_chat_id,
                text=welcome_message,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
        except Exception:
            logging.exception("Failed to send fallback message")

# -------------------------
# Handlers
# -------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    bot_user = await context.bot.get_me()
    bot_username = bot_user.username or "bot"

    # deep link handling
    if context.args:
        payload = context.args[0]
        match = re.match(r'link_(\d+)', payload)
        if match:
            channel_id_str = match.groups()[0]
            target_channel_id_numeric = int(f"-100{channel_id_str}")
            try:
                chat_info = await context.bot.get_chat(chat_id=target_channel_id_numeric)
                channel_title = chat_info.title
                channel_url = chat_info.invite_link or (f"https://t.me/{chat_info.username}" if chat_info.username else None)

                await update.message.reply_text(
                    f"✨ **Connected to {channel_title}**. If a vote post exists you can vote now.",
                    parse_mode='Markdown'
                )

                notification_message = (
                    f"**👑 New Participant Joined!**\n\n"
                    f"User: [{user.first_name}](tg://user?id={user.id}) (`{user.id}`)\n\n"
                    "Click below to let them vote (only joined members can vote)."
                )

                sent = await context.bot.send_photo(
                    chat_id=target_channel_id_numeric,
                    photo=IMAGE_URL,
                    caption=notification_message,
                    parse_mode='Markdown'
                )

                # attach vote button that includes this message_id
                channel_markup = build_vote_keyboard(target_channel_id_numeric, sent.message_id, channel_url)
                await context.bot.edit_message_reply_markup(chat_id=target_channel_id_numeric, message_id=sent.message_id, reply_markup=channel_markup)
                return
            except Exception as e:
                logging.exception("Deep link notify failed")
                await update.message.reply_text("Error connecting to channel. Make sure bot is admin.")
    # regular start
    keyboard = [
        [InlineKeyboardButton("🔗 अपनी लिंक बनाएँ", callback_data='start_channel_conv'),
         InlineKeyboardButton("➕ ग्रुप में जोड़ें", url=f"https://t.me/{bot_username}?startgroup=true")],
        [InlineKeyboardButton("📊 मेरे वोट्स", callback_data='my_polls_list'),
         InlineKeyboardButton("❓ गाइड", url='https://t.me/teamrajweb')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    welcome_message = (
        "**👑 वोट बॉट में आपका स्वागत है!**\n\n"
        "चैनल जोड़ें और यूनिक लिंक जनरेट करें।"
    )
    await send_start_message(update, context, reply_markup, welcome_message)

async def create_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parsed = parse_poll_from_text(" ".join(context.args))
    if not parsed:
        await update.message.reply_text(
            "Use: `/poll [question]? [opt1], [opt2], ...` (2-10 options)",
            parse_mode='Markdown'
        )
        return
    q, options = parsed
    try:
        await context.bot.send_poll(chat_id=update.effective_chat.id, question=q, options=options, is_anonymous=False)
        await update.message.reply_text("✅ Poll created")
    except Exception:
        logging.exception("Failed to send poll")
        await update.message.reply_text("Failed to create poll.")

async def start_channel_poll_conversation_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await context.bot.send_message(chat_id=update.effective_chat.id, text="Send channel @username or numeric ID (-100...) where I am admin.")
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
        chat_member = await context.bot.get_chat_member(chat_id=channel_id, user_id=bot_user.id)
        chat_info = await context.bot.get_chat(chat_id=channel_id)

        if getattr(chat_member, "status", "").lower() in ['administrator', 'creator']:
            raw_id_str = str(chat_info.id)
            link_channel_id = raw_id_str[4:] if raw_id_str.startswith('-100') else raw_id_str.replace('-', '')
            deep_link_payload = f"link_{link_channel_id}"
            share_url = f"https://t.me/{bot_user.username}?start={deep_link_payload}"
            await update.message.reply_text(f"✅ Connected! Share link:\n{share_url}")
            if LOG_CHANNEL_USERNAME:
                await context.bot.send_message(chat_id=LOG_CHANNEL_USERNAME, text=f"New link: {share_url}")
            return ConversationHandler.END
        else:
            await update.message.reply_text("I am not admin in that channel. Make me admin and retry.")
            return GET_CHANNEL_ID
    except Exception:
        logging.exception("get_channel_id failed")
        await update.message.reply_text("Error accessing channel. Ensure channel is valid and bot is admin.")
        return GET_CHANNEL_ID

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END

async def handle_vote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()  # quick ack

    data = query.data or ""
    m = re.match(r'^vote_(-?\d+)_(\d+)$', data)
    if not m:
        return await query.answer(text="Invalid vote id.", show_alert=True)

    channel_id_numeric = int(m.group(1))
    message_id = int(m.group(2))
    user_id = query.from_user.id

    if query.from_user.is_bot:
        return await query.answer(text="Bots cannot vote.", show_alert=True)

    # membership check
    try:
        chat_member = await context.bot.get_chat_member(chat_id=channel_id_numeric, user_id=user_id)
        is_subscriber = chat_member.status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]
    except (Forbidden, BadRequest):
        return await query.answer(text="Cannot verify membership. Ensure bot has permissions.", show_alert=True)
    except Exception:
        logging.exception("Membership check failed")
        return await query.answer(text="API error. Try again later.", show_alert=True)

    if not is_subscriber:
        return await query.answer(text="Please join the channel/group to vote.", show_alert=True)

    post_key = make_post_key(channel_id_numeric, message_id)
    lock = LOCKS[post_key]

    async with lock:
        if user_id in VOTES_PER_POST[post_key]:
            return await query.answer(text="You already voted on this post.", show_alert=True)
        VOTES_PER_POST[post_key].add(user_id)
        current_vote_count = len(VOTES_PER_POST[post_key])

        # update button text
        try:
            chat_info = await context.bot.get_chat(chat_id=channel_id_numeric)
            channel_url = chat_info.invite_link or (f"https://t.me/{chat_info.username}" if chat_info.username else None)
            new_markup = build_vote_keyboard(channel_id_numeric, message_id, channel_url)
            await query.edit_message_reply_markup(reply_markup=new_markup)
        except BadRequest as e:
            logging.info(f"Edit markup skipped: {e}")
        except Exception:
            logging.exception("Failed to update markup")

    await query.answer(text=f"✅ Vote recorded (total {current_vote_count})", show_alert=True)

async def post_stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /poststats <channel_id> <message_id>")
        return
    try:
        channel_id = int(args[0]); message_id = int(args[1])
    except ValueError:
        await update.message.reply_text("channel_id and message_id must be integers.")
        return
    key = make_post_key(channel_id, message_id)
    await update.message.reply_text(f"Post has {len(VOTES_PER_POST.get(key, set()))} votes.")

# -------------------------
# Main: safe startup with delete_webhook()
# -------------------------
def main():
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # add handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("poll", create_poll))
    application.add_handler(CallbackQueryHandler(start_channel_poll_conversation_cb, pattern='^start_channel_conv$'))
    application.add_handler(CallbackQueryHandler(handle_vote, pattern=r'^vote_(-?\d+)_(\d+)$'))
    application.add_handler(CommandHandler("poststats", post_stats_cmd))

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_channel_poll_conversation_cb, pattern='^start_channel_conv$')],
        states={GET_CHANNEL_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_channel_id)]},
        fallbacks=[CommandHandler('cancel', cancel)],
        allow_reentry=False
    )
    application.add_handler(conv)

    # before starting polling: delete any existing webhook (prevents conflict)
    try:
        # run coroutine in loop to delete webhook
        loop = asyncio.get_event_loop()
        loop.run_until_complete(application.bot.delete_webhook(drop_pending_updates=True))
        logging.info("delete_webhook called successfully (if webhook existed).")
    except Conflict as c:
        logging.error("Conflict error while deleting webhook: %s", c)
    except Exception:
        logging.exception("Failed to delete webhook — continuing to polling may still fail if another updater is running.")

    # now start polling (blocking)
    try:
        application.run_polling(poll_interval=2)
    except Exception:
        logging.exception("Application run_polling failed — check logs and ensure only one instance of the bot runs.")

if __name__ == "__main__":
    main()
