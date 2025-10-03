import os
import logging
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputFile
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler

# .env फ़ाइल से environment variables लोड करें
load_dotenv()

# लॉगिंग (Logging) सेट करें ताकि आपको पता चले कि क्या हो रहा है
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# टेलीग्राम बॉट टोकन और एक वैकल्पिक इमेज URL को environment variable से प्राप्त करें
BOT_TOKEN = os.getenv("BOT_TOKEN")
# नोट: आप इस URL को Render/GitHub/Telegram के फ़ाइल ID से बदल सकते हैं 
# या इसे local storage से लोड करने के लिए फाइल पाथ दे सकते हैं।
IMAGE_URL = "https://envs.sh/KXK.jpg/IMG20251003570.jpg" 

# /start कमांड के लिए फ़ंक्शन (ADVANCED)
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """एक एडवांस स्वागत संदेश, इमेज, और इनलाइन बटन्स भेजता है।"""
    
    # 1. स्टाइलिश इनलाइन बटन्स बनाएँ
    keyboard = [
        [
            InlineKeyboardButton("📝 नया वोट बनाएँ", callback_data='create_new_poll'),
            InlineKeyboardButton("❓ गाइड/मदद", url='https://t.me/teamrajweb')
        ],
        [
            InlineKeyboardButton("📊 मेरे बनाए वोट्स", callback_data='my_polls_list'),
            InlineKeyboardButton("🔗 सोर्स कोड", url='https://t.me/teamrajweb')
        ],
        [
            InlineKeyboardButton("📢 चैनल जॉइन करें", url='https://t.me/narzoxbot)
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    # 2. एडवांस वेलकम मैसेज
    welcome_message = (
        "**🎉 वोट बॉट में आपका स्वागत है! 🎉**\n\n"
        "मैं ग्रुप्स और चैट्स में आसानी से वोट बनाने में आपकी मदद करता हूँ। "
        "नीचे दिए गए बटनों का उपयोग करके अपनी यात्रा शुरू करें।\n\n"
        "**_एडवांस फीचर:_** आप अपने वोट में इमोजी और लिंक भी इस्तेमाल कर सकते हैं!\n\n"
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
            parse_mode='Markdown', # मैसेज में **bold** और _italic_ फॉर्मेटिंग के लिए
            reply_markup=reply_markup
        )
    except Exception as e:
        # अगर इमेज लोड नहीं होती है, तो सिर्फ़ टेक्स्ट मैसेज भेजें
        logging.error(f"Image send failed: {e}. Sending text message instead.")
        await update.message.reply_text(
            welcome_message,
            parse_mode='Markdown',
            reply_markup=reply_markup
        )


# /poll कमांड के लिए फ़ंक्शन (पिछला कोड)
async def create_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """यूजर के इनपुट से एक नया वोट (poll) बनाता है और भेजता है।"""
    args = context.args
    
    # चेक करें कि इनपुट सही फॉर्मेट में है या नहीं
    if not args:
        await update.message.reply_text(
            "कृपया सही फॉर्मेट का उपयोग करें: /poll [सवाल]? [ऑप्शन1], [ऑप्शन2], ..."
        )
        return

    full_text = " ".join(args)
    
    # सवाल और ऑप्शंस को अलग करें
    if '?' not in full_text:
        await update.message.reply_text(
            "सवाल के बाद '?' चिह्न ज़रूर लगाएँ।\n"
            "उदाहरण: /poll आज खाने में क्या है? दाल-चावल, रोटी-सब्जी, पिज्जा"
        )
        return

    try:
        # '?' पर स्प्लिट (split) करें
        question, options_str = full_text.split('?', 1)
        question = question.strip()
        options = [opt.strip() for opt in options_str.split(',') if opt.strip()]
    except ValueError:
        await update.message.reply_text("इनपुट फॉर्मेट गलत है। कृपया जांच करें।")
        return

    # ऑप्शंस की संख्या चेक करें (टेलीग्राम को 2-10 ऑप्शन चाहिए)
    if len(options) < 2 or len(options) > 10:
        await update.message.reply_text(
            f"वोट बनाने के लिए 2 से 10 विकल्प (options) होने चाहिए। "
            f"आपको मिले: {len(options)}"
        )
        return

    # वोट (poll) भेजें
    await context.bot.send_poll(
        chat_id=update.effective_chat.id,
        question=question,
        options=options,
        is_anonymous=False, 
        allows_multiple_answers=False, 
    )

    await update.message.reply_text("आपका वोट सफलतापूर्वक बना दिया गया है!")

# मुख्य फ़ंक्शन
def main():
    """बॉट शुरू करने का मुख्य फ़ंक्शन।"""
    if not BOT_TOKEN:
        logging.error("BOT_TOKEN environment variable सेट नहीं है। कृपया .env फ़ाइल चेक करें।")
        return

    # ApplicationBuilder का उपयोग करके Application इंस्टेंस बनाएं
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # कमांड हैंडलर जोड़ें
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("poll", create_poll))

    # बॉट शुरू करें
    logging.info("बॉट शुरू हो रहा है...")
    application.run_polling(poll_interval=3)
    
if __name__ == '__main__':
    main()
