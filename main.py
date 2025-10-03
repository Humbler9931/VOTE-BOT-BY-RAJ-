import os
import logging
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Bot
from telegram.ext import (
    ApplicationBuilder, 
    ContextTypes, 
    CommandHandler, 
    MessageHandler, 
    filters,
    ConversationHandler
)

# .env फ़ाइल से environment variables लोड करें
load_dotenv()

# लॉगिंग सेट करें
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# एनवायर्नमेंट वेरिएबल्स
BOT_TOKEN = os.getenv("BOT_TOKEN")
IMAGE_URL = os.getenv("IMAGE_URL", "https://picsum.photos/600/300") # .env में IMAGE_URL भी जोड़ें
CHANNEL_USERNAME = os.getenv("LOG_CHANNEL_USERNAME", "@teamrajweb") # .env में LOG_CHANNEL_USERNAME जोड़ें (वैकल्पिक)

# कन्वर्सेशन स्टेट्स
(GET_CHANNEL_ID, CHECK_ADMIN, CREATE_POLL) = range(3)

# --- /start कमांड के लिए फ़ंक्शन ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """एक एडवांस स्वागत संदेश और इनलाइन बटन्स भेजता है।"""
    
    # 1. स्टाइलिश इनलाइन बटन्स बनाएँ
    keyboard = [
        [
            InlineKeyboardButton("📝 नया वोट बनाएँ (चैनल के लिए)", callback_data='start_channel_poll'),
            InlineKeyboardButton("❓ गाइड/मदद", url='https://t.me/teamrajweb')
        ],
        [
            InlineKeyboardButton("📊 मेरे बनाए वोट्स", callback_data='my_polls_list'),
            InlineKeyboardButton("🔗 सोर्स कोड", url='https://t.me/teamrajweb')
        ],
        [
            InlineKeyboardButton("📢 चैनल जॉइन करें", url='https://t.me/narzoxbot')
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # 2. एडवांस वेलकम मैसेज
    welcome_message = (
        "**🎉 वोट बॉट में आपका स्वागत है! 🎉**\n\n"
        "मैं ग्रुप्स और चैट्स में आसानी से वोट बनाने में आपकी मदद करता हूँ। "
        "चैनल के लिए वोट बनाने हेतु *'📝 नया वोट बनाएँ'* पर क्लिक करें।\n\n"
        "**_Quote:_**\n"
        "\"सफलता का रहस्य मतदान है: हर आवाज़ मायने रखती है।\"\n"
        "~ Voting System"
    )
    
    # 3. इमेज के साथ मैसेज भेजें
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
        await update.message.reply_text(
            welcome_message,
            parse_mode='Markdown',
            reply_markup=reply_markup
        )

# --- चैनल पोल कन्वर्सेशन शुरू करें ---
async def start_channel_poll_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """चैनल ID/Username पूछकर कन्वर्सेशन शुरू करता है।"""
    query = update.callback_query
    await query.answer()
    
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="कृपया उस **चैनल का @username या ID** भेजें जिसके लिए आप वोट बनाना चाहते हैं।\n\n"
             "*(उदाहरण: @my_channel_name या -100123456789)*"
    )
    return GET_CHANNEL_ID # अगला स्टेट: चैनल ID प्राप्त करें

# --- चैनल ID प्राप्त करें ---
async def get_channel_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """यूजर से चैनल ID प्राप्त करता है और बॉट एडमिन की जाँच करता है।"""
    channel_id = update.message.text.strip()
    user_id = update.effective_user.id
    
    # अस्थायी रूप से डेटा स्टोर करें
    context.user_data['temp_channel_id'] = channel_id
    context.user_data['temp_user_id'] = user_id

    try:
        # बॉट की एडमिन स्थिति की जाँच करें
        chat_member = await context.bot.get_chat_member(chat_id=channel_id, user_id=context.bot.id)
        
        if chat_member.status in ['administrator', 'creator']:
            # अगर बॉट एडमिन है, तो वोट का सवाल पूछें
            await update.message.reply_text(
                "✅ बॉट सफलतापूर्वक चैनल **एडमिन** है।\n"
                "अब आप अपना वोट बना सकते हैं। कृपया **`/poll [सवाल]? [ऑप्शन1], [ऑप्शन2], ...`** फॉर्मेट में भेजें।"
            )
            return CREATE_POLL # अगला स्टेट: वोट बनाएँ
        else:
            # अगर बॉट एडमिन नहीं है
            await update.message.reply_text(
                "❌ मैं आपके चैनल का **एडमिन नहीं** हूँ।\n"
                "कृपया मुझे एडमिन (कम से कम **'Send Messages'** की अनुमति के साथ) बनाएँ और फिर से चैनल का @username भेजें।"
            )
            return GET_CHANNEL_ID # इसी स्टेट में रहें और दोबारा पूछें

    except Exception as e:
        logging.error(f"Error checking admin status: {e}")
        await update.message.reply_text(
            f"चैनल तक पहुँचने में त्रुटि हुई। सुनिश्चित करें कि:\n"
            "1. चैनल का @username/ID सही है।\n"
            "2. चैनल **पब्लिक** है या मैंने आपको **एडमिन** के रूप में जोड़ा है।"
        )
        return GET_CHANNEL_ID # इसी स्टेट में रहें और दोबारा पूछें

# --- लिंक क्लिक होने पर चैनल में मैसेज भेजें (यह काल्पनिक है, /start में ही लॉजिक जोड़ते हैं) ---
async def send_linked_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """जब कोई deep link (/start के बाद कुछ) पर क्लिक करता है तो चैनल में मैसेज भेजता है।"""
    
    # यह लॉजिक सीधे /start फ़ंक्शन में होना चाहिए
    # Deep Linking का उपयोग करने के लिए /start कमांड के बाद 'start' के अलावा 'payload' चेक करें।
    pass


# --- /poll कमांड (चैनल के लिए) ---
async def create_channel_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """चैनल में वोट बनाता है और डीप लिंक के साथ कन्फर्मेशन भेजता है।"""
    
    # 1. कन्वर्सेशन डेटा से चैनल ID निकालें
    channel_id = context.user_data.get('temp_channel_id')
    user = update.effective_user

    if not channel_id:
        await update.message.reply_text(
            "पहले **📝 नया वोट बनाएँ (चैनल के लिए)** पर क्लिक करके चैनल सेट करें।"
        )
        return ConversationHandler.END

    args = update.message.text.split(' ')[1:] # /poll को छोड़कर बाकी टेक्स्ट लें
    
    # ... (बाकी poll बनाने का लॉजिक वही रहेगा) ...
    if not args or '?' not in " ".join(args):
        await update.message.reply_text("कृपया सही फॉर्मेट में सवाल और विकल्प दें।")
        return CREATE_POLL

    full_text = " ".join(args)
    try:
        question, options_str = full_text.split('?', 1)
        question = question.strip()
        options = [opt.strip() for opt in options_str.split(',') if opt.strip() and len(opt.strip()) > 0]
    except:
        await update.message.reply_text("सवाल और विकल्पों को अलग करने के लिए '?' का उपयोग करें।")
        return CREATE_POLL
    
    if len(options) < 2 or len(options) > 10:
        await update.message.reply_text(f"वोट बनाने के लिए 2 से 10 विकल्प चाहिए। आपको मिले: {len(options)}")
        return CREATE_POLL
    
    # 2. चैनल में वोट भेजें
    try:
        poll_message = await context.bot.send_poll(
            chat_id=channel_id,
            question=question,
            options=options,
            is_anonymous=False,
            allows_multiple_answers=False,
        )

        # 3. स्टार्ट इमेज और यूजर डिटेल्स के साथ वेलकम मैसेज
        deep_link_payload = f"poll_{poll_message.message_id}_{channel_id.replace('@', '')}"
        
        # बटन: 'बॉट शुरू करें' बटन बनाएं (deep-link के साथ)
        welcome_keyboard = [[
            InlineKeyboardButton(
                f"👋 {user.first_name} से जुड़ें!", 
                url=f"https://t.me/{context.bot.username}?start={deep_link_payload}"
            )
        ]]
        welcome_markup = InlineKeyboardMarkup(welcome_keyboard)
        
        # चैनल में भेजने वाला मैसेज (जैसा आपने पूछा)
        channel_welcome_message = (
            f"**🥳 नया वोट!**\n\n"
            f"यह वोट यूजर द्वारा बनाया गया है:\n"
            f"👤 **नाम:** [{user.first_name}](tg://user?id={user.id})\n"
            f"🆔 **ID:** `{user.id}`\n"
            f"🌐 **Username:** {f'@{user.username}' if user.username else 'N/A'}\n\n"
            f"इस यूजर से जुड़ने के लिए नीचे दिए बटन पर क्लिक करें।\n"
        )

        # बोट के लॉग/कनेक्शन चैनल में मैसेज भेजें (उदाहरण: @teamrajweb)
        if CHANNEL_USERNAME:
            try:
                await context.bot.send_photo(
                    chat_id=CHANNEL_USERNAME,
                    photo=IMAGE_URL,
                    caption=channel_welcome_message,
                    parse_mode='Markdown',
                    reply_markup=welcome_markup
                )
            except Exception as log_e:
                logging.error(f"Failed to send log message to channel: {log_e}")
        
        # यूजर को कन्फर्मेशन मैसेज
        await update.message.reply_text(
            f"✅ आपका वोट **{channel_id}** चैनल में सफलतापूर्वक भेज दिया गया है!\n"
            f"यूजर लॉग मैसेज आपके कनेक्शन चैनल ({CHANNEL_USERNAME}) में भेज दिया गया है।"
        )
        # कन्वर्सेशन समाप्त करें
        return ConversationHandler.END 

    except Exception as e:
        await update.message.reply_text(f"वोट भेजने में त्रुटि हुई: {e}")
        return CREATE_POLL

# --- कन्वर्सेशन रद्द करें ---
async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """कन्वर्सेशन को रद्द करता है।"""
    await update.message.reply_text('कन्वर्सेशन रद्द कर दिया गया है।')
    return ConversationHandler.END

# --- मुख्य फ़ंक्शन ---
def main():
    """बॉट शुरू करने का मुख्य फ़ंक्शन।"""
    if not BOT_TOKEN:
        logging.error("BOT_TOKEN environment variable सेट नहीं है।")
        return

    # ApplicationBuilder का उपयोग करके Application इंस्टेंस बनाएं
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    
    # 1. /start कमांड हैंडलर
    application.add_handler(CommandHandler("start", start))

    # 2. नया पोल बनाने के लिए कन्वर्सेशन हैंडलर
    poll_conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex('^/poll\b'), create_poll), # /poll से शुरू होने वाले मैसेज
            MessageHandler(filters.Regex('^📝 नया वोट बनाएँ \(चैनल के लिए\)'), start_channel_poll_conversation) # In-line बटन क्लिक
        ],
        states={
            GET_CHANNEL_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_channel_id)],
            CREATE_POLL: [MessageHandler(filters.Regex('^/poll\b'), create_channel_poll)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )

    # 3. कन्वर्सेशन हैंडलर जोड़ें
    application.add_handler(poll_conv_handler)
    application.add_handler(MessageHandler(filters.Regex('^/poll\b'), create_poll)) # सिंपल पोल के लिए भी

    # बॉट शुरू करें
    logging.info(f"बॉट शुरू हो रहा है: @{application.bot.username}")
    application.run_polling(poll_interval=3)
    
if __name__ == '__main__':
    main()
