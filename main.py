import os
import logging
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, ContextTypes, CommandHandler

# .env फ़ाइल से environment variables लोड करें
load_dotenv()

# लॉगिंग (Logging) सेट करें ताकि आपको पता चले कि क्या हो रहा है
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# टेलीग्राम बॉट टोकन को environment variable से प्राप्त करें
BOT_TOKEN = os.getenv("BOT_TOKEN")

# /start कमांड के लिए फ़ंक्शन
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """जब यूजर /start कमांड भेजता है तो एक स्वागत संदेश भेजता है।"""
    welcome_message = (
        "नमस्ते! मैं एक वोट बॉट हूँ।\n"
        "नया वोट बनाने के लिए /poll [सवाल]? [ऑप्शन1], [ऑप्शन2], ... का उपयोग करें।\n\n"
        "उदाहरण: /poll आज खाने में क्या है? दाल-चावल, रोटी-सब्जी, पिज्जा"
    )
    await update.message.reply_text(welcome_message)

# /poll कमांड के लिए फ़ंक्शन
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
        is_anonymous=False,  # आप इसे True भी कर सकते हैं अगर आप गुमनाम (anonymous) वोट चाहते हैं
        allows_multiple_answers=False, # आप इसे True भी कर सकते हैं 
    )

    await update.message.reply_text("आपका वोट सफलतापूर्वक बना दिया गया है!")

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
