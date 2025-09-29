import os
import uuid
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters

# Load ENV
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
POLL_IMAGE_URL = os.getenv("POLL_IMAGE_URL", None)

# Simple in-memory DB
CHANNELS_DB = {}  # {user_id: [channel_ids]}
POLLS_DB = {}     # {poll_id: {channel_id, message_id, creator_id, is_active, votes}}

# --- HELPER ---
async def update_poll_message(context, poll_id: str):
    poll = POLLS_DB.get(poll_id)
    if not poll: return
    votes = poll['votes']
    total_votes = sum(votes.values())
    # Button text
    keyboard = [
        [InlineKeyboardButton(f"Vote ({votes.get('yes',0)} Yes / {votes.get('no',0)} No)", callback_data=f"vote_{poll_id}") ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    try:
        await context.bot.edit_message_reply_markup(
            chat_id=poll['channel_id'],
            message_id=poll['message_id'],
            reply_markup=reply_markup
        )
    except: pass

# --- COMMAND ---
async def start_command(update: Update, context):
    user_id = update.effective_user.id
    payload = context.args[0] if context.args else None

    # Deep link for creating poll
    if payload and payload.startswith("create_poll_"):
        channel_id = int(payload.split("_")[2])
        poll_id = str(uuid.uuid4())
        POLLS_DB[poll_id] = {
            "channel_id": channel_id,
            "message_id": None,
            "creator_id": user_id,
            "is_active": True,
            "votes": {}
        }
        keyboard = [
            [InlineKeyboardButton("Vote ✅", callback_data=f"vote_{poll_id}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        text = f"⚡ Poll created by {update.effective_user.full_name}"
        if POLL_IMAGE_URL:
            msg = await context.bot.send_photo(
                chat_id=channel_id,
                photo=POLL_IMAGE_URL,
                caption=text,
                reply_markup=reply_markup
            )
        else:
            msg = await context.bot.send_message(
                chat_id=channel_id,
                text=text,
                reply_markup=reply_markup
            )
        POLLS_DB[poll_id]['message_id'] = msg.message_id
        await update.message.reply_text("✅ Poll posted successfully!")
        return

    # Default welcome
    welcome_text = (
        "⚡ Welcome to the Simple Vote Bot!\n"
        "➕ Add your channel and create subscriber-only polls."
    )
    keyboard = [
        [InlineKeyboardButton("➕ Add Your Channel", callback_data="connect_channel")],
        [InlineKeyboardButton("💡 How It Works", callback_data="help")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    if POLL_IMAGE_URL:
        await update.message.reply_photo(POLL_IMAGE_URL, caption=welcome_text, reply_markup=reply_markup)
    else:
        await update.message.reply_text(welcome_text, reply_markup=reply_markup)

# --- CALLBACK ---
async def button_handler(update: Update, context):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data == "connect_channel":
        await query.edit_message_text("Send your channel ID or username where the bot is admin.")
        context.user_data['waiting_channel'] = True

    elif data.startswith("vote_"):
        poll_id = data.split("_")[1]
        poll = POLLS_DB.get(poll_id)
        if not poll or not poll['is_active']:
            await query.answer("Voting closed!", show_alert=True)
            return
        # Check subscriber
        member = await context.bot.get_chat_member(poll['channel_id'], user_id)
        if member.status not in ('member','creator','administrator'):
            await query.answer("Only channel subscribers can vote!", show_alert=True)
            return
        # Count votes simple yes/no
        poll['votes']['yes'] = poll['votes'].get('yes',0)+1
        await update_poll_message(context, poll_id)
        await query.answer("Your vote counted!")

# --- MESSAGE HANDLER ---
async def channel_setup_handler(update: Update, context):
    if context.user_data.get('waiting_channel'):
        channel_input = update.message.text.strip()
        user_id = update.effective_user.id
        try:
            chat = await context.bot.get_chat(channel_input)
            # Bot must be admin
            bot_member = await context.bot.get_chat_member(chat.id, context.bot.id)
            if not bot_member.can_post_messages:
                await update.message.reply_text("Bot needs Post/Edit permissions.")
                return
            CHANNELS_DB.setdefault(user_id,[]).append(chat.id)
            context.user_data['waiting_channel'] = False
            create_link = f"https://t.me/{context.bot.username}?start=create_poll_{chat.id}"
            await update.message.reply_text(f"✅ Channel connected!\nClick to create poll:\n{create_link}")
        except Exception as e:
            await update.message.reply_text("❌ Invalid channel or bot not admin.")

# --- MAIN ---
def main():
    application = Application.builder().token(BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), channel_setup_handler))
    application.run_polling()

if __name__ == "__main__":
    main()
