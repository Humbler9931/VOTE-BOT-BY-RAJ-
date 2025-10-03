import os
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
    CallbackQueryHandler # CallbackQueryHandler इम्पोर्ट किया गया
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
# IMAGE_URL को .env से लिया जाएगा, अगर नहीं मिला तो default URL
IMAGE_URL = os.getenv("IMAGE_URL", "https://picsum.photos/600/300") 
# LOG_CHANNEL_USERNAME को .env से लिया जाएगा
LOG_CHANNEL_USERNAME = os.getenv("LOG_CHANNEL_USERNAME", "@teamrajweb") 

# कन्वर्सेशन स्टेट्स
(GET_CHANNEL_ID, CREATE_CHANNEL_POLL) = range(2) # CHECK_ADMIN की ज़रूरत नहीं, सीधे GET_CHANNEL_ID में चेक होगा

# --- Utility Functions ---

def parse_poll_command(args: list) -> tuple | None:
    """/poll कमांड से सवाल और विकल्पों को पार्स करता है।"""
    full_text = " ".join(args)
    if '?' not in full_text:
        return None # Format error
    
    try:
        question, options_str = full_text.split('?', 1)
        question = question.strip()
        options = [opt.strip() for opt in options_str.split(',') if opt.strip()]
        
        if not question or len(options) < 2 or len(options) > 10:
            return None # Invalid poll data
        
        return question, options
    except:
        return None

# --- Core Bot Functions ---

# FAILED IMAGE LOAD पर मैसेज भेजने के लिए अलग फ़ंक्शन
async def send_start_message(update: Update, context: ContextTypes.DEFAULT_TYPE, reply_markup: InlineKeyboardMarkup, welcome_message: str):
    """इमेज या टेक्स्ट के साथ स्टार्ट मैसेज भेजता है।"""
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

# 1. /start कमांड के लिए फ़ंक्शन (ADVANCED)
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """एक एडवांस स्वागत संदेश और इनलाइन बटन्स भेजता है।"""
    
    # Stylish Inline Buttons
    keyboard = [
        [
            InlineKeyboardButton("📝 चैनल के लिए वोट बनाएँ", callback_data='start_channel_poll_conv'),
        ],
        [
            InlineKeyboardButton("📊 मेरे बनाए वोट्स", callback_data='my_polls_list'),
            InlineKeyboardButton("❓ गाइड/मदद", url='https://t.me/teamrajweb')
        ],
        [
            InlineKeyboardButton("🔗 सोर्स कोड", url='https://t.me/teamrajweb'),
            InlineKeyboardButton("📢 चैनल जॉइन करें", url='https://t.me/narzoxbot')
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # Advanced Welcome Message
    welcome_message = (
        "**👑 वोट बॉट में आपका स्वागत है! 👑**\n\n"
        "मैं किसी भी ग्रुप या चैनल के लिए **सुंदर और सुरक्षित** वोट बनाने में माहिर हूँ। "
        "चैनल के लिए वोट बनाने हेतु *'📝 चैनल के लिए वोट बनाएँ'* पर क्लिक करें।\n\n"
        "__**Stylish Quote:**__\n"
        "*\"आपके विचार मायने रखते हैं। वोट दें, बदलाव लाएँ।\"*\n"
        "~ The Voting Bot"
    )
    
    await send_start_message(update, context, reply_markup, welcome_message)

# 2. साधारण /poll कमांड के लिए फ़ंक्शन (BUG FIX: NameError resolve)
async def create_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """साधारण चैट में एक वोट बनाता है।"""
    
    parsed_data = parse_poll_command(context.args)
    if not parsed_data:
        await update.message.reply_text(
            "कृपया सही फॉर्मेट का उपयोग करें:\n"
            "`/poll [सवाल]? [ऑप्शन1], [ऑप्शन2], ...`"
            "\n(कम से कम 2 और अधिकतम 10 विकल्प)"
            , parse_mode='Markdown'
        )
        return

    question, options = parsed_data
    
    await context.bot.send_poll(
        chat_id=update.effective_chat.id,
        question=question,
        options=options,
        is_anonymous=False, 
        allows_multiple_answers=False, 
    )

    await update.message.reply_text("✅ आपका वोट सफलतापूर्वक बना दिया गया है!")


# --- Conversation Handlers ---

# 3. चैनल पोल कन्वर्सेशन शुरू करें (Callback Handler)
async def start_channel_poll_conversation_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Callback query से कन्वर्सेशन शुरू करता है।"""
    query = update.callback_query
    await query.answer()
    
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="👋 **चैनल सेटअप:**\n"
             "कृपया उस **चैनल का @username या ID** भेजें जिसके लिए आप वोट बनाना चाहते हैं।\n\n"
             "*(उदाहरण: `@my_channel_name` या `-100123456789`)*"
             "\n\n**नोट:** मुझे इस चैनल का **एडमिन** होना ज़रूरी है।",
        parse_mode='Markdown'
    )
    return GET_CHANNEL_ID 

# 4. चैनल ID प्राप्त करें और एडमिन चेक करें
async def get_channel_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """यूजर से चैनल ID प्राप्त करता है और बॉट एडमिन की जाँच करता है।"""
    channel_id_input = update.message.text.strip()
    
    # अगर username है तो '@' लगा दें, अगर ID है तो रहने दें
    channel_id = channel_id_input if channel_id_input.startswith(('@', '-')) else f"@{channel_id_input}"
    
    context.user_data['temp_channel_id'] = channel_id

    try:
        # बॉट की एडमिन स्थिति की जाँच करें
        chat_member = await context.bot.get_chat_member(chat_id=channel_id, user_id=context.bot.id)
        
        if chat_member.status in ['administrator', 'creator']:
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
        logging.error(f"Error checking admin status: {e}")
        await update.message.reply_text(
            f"⚠️ **चैनल तक पहुँचने में त्रुटि** हुई। सुनिश्चित करें कि:\n"
            "1. चैनल का @username/ID सही है।\n"
            "2. चैनल **पब्लिक** है या आपने मुझे चैनल में **एडमिन** के रूप में जोड़ा है।"
        )
        return GET_CHANNEL_ID 

# 5. /poll कमांड (चैनल के लिए)
async def create_channel_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """चैनल में वोट बनाता है और लॉग चैनल में सूचना भेजता है।"""
    
    channel_id = context.user_data.get('temp_channel_id')
    user = update.effective_user

    # Poll data parse करें
    parsed_data = parse_poll_command(update.message.text.split(' ')[1:])
    if not parsed_data:
        await update.message.reply_text("वोट का फॉर्मेट गलत है। कृपया फिर से प्रयास करें।")
        return CREATE_CHANNEL_POLL

    question, options = parsed_data
    
    # 2. चैनल में वोट भेजें
    try:
        poll_message = await context.bot.send_poll(
            chat_id=channel_id,
            question=question,
            options=options,
            is_anonymous=False, # जैसा आपने अनुरोध किया
            allows_multiple_answers=False,
        )

        # 3. लॉग चैनल में मैसेज भेजें (यूजर और वोट डिटेल्स)
        deep_link_payload = f"poll_{poll_message.message_id}_{str(channel_id).replace('@', '')}"
        
        welcome_keyboard = [[
            InlineKeyboardButton(
                f"👋 {user.first_name} से जुड़ें!", 
                url=f"https://t.me/{context.bot.username}?start={deep_link_payload}"
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
                # LOG_CHANNEL_USERNAME में फोटो और डिटेल्स भेजें
                await context.bot.send_photo(
                    chat_id=LOG_CHANNEL_USERNAME,
                    photo=IMAGE_URL,
                    caption=channel_welcome_message,
                    parse_mode='Markdown',
                    reply_markup=welcome_markup
                )
            except Exception as log_e:
                logging.error(f"Failed to send log message to channel: {log_e}")
        
        await update.message.reply_text(
            f"✅ आपका वोट **{channel_id}** चैनल में सफलतापूर्वक भेज दिया गया है!\n"
            f"लॉग मैसेज **{LOG_CHANNEL_USERNAME}** में भेज दिया गया है।",
            parse_mode='Markdown'
        )
        return ConversationHandler.END 

    except Exception as e:
        await update.message.reply_text(f"वोट भेजने में त्रुटि हुई: {e}")
        return CREATE_CHANNEL_POLL

# 6. कन्वर्सेशन रद्द करें
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

    # ApplicationBuilder का उपयोग करके Application इंस्टेंस बनाएं (नया तरीका)
    application = ApplicationBuilder().token(BOT_TOKEN).build()
    
    # 1. /start कमांड हैंडलर
    application.add_handler(CommandHandler("start", start))

    # 2. साधारण /poll कमांड हैंडलर (BUG FIX)
    application.add_handler(CommandHandler("poll", create_poll))

    # 3. चैनल पोल के लिए कन्वर्सेशन हैंडलर
    poll_conv_handler = ConversationHandler(
        entry_points=[
            # '📝 चैनल के लिए वोट बनाएँ' बटन से शुरू
            CallbackQueryHandler(start_channel_poll_conversation_cb, pattern='^start_channel_poll_conv$'),
        ],
        states={
            GET_CHANNEL_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_channel_id)],
            CREATE_CHANNEL_POLL: [MessageHandler(filters.COMMAND('poll'), create_channel_poll)],
        },
        fallbacks=[CommandHandler('cancel', cancel)],
    )

    # 4. कन्वर्सेशन हैंडलर जोड़ें
    application.add_handler(poll_conv_handler)
    
    # बॉट शुरू करें
    logging.info(f"बॉट शुरू हो रहा है...")
    application.run_polling(poll_interval=3)
    
if __name__ == '__main__':
    main()
