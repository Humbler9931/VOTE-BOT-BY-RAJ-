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
# आवश्यक मॉड्यूल ही रखें
from telegram.constants import ChatMemberStatus
from collections import defaultdict 
from telegram.error import BadRequest, Forbidden 
from typing import Tuple, Optional # Type Hinting के लिए

# .env फ़ाइल से environment variables लोड करें
load_dotenv()

# लॉगिंग सेट करें
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

# एनवायर्नमेंट वेरिएबल्स को सुरक्षित रूप से लें
BOT_TOKEN = os.getenv("BOT_TOKEN")
IMAGE_URL = os.getenv("IMAGE_URL", "https://picsum.photos/600/300") # Stylish Placeholder Image
LOG_CHANNEL_USERNAME = os.getenv("LOG_CHANNEL_USERNAME", "@teamrajweb") # Log Channel

# कन्वर्सेशन स्टेट्स
(GET_CHANNEL_ID,) = range(1)

# डेटाबेस के बिना वोट ट्रैक करने के लिए दो ग्लोबल डिक्शनरी (अस्थायी!)
# बेहतर परफॉर्मेंस के लिए इसे डेटाबेस (जैसे Redis/SQL) में उपयोग करें।
VOTES_TRACKER = defaultdict(dict) # {user_id: {channel_id: True}}
VOTES_COUNT = defaultdict(int) # {channel_id: count}

# ----------------------------------------
# Utility / Parsing Helpers
# ----------------------------------------
def parse_poll_from_text(text: str) -> Optional[Tuple[str, list]]:
    """किसी raw text में से poll parse करें। सवाल और ऑप्शन की सीमा 2-10 रखी गई है।"""
    if not text or '?' not in text:
        return None
    try:
        question_part, options_part = text.split('?', 1)
        question = question_part.strip()
        options_part = options_part.strip()
        # Regex से split करके बेहतर ट्रिमिंग सुनिश्चित करें
        options = [opt.strip() for opt in re.split(r',\s*', options_part) if opt.strip()]
        
        if not question or len(options) < 2 or len(options) > 10:
            return None
        return question, options
    except Exception:
        logging.exception("parse_poll_from_text failed")
        return None

# ----------------------------------------
# Core Bot Functions 
# ----------------------------------------
async def send_start_message(update: Update, context: ContextTypes.DEFAULT_TYPE, reply_markup: InlineKeyboardMarkup, welcome_message: str, chat_id: int | None = None):
    """इमेज या टेक्स्ट के साथ स्टार्ट मैसेज भेजता है।"""
    target_chat_id = chat_id if chat_id else update.effective_chat.id
    try:
        # इमेज के साथ आकर्षक वेलकम मैसेज
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
            # अगर इमेज भेजने में त्रुटि हो, तो फॉलबैक टेक्स्ट मैसेज
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
    
    # --- DEEP LINK LOGIC (Channel Join Tracker) ---
    if context.args:
        payload = context.args[0]
        match = re.match(r'link_(\d+)', payload)

        if match:
            channel_id_str = match.groups()[0]
            # Telegram Channel IDs are typically -100XXXXXXXXXX
            target_channel_id_numeric = int(f"-100{channel_id_str}") 
            
            # वर्तमान वोट की संख्या प्राप्त करें
            current_vote_count = VOTES_COUNT[target_channel_id_numeric]

            try:
                chat_info = await context.bot.get_chat(chat_id=target_channel_id_numeric)
                channel_title = chat_info.title
                channel_url = chat_info.invite_link or f"https://t.me/{chat_info.username}" if chat_info.username else None
                
                # A. User को कन्फर्मेशन मैसेज भेजें
                await update.message.reply_text(
                    f"✨ **You've Successfully Connected!** 🎉\n\n"
                    f"आप चैनल **`{channel_title}`** से सफलतापूर्वक जुड़ गए हैं।\n"
                    f"यह लिंक अब सक्रिय (Active) है। आप अब वोट दे सकते हैं, अगर किसी पोस्ट पर वोटिंग सक्रिय है।"
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
                vote_callback_data = f'vote_{target_channel_id_numeric}'
                vote_button_text = f"✅ Vote Now ({current_vote_count} Votes)"

                channel_keyboard = []
                # 1. Vote Button (वोट काउंट के साथ)
                channel_keyboard.append([
                    InlineKeyboardButton(vote_button_text, callback_data=vote_callback_data)
                ])
                
                # 2. Go to Channel button
                if channel_url:
                    channel_keyboard.append([
                        InlineKeyboardButton("➡️ Go to Channel", url=channel_url)
                    ])

                channel_markup = InlineKeyboardMarkup(channel_keyboard)

                # Image के साथ एडवांस मैसेज भेजें (Optional: Log Channel में भी भेज सकते हैं)
                try:
                    await context.bot.send_photo(
                        chat_id=target_channel_id_numeric,
                        photo=IMAGE_URL,
                        caption=notification_message,
                        parse_mode='Markdown',
                        reply_markup=channel_markup
                    )
                except (Forbidden, BadRequest) as fb_e:
                     # यदि बॉट चैनल में पोस्ट नहीं कर सकता
                    logging.warning(f"Failed to send notification to channel {target_channel_id_numeric}: {fb_e}")

                return

            except Exception as e:
                logging.error(f"Deep link notification failed: {e}")
                await update.message.reply_text("माफ़ करना, चैनल से जुड़ने/सूचना भेजने में त्रुटि हुई। सुनिश्चित करें कि बॉट चैनल का एडमिन है और सही अनुमतियाँ (permissions) प्राप्त हैं।")
    
    # --- REGULAR START MENU (Stylish Buttons) ---
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
        "चैनल को कनेक्ट कर **तुरंत शेयर लिंक** पाने हेतु *'🔗 अपनी लिंक बनाएँ'* पर क्लिक करें।\n\n"
        "__**Stylish Quote:**__\n"
        "*\"आपके विचार मायने रखते हैं। वोट दें, बदलाव लाएँ।\"*\n"
        "~ The Voting Bot"
    )

    await send_start_message(update, context, reply_markup, welcome_message)


# 2. साधारण /poll कमांड (chat में)
async def create_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """एक साधारण Telegram poll बनाता है (Voting Bot के लिए आवश्यक नहीं, लेकिन उपयोगी)।"""
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
            is_anonymous=False, # इसे anonymous नहीं रखते हैं
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
             "कृपया उस **चैनल का @username या ID** (`-100...`) भेजें जिसके लिए आप लिंक जनरेट करना चाहते हैं।\n\n"
             "**नोट:** मुझे इस चैनल का **एडमिन** होना ज़रूरी है।",
        parse_mode='Markdown'
    )
    return GET_CHANNEL_ID


# 4. चैनल ID प्राप्त करें, बॉट एडमिन चेक करें और INSTANT LINK भेजें
async def get_channel_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    channel_id_input = update.message.text.strip()
    user = update.effective_user

    # ID detection and normalization logic
    if re.match(r'^-?\d+$', channel_id_input):
        channel_id = int(channel_id_input)
    else:
        channel_id = channel_id_input if channel_id_input.startswith('@') else f"@{channel_id_input}"

    try:
        bot_user = await context.bot.get_me()
        bot_id = bot_user.id
        bot_username = bot_user.username or "bot"

        # 1. बॉट एडमिन चेक करें और चैट की जानकारी प्राप्त करें
        chat_member = await context.bot.get_chat_member(chat_id=channel_id, user_id=bot_id)
        chat_info = await context.bot.get_chat(chat_id=channel_id)
        
        if getattr(chat_member, "status", "").lower() in ['administrator', 'creator']:
            
            # 2. सफलता: INSTANT UNIQUE LINK बनाएं
            raw_id_str = str(chat_info.id)
            link_channel_id = raw_id_str[4:] if raw_id_str.startswith('-100') else raw_id_str.replace('-', '')

            deep_link_payload = f"link_{link_channel_id}"
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
            
            # 4. बटन भेजें
            share_keyboard = [[
                InlineKeyboardButton("🔗 अपनी लिंक शेयर करें", url=share_url),
            ]]
            share_markup = InlineKeyboardMarkup(share_keyboard)
            
            await update.message.reply_text(
                "शेयर करने के लिए बटन दबाएँ:",
                reply_markup=share_markup
            )
            
            # 5. LOG_CHANNEL_USERNAME में सूचना भेजें
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


# ----------------------------------------
# Vote Handler (Fixing Subscription Check Error)
# ----------------------------------------
async def handle_vote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    
    # 1. Callback data से Channel ID निकालें
    data = query.data
    match = re.match(r'vote_(-?\d+)', data)
    
    if not match:
        await query.answer(text="❌ त्रुटि: वोट ID सही नहीं है।", show_alert=True)
        return

    channel_id_numeric = int(match.group(1))
    user_id = query.from_user.id
    
    # 2. One-Time Vote Logic Check
    has_voted = VOTES_TRACKER[user_id].get(channel_id_numeric, False)
    
    if has_voted:
        await query.answer(text="🗳️ आप पहले ही इस पोस्ट पर वोट कर चुके हैं।", show_alert=True)
        return
        
    # 3. यूज़र का सब्सक्रिप्शन स्टेटस चेक करें (सबसे महत्वपूर्ण सुधार)
    is_subscriber = False
    channel_url = None
    
    try:
        # चैनल की जानकारी पहले ही प्राप्त कर लें (URL के लिए)
        chat_info = await context.bot.get_chat(chat_id=channel_id_numeric)
        channel_url = chat_info.invite_link or f"https://t.me/{chat_info.username}" if chat_info.username else None
        
        # बॉट सदस्य की स्थिति की जाँच करने का प्रयास करता है
        chat_member = await context.bot.get_chat_member(chat_id=channel_id_numeric, user_id=user_id)
        is_subscriber = chat_member.status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]
        
    except (Forbidden, BadRequest) as e:
        # यह त्रुटि तब आती है जब बॉट एडमिन तो है, लेकिन उसके पास 'Manage Users' की अनुमति नहीं है।
        logging.error(f"Bot failed to check subscriber status for {channel_id_numeric}: {e}")
        
        # एडमिन के लिए स्पष्ट अलर्ट
        await query.answer(
            text="🚨 वोटिंग त्रुटि: बॉट चैनल सदस्यता जाँचने में असमर्थ है। कृपया सुनिश्चित करें कि बॉट के पास **'उपयोगकर्ताओं को प्रबंधित करें' (Manage Users)** की अनुमति है।",
            show_alert=True
        )
        return
    except Exception as e:
        # Catch any other unexpected error
        logging.exception(f"Unknown error in handle_vote for {channel_id_numeric}")
        await query.answer(
            text="⚠️ अप्रत्याशित त्रुटि हुई। कृपया दोबारा प्रयास करें या चैनल एडमिन से संपर्क करें।",
            show_alert=True
        )
        return

    # 4. वोटिंग लॉजिक (अगर सदस्यता जाँच सफल रही)
    
    if not is_subscriber:
        # अगर सब्सक्राइबर नहीं है
        await query.answer(
            text="❌ आप वोट नहीं कर सकते। कृपया पहले चैनल को सब्सक्राइब करें।", 
            show_alert=True,
            url=channel_url if channel_url else None # अगर URL है तो क्लिकेबल बटन दिखाएगा
        )
        return
    
    else:
        # सफल वोट
        
        VOTES_TRACKER[user_id][channel_id_numeric] = True
        VOTES_COUNT[channel_id_numeric] += 1
        current_vote_count = VOTES_COUNT[channel_id_numeric]
        
        await query.answer(text=f"✅ आपका वोट ({current_vote_count}वां) दर्ज कर लिया गया है। धन्यवाद!", show_alert=True)
        
        # 5. बटन को नए वोट काउंट के साथ अपडेट करें
        original_markup = query.message.reply_markup
        new_keyboard = []
        
        if original_markup and original_markup.inline_keyboard:
            for row in original_markup.inline_keyboard:
                new_row = []
                for button in row:
                    if button.callback_data and button.callback_data.startswith('vote_'):
                        new_button_text = f"✅ Vote Now ({current_vote_count} Votes)"
                        new_row.append(InlineKeyboardButton(new_button_text, callback_data=button.callback_data))
                    else:
                        new_row.append(button)
                new_keyboard.append(new_row)
        
        new_markup = InlineKeyboardMarkup(new_keyboard)
        
        try:
            await query.edit_message_reply_markup(reply_markup=new_markup)
        except Exception as e:
             # इसे warning के बजाय debug या info पर रखें, क्योंकि यह अक्सर होता है
             logging.info(f"Could not edit vote message markup: {e}")
            
# ----------------------------------------
# main() - Application Setup
# ----------------------------------------
def main():
    """बॉट एप्लीकेशन शुरू करता है और सभी हैंडल्स जोड़ता है।"""
    if not BOT_TOKEN:
        logging.error("BOT_TOKEN environment variable सेट नहीं है। कृपया .env फ़ाइल में TOKEN जोड़ें।")
        return

    application = ApplicationBuilder().token(BOT_TOKEN).build() 

    # 1. /start (Deep Link Logic Included)
    application.add_handler(CommandHandler("start", start))

    # 2. simple /poll for chats (Optional feature)
    application.add_handler(CommandHandler("poll", create_poll))

    # 3. Vote Callback Handler 
    application.add_handler(CallbackQueryHandler(handle_vote, pattern=r'^vote_(-?\d+)$')) 

    # 4. Conversation for instant link generation
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

    logging.info("👑 Stylish Voting Bot Starting... 🚀")
    application.run_polling(poll_interval=2) 


if __name__ == '__main__':
    main()
