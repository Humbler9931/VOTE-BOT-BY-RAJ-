import os
import re
import logging
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
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
(GET_CHANNEL_ID,) = range(1)


# -------------------------
# Utility / Parsing Helpers (Poll functions kept for /poll command)
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
    """इमेज या टेक्स्ट के साथ स्टार्ट मैसेज भेजता है।"""
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
        payload = context.args[0]
        match = re.match(r'link_(\d+)', payload)

        if match:
            channel_id_str = match.groups()[0]
            # ID को वापस full numeric format (-100...) में बदलें
            target_channel_id_numeric = int(f"-100{channel_id_str}") 

            try:
                # चैनल का नाम प्राप्त करें (Display के लिए)
                chat_info = await context.bot.get_chat(chat_id=target_channel_id_numeric)
                channel_title = chat_info.title
                
                # 1. User को कन्फर्मेशन मैसेज भेजें (You are participate)
                await update.message.reply_text(
                    f"🎉 **आप सफलतापूर्वक शामिल हो गए हैं!**\n\n"
                    f"आप चैनल **`{channel_title}`** से कनेक्ट हो गए हैं। आपकी एक्टिविटी की सूचना चैनल में भेज दी गई है।"
                )

                # 2. Notification message चैनल में भेजें
                notification_message = (
                    f"**🤝 नया यूजर कनेक्ट हुआ!**\n\n"
                    f"👤 **नाम:** [{user.first_name}](tg://user?id={user.id})\n"
                    f"🆔 **ID:** `{user.id}`\n"
                    f"🌐 **Username:** {f'@{user.username}' if user.username else 'N/A'}\n\n"
                    f"🤖 **बॉट Username:** @{bot_username}\n\n"
                    f"यह यूजर **`{channel_title}`** से जुड़ा है।"
                )

                # 'Connect with User' बटन
                connect_keyboard = [[
                    InlineKeyboardButton("👋 Connect with User", url=f"tg://user?id={user.id}")
                ]]
                connect_markup = InlineKeyboardMarkup(connect_keyboard)

                await context.bot.send_photo(
                    chat_id=target_channel_id_numeric,
                    photo=IMAGE_URL,
                    caption=notification_message,
                    parse_mode='Markdown',
                    reply_markup=connect_markup
                )
                
                return

            except Exception as e:
                logging.error(f"Deep link notification failed: {e}")
                await update.message.reply_text("माफ़ करना, चैनल से जुड़ने में त्रुटि हुई।")
                # Fallback to main start menu
    
    # --- REGULAR START MENU (Stylish Buttons) ---
    keyboard = [
        [
            InlineKeyboardButton("🔗 लिंक पाएँ", callback_data='start_channel_conv'),
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
        "चैनल को कनेक्ट कर **तुरंत शेयर लिंक** पाने हेतु *'🔗 लिंक पाएँ'* पर क्लिक करें।\n\n"
        "__**Stylish Quote:**__\n"
        "*\"आपके विचार मायने रखते हैं। वोट दें, बदलाव लाएँ।\"*\n"
        "~ The Voting Bot"
    )

    await send_start_message(update, context, reply_markup, welcome_message)


# 2. साधारण /poll कमांड (chat में)
async def create_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # This remains the same for creating simple polls outside the conversation flow.
    parsed = parse_poll_from_args(context.args)
    if not parsed:
        text = update.message.text if update.message else ""
        text = re.sub(r'^/poll(@\w+)?\s*', '', text, count=1)
        parsed = parse_poll_from_text(text)

    if not parsed:
        await update.message.reply_text(
            "कृपया सही फॉर्मेट का उपयोग करें:\n"
            "`/poll [सवाल]? [ऑप्शन1], [ऑप्शन2], ...`",
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
        text="👋 **चैनल लिंक सेटअप:**\n"
             "कृपया उस **चैनल का @username या ID** भेजें जिसके लिए आप लिंक जनरेट करना चाहते हैं।\n\n"
             "**नोट:** मुझे इस चैनल का **एडमिन** होना ज़रूरी है।",
        parse_mode='Markdown'
    )
    return GET_CHANNEL_ID


# 4. चैनल ID प्राप्त करें, बॉट एडमिन चेक करें और INSTANT LINK भेजें
async def get_channel_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    channel_id_input = update.message.text.strip()
    user = update.effective_user

    # ID detection and normalization logic
    numeric_match = re.match(r'^-?\d+$', channel_id_input)
    if numeric_match:
        channel_id = int(channel_id_input) # Numeric ID (-100...)
    else:
        channel_id = channel_id_input if channel_id_input.startswith('@') else f"@{channel_id_input}"

    try:
        bot_user = await context.bot.get_me()
        bot_id = bot_user.id
        bot_username = bot_user.username or "bot"

        # 1. बॉट एडमिन चेक करें
        chat_member = await context.bot.get_chat_member(chat_id=channel_id, user_id=bot_id)
        
        if getattr(chat_member, "status", "").lower() in ['administrator', 'creator']:
            
            # 2. सफलता: INSTANT LINK बनाएं और भेजें
            
            # Channel ID को Deep Link में उपयोग के लिए साफ़ करें (बिना @ और -100 prefix के)
            link_channel_id = str(channel_id).replace('@', '')
            if link_channel_id.startswith('-100'):
                link_channel_id = link_channel_id[4:] # -100 हटाएँ

            # Payload: link_<channel_id_clean>
            deep_link_payload = f"link_{link_channel_id}"
            
            # शेयर करने योग्य लिंक
            share_url = f"https://t.me/{bot_username}?start={deep_link_payload}"
            
            # चैनल का नाम प्राप्त करें (Display के लिए)
            chat_info = await context.bot.get_chat(chat_id=channel_id)
            channel_title = chat_info.title
            
            # 3. यूज़र को लिंक दिखाएँ (कॉपी करने योग्य)
            await update.message.reply_text(
                f"✅ चैनल **{channel_title}** सफलतापूर्वक कनेक्ट हो गया है!\n\n"
                f"**आपकी शेयर करने योग्य लिंक तैयार है। इसे कॉपी करें:**\n"
                f"```\n{share_url}\n```\n\n"
                f"**या इस बटन का उपयोग करें:**",
                parse_mode='Markdown'
            )
            
            # 4. बटन भेजें (लिंक को आसान बनाने के लिए)
            share_keyboard = [[
                InlineKeyboardButton("🔗 अपनी लिंक शेयर करें", url=share_url),
            ]]
            share_markup = InlineKeyboardMarkup(share_keyboard)
            
            await update.message.reply_text(
                "शेयर करने के लिए बटन दबाएँ:",
                reply_markup=share_markup
            )
            
            # 5. LOG_CHANNEL_USERNAME में सूचना भेजें (Optional)
            if LOG_CHANNEL_USERNAME:
                log_message = (
                    f"**🔗 नया चैनल लिंक बना!**\n"
                    f"यूजर: [{user.first_name}](tg://user?id={user.id})\n"
                    f"चैनल: `{channel_title}`\n"
                    f"शेयर लिंक: {share_url}"
                )
                await context.bot.send_message(
                    chat_id=LOG_CHANNEL_USERNAME,
                    text=log_message,
                    parse_mode='Markdown'
                )

            return ConversationHandler.END # कन्वर्सेशन समाप्त

        else:
            # 3. असफलता: बॉट एडमिन नहीं है
            await update.message.reply_text(
                "❌ मैं आपके चैनल का **एडमिन नहीं** हूँ।\n"
                "कृपया मुझे एडमिन (कम से कम **'Post Messages'** की अनुमति के साथ) बनाएँ और फिर से चैनल का @username/ID भेजें।"
            )
            return GET_CHANNEL_ID # इसी स्टेट में रहें

    except Exception:
        logging.exception("Error checking admin status or getting chat info")
        await update.message.reply_text(
            "⚠️ **चैनल तक पहुँचने में त्रुटि** हुई। सुनिश्चित करें कि:\n"
            "1. चैनल का @username/ID सही है।\n"
            "2. चैनल **पब्लिक** है या आपने मुझे चैनल में **एडमिन** के रूप में जोड़ा है।"
        )
        return GET_CHANNEL_ID


# 5. cancel handler
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text('कन्वर्सेशन रद्द कर दिया गया है।')
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

    # 3. conversation for instant link
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

    application.add_handler(link_conv_handler)

    logging.info("बॉट शुरू हो रहा है...")
    application.run_polling(poll_interval=3)


if __name__ == '__main__':
    main()
