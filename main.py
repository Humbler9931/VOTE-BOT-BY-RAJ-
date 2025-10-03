import os
import re
import logging
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    CallbackQueryHandler
)

# .env फ़ाइल से environment variables लोड करें
load_dotenv()

# लॉगिंग सेट करें
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# एनवायर्नमेंट वेरिएबल्स को सुरक्षित रूप से लें
BOT_TOKEN = os.getenv("BOT_TOKEN")
IMAGE_URL = os.getenv("IMAGE_URL", "https://picsum.photos/600/300")
LOG_CHANNEL_USERNAME = os.getenv("LOG_CHANNEL_USERNAME", "@teamrajweb")

# कन्वर्सेशन स्टेट्स
(GET_CHANNEL_ID, CREATE_CHANNEL_POLL) = range(2)


# -------------------------
# Utility / Parsing Helpers
# -------------------------
def parse_poll_from_args(args: list) -> tuple | None:
    """
    पुराने तरीके से जब context.args दिए हों (list of tokens),
    तब parse करता है। यह तभी तब useful है जब handler CommandHandler से आता हो।
    """
    if not args:
        return None
    full_text = " ".join(args)
    return parse_poll_from_text(full_text)


def parse_poll_from_text(text: str) -> tuple | None:
    """
    किसी raw text में से poll parse करें। format अपेक्षित है:
       <question>? <option1>, <option2>, ...
    '?' होना अनिवार्य माना गया है (आप चाहें तो इसे optional कर सकते हैं)।
    """
    if not text or '?' not in text:
        return None
    try:
        question_part, options_part = text.split('?', 1)
        question = question_part.strip()
        # अगर options_part में leading /poll रहे तो उसे हटाएँ (safety)
        options_part = options_part.strip()
        # options comma से split
        options = [opt.strip() for opt in options_part.split(',') if opt.strip()]
        if not question or len(options) < 2 or len(options) > 10:
            return None
        return question, options
    except Exception as e:
        logging.exception("parse_poll_from_text failed")
        return None


# -------------------------
# Core Bot Functions
# -------------------------
async def send_start_message(update: Update, context: ContextTypes.DEFAULT_TYPE, reply_markup: InlineKeyboardMarkup, welcome_message: str):
    """
    इमेज या टेक्स्ट के साथ स्टार्ट मैसेज भेजता है। safe fallback जब update.message None हो।
    """
    try:
        await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=IMAGE_URL,
            caption=welcome_message,
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
    except Exception as e:
        logging.error(f"Image send failed: {e}. Sending text message instead.")
        # safe fallback: अगर update.message मौजूद है तो वही use करें, नहीं तो context.bot.send_message
        try:
            if getattr(update, "message", None):
                await update.message.reply_text(
                    welcome_message,
                    parse_mode='Markdown',
                    reply_markup=reply_markup
                )
            else:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=welcome_message,
                    parse_mode='Markdown',
                    reply_markup=reply_markup
                )
        except Exception:
            logging.exception("Failed to send fallback welcome message")


# 1. /start कमांड
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📝 चैनल के लिए वोट बनाएँ", callback_data='start_channel_poll_conv')],
        [InlineKeyboardButton("📊 मेरे बनाए वोट्स", callback_data='my_polls_list'),
         InlineKeyboardButton("❓ गाइड/मदद", url='https://t.me/teamrajweb')],
        [InlineKeyboardButton("🔗 सोर्स कोड", url='https://t.me/teamrajweb'),
         InlineKeyboardButton("📢 चैनल जॉइन करें", url='https://t.me/narzoxbot')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    welcome_message = (
        "**👑 वोट बॉट में आपका स्वागत है! 👑**\n\n"
        "मैं किसी भी ग्रुप या चैनल के लिए **सुंदर और सुरक्षित** वोट बनाने में माहिर हूँ। "
        "चैनल के लिए वोट बनाने हेतु *'📝 चैनल के लिए वोट बनाएँ'* पर क्लिक करें।\n\n"
        "__**Stylish Quote:**__\n"
        "*\"आपके विचार मायने रखते हैं। वोट दें, बदलाव लाएँ।\"*\n"
        "~ The Voting Bot"
    )

    await send_start_message(update, context, reply_markup, welcome_message)


# 2. साधारण /poll कमांड (chat में)
async def create_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # context.args से parse करने की कोशिश
    parsed = parse_poll_from_args(context.args)
    if not parsed:
        # fallback: पूरा message text से parse करने की कोशिश
        text = ""
        if update.message and update.message.text:
            # remove the command token (/poll or /poll@BotName)
            text = re.sub(r'^/poll(@\w+)?\s*', '', update.message.text, count=1)
        parsed = parse_poll_from_text(text)

    if not parsed:
        await update.message.reply_text(
            "कृपया सही फॉर्मेट का उपयोग करें:\n"
            "`/poll [सवाल]? [ऑप्शन1], [ऑप्शन2], ...`\n(कम से कम 2 और अधिकतम 10 विकल्प)",
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


# 3. Callback से कन्वर्सेशन शुरू करना
async def start_channel_poll_conversation_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="👋 **चैनल सेटअप:**\n"
             "कृपया उस **चैनल का @username या ID** भेजें जिसके लिए आप वोट बनाना चाहते हैं।\n\n"
             "*(उदाहरण: `@my_channel_name` या `-100123456789`)*\n\n"
             "**नोट:** मुझे इस चैनल का **एडमिन** होना ज़रूरी है।",
        parse_mode='Markdown'
    )
    return GET_CHANNEL_ID


# 4. चैनल ID प्राप्त करें और बॉट एडमिन चेक करें
async def get_channel_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    channel_id_input = update.message.text.strip()
    # detect numeric id pattern (like -100123456789 या सिर्फ digits)
    numeric_match = re.match(r'^-?\d+$', channel_id_input)

    if numeric_match:
        # numeric chat id (int)
        try:
            channel_id = int(channel_id_input)
        except ValueError:
            channel_id = channel_id_input  # fallback but unlikely
    else:
        # treat as username; ensure startswith '@'
        channel_id = channel_id_input if channel_id_input.startswith('@') else f"@{channel_id_input}"

    context.user_data['temp_channel_id'] = channel_id

    try:
        bot_user = await context.bot.get_me()
        bot_id = bot_user.id

        chat_member = await context.bot.get_chat_member(chat_id=channel_id, user_id=bot_id)
        # chat_member.status could be 'administrator' or 'creator'
        if getattr(chat_member, "status", "").lower() in ['administrator', 'creator']:
            await update.message.reply_text(
                "✅ बॉट सफलतापूर्वक चैनल **एडमिन** है।\n"
                "अब आप अपना वोट बना सकते हैं। कृपया **`/poll [सवाल]? [ऑप्शन1], [ऑप्शन2], ...`** फॉर्मेट में भेजें।\n"
                "*(या /cancel दबाकर रद्द करें)*",
                parse_mode='Markdown'
            )
            return CREATE_CHANNEL_POLL
        else:
            await update.message.reply_text(
                "❌ मैं आपके चैनल का **एडमिन नहीं** हूँ।\n"
                "कृपया मुझे एडमिन (कम से कम **'Post Messages'** की अनुमति के साथ) बनाएँ और फिर से चैनल का @username भेजें।"
            )
            return GET_CHANNEL_ID

    except Exception as e:
        logging.exception("Error checking admin status")
        await update.message.reply_text(
            "⚠️ **चैनल तक पहुँचने में त्रुटि** हुई। सुनिश्चित करें कि:\n"
            "1. चैनल का @username/ID सही है।\n"
            "2. चैनल **पब्लिक** है या आपने मुझे चैनल में **एडमिन** के रूप में जोड़ा है।"
        )
        return GET_CHANNEL_ID


# 5. चैनल के लिए /poll (Conversation में)
async def create_channel_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    channel_id = context.user_data.get('temp_channel_id')
    user = update.effective_user

    # message text से parse करें (command में से /poll हटाकर)
    text = ""
    if update.message and update.message.text:
        text = re.sub(r'^/poll(@\w+)?\s*', '', update.message.text, count=1)

    parsed = parse_poll_from_text(text)
    if not parsed:
        await update.message.reply_text("वोट का फॉर्मेट गलत है। कृपया फिर से प्रयास करें।")
        return CREATE_CHANNEL_POLL

    question, options = parsed

    try:
        poll_message = await context.bot.send_poll(
            chat_id=channel_id,
            question=question,
            options=options,
            is_anonymous=False,
            allows_multiple_answers=False,
        )

        # prepare deep link using bot username (safer to fetch)
        bot_user = await context.bot.get_me()
        bot_username = bot_user.username or context.bot.username or "bot"

        deep_link_payload = f"poll_{poll_message.message_id}_{str(channel_id).replace('@','')}"
        welcome_keyboard = [[
            InlineKeyboardButton(
                f"👋 {user.first_name} से जुड़ें!",
                url=f"https://t.me/{bot_username}?start={deep_link_payload}"
            )
        ]]
        welcome_markup = InlineKeyboardMarkup(welcome_keyboard)

        channel_welcome_message = (
            f"**📊 नया चैनल वोट बना!**\n\n"
            f"यह वोट यूजर द्वारा बनाया गया है:\n"
            f"👤 **नाम:** [{user.first_name}](tg://user?id={user.id})\n"
            f"🆔 **ID:** `{user.id}`\n"
            f"🌐 **Username:** {f'@{user.username}' if user.username else 'N/A'}\n"
            f"🔗 **चैनल:** `{channel_id}`\n\n"
            f"इस यूजर से जुड़ने के लिए नीचे दिए बटन पर क्लिक करें।\n"
        )

        if LOG_CHANNEL_USERNAME:
            try:
                await context.bot.send_photo(
                    chat_id=LOG_CHANNEL_USERNAME,
                    photo=IMAGE_URL,
                    caption=channel_welcome_message,
                    parse_mode='Markdown',
                    reply_markup=welcome_markup
                )
            except Exception:
                logging.exception("Failed to send log message to LOG_CHANNEL_USERNAME")

        # cleanup temp data
        context.user_data.pop('temp_channel_id', None)

        await update.message.reply_text(
            f"✅ आपका वोट **{channel_id}** चैनल में सफलतापूर्वक भेज दिया गया है!\n"
            f"लॉग मैसेज **{LOG_CHANNEL_USERNAME}** में भेज दिया गया है।",
            parse_mode='Markdown'
        )
        return ConversationHandler.END

    except Exception as e:
        logging.exception("Failed to send poll to target channel")
        await update.message.reply_text(f"वोट भेजने में त्रुटि हुई: {e}")
        return CREATE_CHANNEL_POLL


# 6. cancel handler
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text('कन्वर्सेशन रद्द कर दिया गया है।')
    # cleanup
    context.user_data.pop('temp_channel_id', None)
    return ConversationHandler.END


# -------------------------
# main()
# -------------------------
def main():
    if not BOT_TOKEN:
        logging.error("BOT_TOKEN environment variable सेट नहीं है।")
        return

    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # /start
    application.add_handler(CommandHandler("start", start))

    # simple /poll for chats
    application.add_handler(CommandHandler("poll", create_poll))

    # conversation for channel polls
    poll_conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(start_channel_poll_conversation_cb, pattern='^start_channel_poll_conv$'),
        ],
        states={
            GET_CHANNEL_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_channel_id)],
            # यहाँ CommandHandler उपयोग करें ताकि '/poll' कमांड से ही आगे बढ़े
            CREATE_CHANNEL_POLL: [CommandHandler('poll', create_channel_poll)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
        allow_reentry=False
    )

    application.add_handler(poll_conv_handler)

    logging.info("बॉट शुरू हो रहा है...")
    application.run_polling(poll_interval=3)


if __name__ == '__main__':
    main()
