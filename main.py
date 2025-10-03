import os
import re
import logging
from dotenv import load_dotenv
from collections import defaultdict

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup
)
from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest, Forbidden

from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
    ConversationHandler,
    CallbackQueryHandler,
    ChatMemberHandler
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
IMAGE_URL = os.getenv("IMAGE_URL", "https://picsum.photos/600/300")
LOG_CHANNEL_USERNAME = os.getenv("LOG_CHANNEL_USERNAME", "@teamrajweb")

# Conversation states
(GET_CHANNEL_ID,) = range(1)

# Vote tracking (in-memory; restart -> reset)
# VOTES_TRACKER: user_id -> { channel_id: True }
VOTES_TRACKER = {}  # dict: {user_id: {channel_id: True}}
# VOTES_COUNT: channel_id -> int
VOTES_COUNT = defaultdict(int)

# VOTE_MESSAGES: channel_id -> {'msgs': set(message_id), 'channel_url': str or None}
VOTE_MESSAGES = defaultdict(lambda: {"msgs": set(), "channel_url": None})

# -------------------------
# Helpers
# -------------------------
def parse_poll_from_args(args: list) -> tuple | None:
    if not args:
        return None
    full_text = " ".join(args)
    return parse_poll_from_text(full_text)

def parse_poll_from_text(text: str) -> tuple | None:
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

async def send_start_message(update: Update, context: ContextTypes.DEFAULT_TYPE, reply_markup: InlineKeyboardMarkup, welcome_message: str, chat_id=None):
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

# -------------------------
# Handlers
# -------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    bot_user = await context.bot.get_me()
    bot_username = bot_user.username

    # Deep link logic
    if context.args:
        payload = context.args[0]
        match = re.match(r'link_(\d+)', payload)
        if match:
            channel_id_str = match.groups()[0]
            target_channel_id_numeric = int(f"-100{channel_id_str}")

            current_vote_count = VOTES_COUNT[target_channel_id_numeric]

            try:
                chat_info = await context.bot.get_chat(chat_id=target_channel_id_numeric)
                channel_title = chat_info.title
                channel_url = chat_info.invite_link or (f"https://t.me/{chat_info.username}" if chat_info.username else None)

                await update.message.reply_text(
                    f"✨ **You have Joined!** 🎉\n\n"
                    f"आप चैनल **`{channel_title}`** से सफलतापूर्वक जुड़ गए हैं।\n"
                    f"आपकी भागीदारी की सूचना अब चैनल एडमिन को भेज दी गई है।"
                )

                notification_message = (
                    f"**👑 New Participant Joined! 👑**\n"
                    f"--- **Participation Details** ---\n\n"
                    f"👤 **Name:** [{user.first_name}](tg://user?id={user.id})\n"
                    f"🆔 **User ID:** `{user.id}`\n"
                    f"🌐 **Username:** {f'@{user.username}' if user.username else 'N/A'}\n\n"
                    f"🔗 **Channel:** `{channel_title}`\n"
                    f"🤖 **Bot:** @{bot_username}"
                )

                vote_callback_data = f'vote_{target_channel_id_numeric}'
                vote_button_text = f"✅ Vote Now ({current_vote_count} Votes)"

                channel_keyboard = [[InlineKeyboardButton(vote_button_text, callback_data=vote_callback_data)]]

                if channel_url:
                    channel_keyboard.append([InlineKeyboardButton("➡️ Go to Channel", url=channel_url)])
                else:
                    channel_keyboard.append([InlineKeyboardButton("💬 Connect with User", url=f"tg://user?id={user.id}")])

                channel_markup = InlineKeyboardMarkup(channel_keyboard)

                # Send photo and **store message_id** so we can update the vote button later
                sent_msg = await context.bot.send_photo(
                    chat_id=target_channel_id_numeric,
                    photo=IMAGE_URL,
                    caption=notification_message,
                    parse_mode='Markdown',
                    reply_markup=channel_markup
                )

                # Store message id and channel_url for later updates
                VOTE_MESSAGES[target_channel_id_numeric]["msgs"].add(sent_msg.message_id)
                VOTE_MESSAGES[target_channel_id_numeric]["channel_url"] = channel_url

                return

            except Exception as e:
                logging.error(f"Deep link notification failed: {e}")
                await update.message.reply_text("माफ़ करना, चैनल से जुड़ने में त्रुटि हुई। सुनिश्चित करें कि बॉट चैनल का एडमिन है।")

    # Regular start menu
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

async def create_poll(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

async def get_channel_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    channel_id_input = update.message.text.strip()
    user = update.effective_user

    numeric_match = re.match(r'^-?\d+$', channel_id_input)
    if numeric_match:
        channel_id = int(channel_id_input)
    else:
        channel_id = channel_id_input if channel_id_input.startswith('@') else f"@{channel_id_input}"

    try:
        bot_user = await context.bot.get_me()
        bot_id = bot_user.id
        bot_username = bot_user.username or "bot"

        chat_member = await context.bot.get_chat_member(chat_id=channel_id, user_id=bot_id)
        chat_info = await context.bot.get_chat(chat_id=channel_id)

        if getattr(chat_member, "status", "").lower() in ['administrator', 'creator']:
            raw_id_str = str(chat_info.id)
            if raw_id_str.startswith('-100'):
                link_channel_id = raw_id_str[4:]
            else:
                link_channel_id = raw_id_str.replace('-', '')

            deep_link_payload = f"link_{link_channel_id}"
            share_url = f"https://t.me/{bot_username}?start={deep_link_payload}"
            channel_title = chat_info.title

            await update.message.reply_text(
                f"✅ चैनल **{channel_title}** सफलतापूर्वक कनेक्ट हो गया है!\n\n"
                f"**आपकी शेयर करने योग्य UNIQUE LINK तैयार है। इसे कॉपी करें:**\n"
                f"```\n{share_url}\n```\n\n"
                f"**या इस बटन का उपयोग करें:**",
                parse_mode='Markdown'
            )

            share_keyboard = [[InlineKeyboardButton("🔗 अपनी लिंक शेयर करें", url=share_url)]]
            share_markup = InlineKeyboardMarkup(share_keyboard)

            await update.message.reply_text("शेयर करने के लिए बटन दबाएँ:", reply_markup=share_markup)

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

            return ConversationHandler.END
        else:
            await update.message.reply_text(
                "❌ मैं आपके चैनल का **एडमिन नहीं** हूँ।\n"
                "कृपया मुझे एडमिन (कम से कम **'Post Messages'** की अनुमति के साथ) बनाएँ और फिर से चैनल का @username/ID भेजें।"
            )
            return GET_CHANNEL_ID

    except Exception as e:
        logging.error(f"Error in get_channel_id for input {channel_id_input}: {e}")
        await update.message.reply_text(
            "⚠️ **चैनल तक पहुँचने में त्रुटि** हुई। सुनिश्चित करें कि:\n"
            "1. चैनल का @username/ID सही है।\n"
            "2. चैनल **पब्लिक** है या आपने मुझे चैनल में **एडमिन** के रूप में जोड़ा है।"
        )
        return GET_CHANNEL_ID

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text('कन्वर्सेशन रद्द कर दिया गया है।')
    return ConversationHandler.END

# -------------------------
# Vote handling & updates
# -------------------------
async def update_channel_vote_buttons(context: ContextTypes.DEFAULT_TYPE, channel_id: int):
    """Helper: सभी स्टोर किए गए message_ids पर वोट बटन टेक्स्ट अपडेट करें"""
    try:
        current_vote_count = VOTES_COUNT.get(channel_id, 0)
        stored = VOTE_MESSAGES.get(channel_id, {"msgs": set(), "channel_url": None})
        channel_url = stored.get("channel_url", None)

        new_button_text = f"✅ Vote Now ({current_vote_count} Votes)"
        new_keyboard = [[InlineKeyboardButton(new_button_text, callback_data=f'vote_{channel_id}')]]
        if channel_url:
            new_keyboard.append([InlineKeyboardButton("➡️ Go to Channel", url=channel_url)])
        new_markup = InlineKeyboardMarkup(new_keyboard)

        for mid in list(stored.get("msgs", set())):
            try:
                await context.bot.edit_message_reply_markup(chat_id=channel_id, message_id=mid, reply_markup=new_markup)
            except Exception as e:
                logging.warning(f"Could not edit message {mid} in {channel_id}: {e}")
    except Exception:
        logging.exception("update_channel_vote_buttons failed")

async def handle_vote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()  # immediate feedback (without a visible alert)
    data = query.data
    match = re.match(r'vote_(-?\d+)', data)

    if not match:
        await query.answer(text="❌ त्रुटि: वोट ID सही नहीं है।", show_alert=True)
        return

    channel_id_numeric = int(match.group(1))
    user_id = query.from_user.id

    # One-time vote check
    user_votes = VOTES_TRACKER.get(user_id, {})
    has_voted = user_votes.get(channel_id_numeric, False)
    if has_voted:
        await query.answer(text="🗳️ आप पहले ही इस पोस्ट पर वोट कर चुके हैं।", show_alert=True)
        return

    # Check subscription status
    is_subscriber = False
    channel_url = None
    try:
        chat_info = await context.bot.get_chat(chat_id=channel_id_numeric)
        channel_url = chat_info.invite_link or (f"https://t.me/{chat_info.username}" if chat_info.username else None)

        chat_member = await context.bot.get_chat_member(chat_id=channel_id_numeric, user_id=user_id)
        is_subscriber = chat_member.status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]

    except (Forbidden, BadRequest) as e:
        logging.error(f"Bot failed to check subscriber status for {channel_id_numeric}: {e}")
        await query.answer(
            text="🚨 वोटिंग त्रुटि: बॉट चैनल सदस्यता जाँचने में असमर्थ है। कृपया सुनिश्चित करें कि बॉट के पास आवश्यक अनुमति है।",
            show_alert=True
        )
        return
    except Exception as e:
        logging.exception(f"Unknown error in handle_vote for {channel_id_numeric}: {e}")
        await query.answer(
            text="⚠️ अप्रत्याशित त्रुटि हुई। कृपया दोबारा प्रयास करें।",
            show_alert=True
        )
        return

    if not is_subscriber:
        # Ask to subscribe - show channel link if available
        if channel_url:
            await query.answer(text="❌ आप वोट नहीं कर सकते। कृपया पहले चैनल को सब्सक्राइब करें।", show_alert=True)
            # optionally send a message with channel_url
            try:
                await context.bot.send_message(chat_id=user_id, text=f"कृपया चैनल जॉइन करें: {channel_url}")
            except Exception:
                pass
        else:
            await query.answer(text="❌ आप वोट नहीं कर सकते। कृपया पहले चैनल को सब्सक्राइब करें। (चैनल लिंक उपलब्ध नहीं)", show_alert=True)
        return
    else:
        # Register vote
        user_votes[channel_id_numeric] = True
        VOTES_TRACKER[user_id] = user_votes

        VOTES_COUNT[channel_id_numeric] += 1
        current_vote_count = VOTES_COUNT[channel_id_numeric]

        await query.answer(text=f"✅ आपका वोट ({current_vote_count}वां) दर्ज कर लिया गया है। धन्यवाद!", show_alert=True)

        # Update the specific message where user clicked (fast) and update all channel messages we recorded
        try:
            # Update clicked message reply_markup if possible
            original_markup = query.message.reply_markup
            new_keyboard = []
            if original_markup and original_markup.inline_keyboard:
                for row in original_markup.inline_keyboard:
                    new_row = []
                    for button in row:
                        if button.callback_data and button.callback_data.startswith('vote_'):
                            new_row.append(InlineKeyboardButton(f"✅ Vote Now ({current_vote_count} Votes)", callback_data=button.callback_data))
                        else:
                            new_row.append(button)
                    new_keyboard.append(new_row)
            else:
                new_keyboard = [[InlineKeyboardButton(f"✅ Vote Now ({current_vote_count} Votes)", callback_data=f'vote_{channel_id_numeric}')]]

            new_markup = InlineKeyboardMarkup(new_keyboard)
            try:
                await query.edit_message_reply_markup(reply_markup=new_markup)
            except Exception as e:
                logging.warning(f"Could not edit reply_markup of clicked message: {e}")

            # Update all stored channel messages
            await update_channel_vote_buttons(context, channel_id_numeric)

        except Exception:
            logging.exception("Failed to update message markup after vote")

# -------------------------
# ChatMember updates handler (detect join/leave and remove vote if left)
# -------------------------
async def handle_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Called when any member's status in a chat changes (requires bot to receive chat member updates).
    If a user leaves/kicked from a channel for which we tracked a vote, remove their vote and decrement count.
    """
    try:
        cmu = update.chat_member  # ChatMemberUpdated
        chat = cmu.chat
        old = cmu.old_chat_member
        new = cmu.new_chat_member

        channel_id = chat.id
        target_user = new.user  # the affected user
        target_user_id = target_user.id

        # Detect a leave/kick: old was member/admin/creator and new is left/kicked
        old_status = getattr(old, "status", "").lower()
        new_status = getattr(new, "status", "").lower()

        left_states = ['left', 'kicked', 'restricted']  # restricted may not always mean left but included cautiously

        was_member = old_status in ['member', 'administrator', 'creator']
        is_now_left = new_status in left_states

        if was_member and is_now_left:
            # If this user had voted for this channel, remove their vote
            user_votes = VOTES_TRACKER.get(target_user_id, {})
            if user_votes and user_votes.get(channel_id, False):
                # remove vote
                del user_votes[channel_id]
                if user_votes:
                    VOTES_TRACKER[target_user_id] = user_votes
                else:
                    del VOTES_TRACKER[target_user_id]

                # decrement global count (safety)
                old_count = VOTES_COUNT.get(channel_id, 0)
                new_count = max(0, old_count - 1)
                VOTES_COUNT[channel_id] = new_count

                logging.info(f"Removed vote of user {target_user_id} for channel {channel_id}. Votes: {old_count} -> {new_count}")

                # Update stored messages' markup to new count
                await update_channel_vote_buttons(context, channel_id)

    except Exception:
        logging.exception("Error in handle_chat_member_update")

# -------------------------
# main()
# -------------------------
def main():
    if not BOT_TOKEN:
        logging.error("BOT_TOKEN environment variable सेट नहीं है।")
        return

    application = ApplicationBuilder().token(BOT_TOKEN).build()

    # Handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("poll", create_poll))
    application.add_handler(CallbackQueryHandler(handle_vote, pattern=r'^vote_(-?\d+)$'))

    link_conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_channel_poll_conversation_cb, pattern='^start_channel_conv$')],
        states={GET_CHANNEL_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_channel_id)]},
        fallbacks=[CommandHandler('cancel', cancel)],
        allow_reentry=False
    )
    application.add_handler(link_conv_handler)

    # ChatMember updates handler - needs bot to be allowed to receive these updates (bot must be admin)
    application.add_handler(ChatMemberHandler(handle_chat_member_update, chat_member_types=ChatMemberHandler.CHAT_MEMBER))

    logging.info("बॉट शुरू हो रहा है...")
    application.run_polling(poll_interval=2)

if __name__ == '__main__':
    main()
