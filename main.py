# bot.py
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
from telegram.constants import ChatMemberStatus, ParseMode
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

# ----------------------------
# Load env & logging
# ----------------------------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
IMAGE_URL = os.getenv("IMAGE_URL", "https://picsum.photos/900/400")
LOG_CHANNEL_USERNAME = os.getenv("LOG_CHANNEL_USERNAME", None)  # optional

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

# ----------------------------
# Conversation states
# ----------------------------
(GET_CHANNEL_ID,) = range(1)

# ----------------------------
# Storage (in-memory)
# ----------------------------
# VOTE_POSTS keyed by (channel_id, message_id) -> {'count':int, 'voters': set(user_id), 'channel_url': str|None}
VOTE_POSTS = {}  # {(channel_id, message_id): {...}}

# CHANNEL_POSTS: channel_id -> set(message_id) to quickly find all posts of a channel
CHANNEL_POSTS = defaultdict(set)

# USER_VOTES: user_id -> set( (channel_id, message_id) ) for quick lookup (optional)
USER_VOTES = defaultdict(set)

# ----------------------------
# Helpers
# ----------------------------
def make_vote_callback(channel_id: int, message_id: int) -> str:
    # callback data: vote_<channel>_<message>
    return f"vote_{channel_id}_{message_id}"

def parse_vote_callback(data: str):
    # returns (channel_id:int, message_id:int) or None
    m = re.match(r'^vote_(-?\d+)_([0-9]+)$', data or "")
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))

def build_post_markup(channel_id: int, message_id: int, channel_url: str | None):
    count = VOTE_POSTS.get((channel_id, message_id), {}).get("count", 0)
    btn_text = f"✅ Vote Now ({count} Votes)"
    kb = [[InlineKeyboardButton(btn_text, callback_data=make_vote_callback(channel_id, message_id))]]
    if channel_url:
        kb.append([InlineKeyboardButton("➡️ Go to Channel", url=channel_url)])
    return InlineKeyboardMarkup(kb)

async def safe_edit_markup(bot, chat_id: int, message_id: int, markup: InlineKeyboardMarkup):
    try:
        await bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=markup)
    except Exception as e:
        logging.warning("edit_message_reply_markup failed for %s:%s -> %s", chat_id, message_id, e)

# ----------------------------
# Handlers
# ----------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    bot_user = await context.bot.get_me()
    bot_username = bot_user.username or "bot"

    # handle deep link (if any)
    if context.args:
        payload = context.args[0]
        m = re.match(r'link_(\d+)', payload)
        if m:
            clean = m.group(1)
            channel_id = int(f"-100{clean}")
            try:
                chat_info = await context.bot.get_chat(channel_id)
                channel_title = chat_info.title or "Channel"
                channel_url = chat_info.invite_link or (f"https://t.me/{chat_info.username}" if chat_info.username else None)

                # notify user in private
                if update.message:
                    await update.message.reply_text(
                        f"✨ You have joined *{channel_title}*! 🎉\n\n"
                        "आप सफलतापूर्वक चैनल जॉइन कर चुके हैं।",
                        parse_mode=ParseMode.MARKDOWN
                    )

                # create notification post in the channel and create vote button for *that specific post*
                notification = (
                    f"👑 *New Participant Joined!* 👑\n"
                    f"────────────────────────\n"
                    f"👤 Name: [{update.effective_user.first_name}](tg://user?id={update.effective_user.id})\n"
                    f"🆔 User ID: `{update.effective_user.id}`\n\n"
                    f"📣 Channel: *{channel_title}*\n"
                    f"🤖 Bot: @{bot_username}\n"
                    f"────────────────────────"
                )

                # 1) send photo+caption without markup to get message_id
                sent = await context.bot.send_photo(
                    chat_id=channel_id,
                    photo=IMAGE_URL,
                    caption=notification,
                    parse_mode=ParseMode.MARKDOWN
                )
                msg_id = sent.message_id

                # 2) create post entry for this (channel_id, msg_id)
                VOTE_POSTS[(channel_id, msg_id)] = {
                    "count": 0,
                    "voters": set(),
                    "channel_url": channel_url
                }
                CHANNEL_POSTS[channel_id].add(msg_id)

                # 3) build markup (uses the known message id) and attach
                markup = build_post_markup(channel_id, msg_id, channel_url)
                await safe_edit_markup(context.bot, chat_id=channel_id, message_id=msg_id, markup=markup)

                return
            except Exception as e:
                logging.exception("Deep link handling failed: %s", e)
                if update.message:
                    await update.message.reply_text("⚠️ चैनल नोटिफ़िकेशन भेजने में त्रुटि हुई। सुनिश्चित करें कि बॉट चैनल का एडमिन है।")

    # Normal start (menu)
    bot_username = (await context.bot.get_me()).username or "bot"
    keyboard = [
        [InlineKeyboardButton("🔗 लिंक पाएँ", callback_data='start_channel_conv'),
         InlineKeyboardButton("➕ Add to Group", url=f"https://t.me/{bot_username}?startgroup=true")],
        [InlineKeyboardButton("📊 My Votes", callback_data='my_polls_list'),
         InlineKeyboardButton("❓ Guide", url='https://t.me/teamrajweb')]
    ]
    welcome = (
        "*👑 वोट बॉट में आपका स्वागत है!* 👑\n\n"
        "चैनल कनेक्ट कर अपनी UNIQUE लिंक बनाएं और वोट संदेश भेजें।\n"
        "_\"एक वोट भी फर्क डाल सकता है\"_"
    )
    await context.bot.send_photo(chat_id=update.effective_chat.id, photo=IMAGE_URL, caption=welcome, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard))

async def start_channel_poll_conversation_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.message.reply_text("👋 कृपया उस चैनल का @username या ID भेजें (जैसे: `@mychannel` या `-1001234567890`) — नोट: बॉट को चैनल में एडमिन बनाना ज़रूरी है।", parse_mode=ParseMode.MARKDOWN)
    return GET_CHANNEL_ID

async def get_channel_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    channel_id_input = update.message.text.strip()
    # normalize
    if re.match(r'^-?\d+$', channel_id_input):  # numeric id
        channel_id = int(channel_id_input)
    else:
        channel_id = channel_id_input if channel_id_input.startswith('@') else f"@{channel_id_input}"

    try:
        bot_me = await context.bot.get_me()
        bot_id = bot_me.id

        # check bot is admin in channel
        chat_member = await context.bot.get_chat_member(chat_id=channel_id, user_id=bot_id)
        chat_info = await context.bot.get_chat(chat_id=channel_id)

        if getattr(chat_member, "status", "").lower() in ['administrator', 'creator']:
            raw = str(chat_info.id)
            clean = raw[4:] if raw.startswith('-100') else raw.replace('-', '')
            deep_payload = f"link_{clean}"
            share_url = f"https://t.me/{bot_me.username}?start={deep_payload}"
            await update.message.reply_text(f"✅ Channel *{chat_info.title}* connected!\n\nShare link:\n`{share_url}`", parse_mode=ParseMode.MARKDOWN)

            # optional log
            if LOG_CHANNEL_USERNAME:
                try:
                    await context.bot.send_message(chat_id=LOG_CHANNEL_USERNAME, text=f"🔗 New channel linked: {chat_info.title} — {share_url}")
                except Exception:
                    pass

            return ConversationHandler.END
        else:
            await update.message.reply_text("❌ Bot को चैनल का एडमिन बनाएं और फिर ID भेजें।")
            return GET_CHANNEL_ID

    except Exception as e:
        logging.exception("get_channel_id error: %s", e)
        await update.message.reply_text("⚠️ चैनल एक्सेस में त्रुटि। ID/username और bot permissions जाँचें।")
        return GET_CHANNEL_ID

# ----------------------------
# Voting: callback handler
# ----------------------------
async def handle_vote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    data = query.data
    parsed = parse_vote_callback(data)
    if not parsed:
        await query.answer("Invalid vote data.", show_alert=True)
        return
    channel_id, message_id = parsed
    user_id = query.from_user.id

    post_key = (channel_id, message_id)
    post = VOTE_POSTS.get(post_key)
    if not post:
        await query.answer("यह पोस्ट उपलब्ध नहीं है या समाप्त हो चुकी है।", show_alert=True)
        return

    # if user already voted on this post
    if user_id in post["voters"]:
        await query.answer("🗳️ आप पहले ही इस पोस्ट पर वोट कर चुके हैं।", show_alert=True)
        return

    # check subscription/membership
    try:
        chat_member = await context.bot.get_chat_member(chat_id=channel_id, user_id=user_id)
        is_member = chat_member.status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR]
    except (Forbidden, BadRequest) as e:
        logging.error("membership check failed: %s", e)
        await query.answer("🚨 सदस्यता जाँच में समस्या। बॉट की permissions चेक करें।", show_alert=True)
        return
    except Exception as e:
        logging.exception("membership unknown error: %s", e)
        await query.answer("⚠️ त्रुटि हुई — पुनः प्रयास करें।", show_alert=True)
        return

    if not is_member:
        # prompt to join
        channel_url = post.get("channel_url")
        if channel_url:
            await query.answer("❌ वोट करने के लिए पहले चैनल जॉइन करें।", show_alert=True)
            # also try sending DM with channel link
            try:
                await context.bot.send_message(chat_id=user_id, text=f"कृपया चैनल जॉइन करें: {channel_url}")
            except Exception:
                pass
        else:
            await query.answer("❌ आप वोट नहीं कर सकते — चैनल जॉइन करें।", show_alert=True)
        return

    # register vote
    post["voters"].add(user_id)
    post["count"] += 1
    USER_VOTES[user_id].add(post_key)

    # acknowledge
    await query.answer(f"✅ आपका वोट दर्ज हो गया — कुल {post['count']} वोट्स!", show_alert=True)

    # update this post's button (show new count)
    new_markup = build_post_markup(channel_id, message_id, post.get("channel_url"))
    await safe_edit_markup(context.bot, chat_id=channel_id, message_id=message_id, markup=new_markup)

# ----------------------------
# ChatMember updates: when user leaves/kicked -> remove their votes for that channel's posts
# ----------------------------
async def handle_chat_member_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmu = update.chat_member  # ChatMemberUpdated
    chat = cmu.chat
    old = cmu.old_chat_member
    new = cmu.new_chat_member

    channel_id = chat.id
    target_user = new.user
    target_user_id = target_user.id

    old_status = getattr(old, "status", "").lower()
    new_status = getattr(new, "status", "").lower()

    # consider left/kicked as removal
    left_states = ['left', 'kicked']

    was_member = old_status in ['member', 'administrator', 'creator']
    is_now_left = new_status in left_states

    if was_member and is_now_left:
        # find posts in this channel where this user voted
        posts = list(CHANNEL_POSTS.get(channel_id, set()))
        removed_any = False
        for msg_id in posts:
            key = (channel_id, msg_id)
            post = VOTE_POSTS.get(key)
            if not post:
                continue
            if target_user_id in post["voters"]:
                post["voters"].remove(target_user_id)
                post["count"] = max(0, post["count"] - 1)
                removed_any = True

                # update USER_VOTES
                if key in USER_VOTES.get(target_user_id, set()):
                    USER_VOTES[target_user_id].discard(key)

                # update the post button to reflect new count
                new_markup = build_post_markup(channel_id, msg_id, post.get("channel_url"))
                await safe_edit_markup(context.bot, chat_id=channel_id, message_id=msg_id, markup=new_markup)

        if removed_any:
            logging.info("Removed votes of user %s after leaving channel %s", target_user_id, channel_id)

# ----------------------------
# Utility / Admin commands (optional)
# ----------------------------
async def my_votes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    votes = USER_VOTES.get(uid, set())
    if not votes:
        await update.message.reply_text("आपने अभी तक किसी पोस्ट पर वोट नहीं किया है।")
        return
    lines = []
    for (cid, mid) in votes:
        post = VOTE_POSTS.get((cid, mid))
        title = f"Channel {cid}"
        count = post["count"] if post else 0
        lines.append(f"Channel `{cid}` — Message `{mid}` — {count} votes")
    await update.message.reply_text("आपके वोट:\n" + "\n".join(lines), parse_mode=ParseMode.MARKDOWN)

# ----------------------------
# main()
# ----------------------------
def main():
    if not BOT_TOKEN:
        logging.error("BOT_TOKEN not set in environment.")
        return

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(start_channel_poll_conversation_cb, pattern=r'^start_channel_conv$'))

    conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_channel_poll_conversation_cb, pattern=r'^start_channel_conv$')],
        states={GET_CHANNEL_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_channel_id)]},
        fallbacks=[],
        allow_reentry=False
    )
    app.add_handler(conv)

    app.add_handler(CallbackQueryHandler(handle_vote, pattern=r'^vote_'))
    # ChatMember updates handler — bot must be admin in channel to receive these updates
    app.add_handler(ChatMemberHandler(handle_chat_member_update, ChatMemberHandler.CHAT_MEMBER))

    # optional: show where user voted
    app.add_handler(CommandHandler("myvotes", my_votes_cmd))

    logging.info("Bot starting...")
    app.run_polling(poll_interval=2)

if __name__ == "__main__":
    main()
