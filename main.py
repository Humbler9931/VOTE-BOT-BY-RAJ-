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
from telegram.constants import ChatMemberStatus

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

# डेटाबेस के बिना वोट ट्रैक करने के लिए एक ग्लोबल डिक्शनरी (अस्थायी!)
# {user_id: {channel_id: True/False}}
VOTES_TRACKER = {} 
# ------------------------------------------------------------------

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
            # ID को वापस full numeric format (-100...) में बदलें (14 digits)
            target_channel_id_numeric = int(f"-100{channel_id_str}") 

            try:
                # चैनल का नाम और URL प्राप्त करें
                chat_info = await context.bot.get_chat(chat_id=target_channel_id_numeric)
                channel_title = chat_info.title
                channel_url = chat_info.invite_link or f"https://t.me/{chat_info.username}" if chat_info.username else None
                
                # A. User को कन्फर्मेशन मैसेज भेजें (Advanced "You have Joined" Message)
                await update.message.reply_text(
                    f"✨ **You have Joined!** 🎉\n\n"
                    f"आप चैनल **`{channel_title}`** से सफलतापूर्वक जुड़ गए हैं।\n"
                    f"आपकी भागीदारी की सूचना अब चैनल एडमिन को भेज दी गई है।"
                )

                # B. Notification message चैनल में भेजें (Advanced Style)
                
                notification_message = (
                    f"**👑 New Participant Joined! 👑**\n"
                    f"--- **Participation Details** ---\n\n"
                    f"👤 **Name:** [{user.first_name}](tg://user?id={user.id})\n"
                    f"🆔 **User ID:** `{user.id}`\n"
                    f"🌐 **Username:** {f'@{user.username}' if user.username else 'N/A'}\n\n"
                    f"🔗 **Channel:** `{channel_title}`\n"
                    f"🤖 **Bot:** @{bot_username}"
                )

                # --- ADVANCED VOTE BUTTON LOGIC ---
                # Callback Data: 'vote_<channel_numeric_id>'
                vote_callback_data = f'vote_{target_channel_id_numeric}'

                channel_keyboard = []
                # 1. Vote Button
                channel_keyboard.append([
                    InlineKeyboardButton("✅ Vote Now", callback_data=vote_callback_data)
                ])
                
                # 2. Go to Channel / Connect button
                if channel_url:
                    channel_keyboard.append([
                        InlineKeyboardButton("➡️ Go to Channel", url=channel_url)
                    ])
                else:
                     channel_keyboard.append([
                        InlineKeyboardButton("💬 Connect with User", url=f"tg://user?id={user.id}")
                    ])

                channel_markup = InlineKeyboardMarkup(channel_keyboard)

                # Image के साथ एडवांस मैसेज भेजें
                await context.bot.send_photo(
                    chat_id=target_channel_id_numeric,
                    photo=IMAGE_URL,
                    caption=notification_message,
                    parse_mode='Markdown',
                    reply_markup=channel_markup
                )
                
                return

            except Exception as e:
                logging.error(f"Deep link notification failed: {e}")
                await update.message.reply_text("माफ़ करना, चैनल से जुड़ने में त्रुटि हुई। सुनिश्चित करें कि बॉट चैनल का एडमिन है।")
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

        # 1. बॉट एडमिन चेक करें और चैट की जानकारी प्राप्त करें
        chat_member = await context.bot.get_chat_member(chat_id=channel_id, user_id=bot_id)
        chat_info = await context.bot.get_chat(chat_id=channel_id) # Chat info एक बार में ही प्राप्त करें
        
        if getattr(chat_member, "status", "").lower() in ['administrator', 'creator']:
            
            # 2. सफलता: INSTANT UNIQUE LINK बनाएं और भेजें
            
            # chat_info.id का उपयोग करके Deep Link payload बनाएं
            raw_id_str = str(chat_info.id)
            if raw_id_str.startswith('-100'):
                link_channel_id = raw_id_str[4:] 
            else:
                link_channel_id = raw_id_str.replace('-', '')

            # Payload: link_<channel_id_clean>
            deep_link_payload = f"link_{link_channel_id}"
            
            # शेयर करने योग्य लिंक
            share_url = f"https://t.me/{bot_username}?start={deep_link_payload}"
            
            channel_title = chat_info.title
            
            # 3. यूज़र को लिंक दिखाएँ (कॉपी करने योग्य)
            await update.message.reply_text(
                f"✅ चैनल **{channel_title}** सफलतापूर्वक कनेक्ट हो गया है!\n\n"
                f"**आपकी शेयर करने योग्य UNIQUE LINK तैयार है। इसे कॉपी करें:**\n"
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

    except Exception as e:
        logging.error(f"Error in get_channel_id for input {channel_id_input}: {e}")
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
# New Vote Handler
# -------------------------
async def handle_vote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer(text="वोट प्रोसेस किया जा रहा है...")
    
    # 1. Callback data से Channel ID निकालें
    data = query.data
    match = re.match(r'vote_(-?\d+)', data)
    
    if not match:
        await query.edit_message_caption(caption="❌ त्रुटि: वोट ID सही नहीं है।", parse_mode='Markdown')
        return

    channel_id_numeric = int(match.group(1))
    user_id = query.from_user.id
    
    # 2. यूज़र का सब्सक्रिप्शन स्टेटस चेक करें
    try:
        chat_member = await context.bot.get_chat_member(chat_id=channel_id_numeric, user_id=user_id)
        is_subscriber = chat_member.status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]
        
    except Exception as e:
        logging.error(f"Error checking subscriber status: {e}")
        await query.edit_message_caption(caption="⚠️ त्रुटि: चैनल सब्सक्रिप्शन चेक नहीं किया जा सका।", parse_mode='Markdown')
        return

    # 3. वोटिंग लॉजिक
    
    # ग्लोबल ट्रैकर में यूज़र की एंट्री देखें
    user_votes = VOTES_TRACKER.get(user_id, {})
    has_voted = user_votes.get(channel_id_numeric, False)

    if not is_subscriber:
        # अगर सब्सक्राइबर नहीं है
        await query.answer(text="❌ आप वोट नहीं कर सकते। कृपया पहले चैनल को सब्सक्राइब करें।", show_alert=True)
    
    elif has_voted:
        # अगर पहले ही वोट कर चुका है
        await query.answer(text="🗳️ आप पहले ही इस चैनल के लिए वोट कर चुके हैं।", show_alert=True)
    
    else:
        # सफल वोट
        
        # वोट को ट्रैक करें (अस्थायी!)
        user_votes[channel_id_numeric] = True
        VOTES_TRACKER[user_id] = user_votes

        # मैसेज को अपडेट करें (वोट काउंटर या सिर्फ़ कन्फर्मेशन)
        
        # (यह हिस्सा वोट काउंटर के लिए है, जिसे हम डेटाबेस के बिना छोड़ रहे हैं)
        # हम सिर्फ़ कन्फर्मेशन मैसेज भेजेंगे
        
        # यूज़र को कन्फर्मेशन दें
        await query.answer(text="✅ आपका वोट दर्ज कर लिया गया है। धन्यवाद!", show_alert=True)
        
        # ओरिजिनल मैसेज के कैप्शन को अपडेट करें
        original_caption = query.message.caption
        new_caption = f"{original_caption}\n\n**✅ Vote Recorded!**"
        
        # बटन हटाएँ (ताकि दोबारा वोट न हो)
        new_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Voted", callback_data='ignore')]
        ])
        
        try:
            await query.edit_message_caption(
                caption=new_caption,
                reply_markup=new_markup,
                parse_mode='Markdown'
            )
        except Exception:
             # अगर मैसेज अपडेट नहीं हो पाता तो सिर्फ़ लॉग करें
            logging.warning("Could not edit vote message caption.")
            
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

    # 3. Vote Callback Handler (New)
    application.add_handler(CallbackQueryHandler(handle_vote, pattern='^vote_(-?\d+)$'))

    # 4. conversation for instant link
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
