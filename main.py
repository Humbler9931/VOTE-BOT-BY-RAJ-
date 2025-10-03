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
    """/poll कमांड से सवाल और विकल्पों को पार्स करता है।"""
    if not args:
        return None
    full_text = " ".join(args)
    return parse_poll_from_text(full_text)


def parse_poll_from_text(text: str) -> tuple | None:
    """किसी raw text में से poll parse करें।"""
    if not text or '?' not in text:
        return None
    try:
        question_part, options_part = text.split('?', 1)
        question = question_part.strip()
        options_part = options_part.strip()
        options = [opt.strip() for opt in options_part.split(',') if opt.strip()]
        if not question or len(options) < 2 or len(options) > 10:
            return None
        return question, options
    except Exception:
        logging.exception("parse_poll_from_text failed")
        return None


# -------------------------
# Core Bot Functions
# -------------------------
async def send_start_message(update: Update, context: ContextTypes.DEFAULT_TYPE, reply_markup: InlineKeyboardMarkup, welcome_message: str, chat_id=None):
    """
    इमेज या टेक्स्ट के साथ स्टार्ट मैसेज भेजता है।
    chat_id parameter जोड़ा गया ताकि Deep Link से भी message भेजा जा सके।
    """
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


# 1. /start कमांड (Deep Link Handling के साथ)
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    bot_user = await context.bot.get_me()
    bot_username = bot_user.username

    # --- DEEP LINK LOGIC ---
    if context.args:
        # Expected Payload: poll_<message_id>_<channel_id_without_@>
        payload = context.args[0]
        match = re.match(r'poll_(\d+)_(-?\d+)', payload) # Check for ID pattern

        if match:
            message_id, channel_id = match.groups()
            
            # Channel ID को int में बदलने की ज़रूरत नहीं, सीधे string ID (-100...) रखें
            target_channel_id = int(channel_id) if channel_id.startswith('-100') else f"@{channel_id}" 

            notification_message = (
                f"**🎉 नया सब्सक्राइबर जुड़ा!**\n\n"
                f"👤 **नाम:** [{user.first_name}](tg://user?id={user.id})\n"
                f"🆔 **ID:** `{user.id}`\n"
                f"🌐 **Username:** {f'@{user.username}' if user.username else 'N/A'}\n\n"
                f"इस यूजर ने आपके वोट में रुचि दिखाई है।"
            )

            try:
                # 1. Notification message चैनल में भेजें
                vote_keyboard = [[
                    InlineKeyboardButton("🗳️ Go to Vote", url=f"https://t.me/c/{channel_id}/{message_id}")
                ]]
                vote_markup = InlineKeyboardMarkup(vote_keyboard)

                await context.bot.send_photo(
                    chat_id=target_channel_id,
                    photo=IMAGE_URL,
                    caption=notification_message,
                    parse_mode='Markdown',
                    reply_markup=vote_markup
                )
                
                # 2. User को कन्फर्मेशन दें
                await update.message.reply_text(
                    f"✅ आपको वोट में शामिल कर लिया गया है!\n"
                    f"चैनल **`{target_channel_id}`** में आपकी एक्टिविटी की सूचना भेज दी गई है।"
                )
                return

            except Exception as e:
                logging.error(f"Deep link notification failed: {e}")
                await update.message.reply_text("माफ़ करना, वोट तक पहुँचने में त्रुटि हुई।")
                # Fallback to main start menu
    
    # --- REGULAR START MENU ---
    keyboard = [
        [
            InlineKeyboardButton("📝 चैनल के लिए वोट बनाएँ", callback_data='start_channel_poll_conv'),
            InlineKeyboardButton("➕ Add Me to Group", url=f"https://t.me/{bot_username}?startgroup=true") # NEW BUTTON
        ],
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
        text = update.message.text if update.message else ""
        text = re.sub(r'^/poll(@\w+)?\s*', '', text, count=1)
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
    
    # Send a new message instead of editing for better flow
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
    
    # ID detection and normalization logic
    numeric_match = re.match(r'^-?\d+$', channel_id_input)
    if numeric_match:
        channel_id = int(channel_id_input) # Telegram API prefers int for known IDs
    else:
        channel_id = channel_id_input if channel_id_input.startswith('@') else f"@{channel_id_input}"

    context.user_data['temp_channel_id'] = channel_id

    try:
        bot_user = await context.bot.get_me()
        bot_id = bot_user.id

        chat_member = await context.bot.get_chat_member(chat_id=channel_id, user_id=bot_id)
        
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

    except Exception:
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

    # Poll data parse करें
    text = update.message.text if update.message else ""
    text = re.sub(r'^/poll(@\w+)?\s*', '', text, count=1)
    parsed = parse_poll_from_text(text)
    
    if not parsed:
        await update.message.reply_text("वोट का फॉर्मेट गलत है। कृपया फिर से प्रयास करें।")
        return CREATE_CHANNEL_POLL

    question, options = parsed

    try:
        # 1. चैनल में वोट भेजें
        poll_message = await context.bot.send_poll(
            chat_id=channel_id,
            question=question,
            options=options,
            is_anonymous=False,
            allows_multiple_answers=False,
        )

        # 2. Deep Link बनाएं
        bot_user = await context.bot.get_me()
        bot_username = bot_user.username or "bot"
        
        # Channel ID को string में कन्वर्ट करें और '-100' हटा दें (Telegram Deep Link format)
        link_channel_id = str(channel_id).replace('@', '').replace('-100', '')
        
        # Payload: poll_<message_id>_<channel_id_without_@_and_-100>
        deep_link_payload = f"poll_{poll_message.message_id}_{link_channel_id}"
        
        # 3. शेयर करने योग्य लिंक भेजें
        share_keyboard = [[
            InlineKeyboardButton(
                "🔗 वोट लिंक शेयर करें (Start Link)",
                url=f"https://t.me/{bot_username}?start={deep_link_payload}"
            )
        ]]
        share_markup = InlineKeyboardMarkup(share_keyboard)
        
        await update.message.reply_text(
            f"✅ आपका वोट **{channel_id}** चैनल में सफलतापूर्वक भेज दिया गया है!\n\n"
            "**यह शेयर करने योग्य लिंक है। जब कोई इस पर क्लिक करेगा, तो आपके चैनल में एक नोटिफिकेशन जाएगा:**",
            parse_mode='Markdown',
            reply_markup=share_markup
        )
        
        # LOG_CHANNEL_USERNAME में सूचना भेजें (Optional, but kept for logging)
        if LOG_CHANNEL_USERNAME:
            log_message = (
                f"**📊 नया चैनल वोट बना!**\n"
                f"यूजर: [{user.first_name}](tg://user?id={user.id})\n"
                f"चैनल: `{channel_id}`"
            )
            await context.bot.send_message(
                chat_id=LOG_CHANNEL_USERNAME,
                text=log_message,
                parse_mode='Markdown'
            )

        # cleanup temp data
        context.user_data.pop('temp_channel_id', None)
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

    # 1. /start (Deep Link Logic Included)
    application.add_handler(CommandHandler("start", start))

    # 2. simple /poll for chats
    application.add_handler(CommandHandler("poll", create_poll))

    # 3. conversation for channel polls
    poll_conv_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(start_channel_poll_conversation_cb, pattern='^start_channel_poll_conv$'),
        ],
        states={
            GET_CHANNEL_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_channel_id)],
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
