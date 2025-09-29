import os
import logging
import uuid
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ChatMemberHandler

# --- 1. SETUP ---

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
POLL_IMAGE_URL = os.getenv("POLL_IMAGE_URL", None) 
# Agar aap chahein to POLL_IMAGE_FILE_ID bhi use kar sakte hain
# POLL_IMAGE_FILE_ID = os.getenv("POLL_IMAGE_FILE_ID", None)

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Mock Database (ADVANCED LOGIC KE LIYE ZAROORI)
MOCK_DB_VOTES = {} 
MOCK_DB_CHANNELS = {} 
MOCK_DB_POLLS = {} # Added 'is_active': True/False for stop voting

# --- 1.1 HELPER FUNCTIONS ---

def get_poll_results(poll_id: str) -> dict:
    """Calculates vote counts for a specific poll."""
    votes_data = MOCK_DB_VOTES.get(poll_id, {})
    results = {}
    poll_info = MOCK_DB_POLLS.get(poll_id)
    if not poll_info:
        return {}
    
    for option in poll_info['vote_options']:
        results[option] = 0
        
    for user_id, vote_option in votes_data.items():
        if vote_option in results:
            results[vote_option] += 1
            
    return results

async def update_poll_message(context, poll_id: str) -> None:
    """Edits the existing poll message with updated vote counts and dynamic content."""
    poll_info = MOCK_DB_POLLS.get(poll_id)
    if not poll_info:
        logger.warning(f"Poll {poll_id} not found for update.")
        return

    results = get_poll_results(poll_id)
    total_votes = sum(results.values())
    is_active = poll_info.get('is_active', True)
    
    # Get creator details (Fallback for errors)
    try:
        creator = await context.bot.get_chat(poll_info['creator_id'])
    except Exception:
        creator = type('CreatorMock', (object,), {'full_name': 'Unknown User', 'id': poll_info['creator_id'], 'username': 'N/A'})
    
    # ⚡ CAPTION taiyar karna (Image jaisa format) ⚡
    status_text = ""
    if not is_active:
        status_text = "🔒 **VOTING HAS ENDED!**\n"
        
    caption_template = (
        f"{status_text}**VOTE BOT**\n\n"
        "[*⚡*] **PARTICIPANT DETAILS** [*⚡*]\n"
        "► USER: `{full_name}`\n"
        "► USER-ID: `{user_id}`\n"
        "► USERNAME: @{username}\n\n"
        "**NOTE: ONLY CHANNEL SUBSCRIBERS CAN VOTE.**\n"
        "© CREATED BY USING @{bot_username}"
    )

    creator_username = creator.username if hasattr(creator, 'username') and creator.username else "N/A"
    creator_full_name = creator.full_name if hasattr(creator, 'full_name') else "Unknown User"
    
    caption = caption_template.format(
        full_name=creator_full_name,
        user_id=creator.id,
        username=creator_username,
        bot_username=context.bot.username
    )
    
    # Current Vote Count
    vote_count_text = f"\n\n**{total_votes}** ⚡"
    
    # Recreate the keyboard 
    keyboard = []
    
    if is_active:
        # Voting buttons
        for option in poll_info['vote_options']:
            callback_data = f'vote_{poll_id}_{option}' 
            count_display = results.get(option, 0)
            keyboard.append([InlineKeyboardButton(f"{option} ({count_display} Votes)", callback_data=callback_data)])
        
        # ADVANCED FEATURE: Creator's Stop Voting Button
        # Button data format: stopvote_POLLID
        keyboard.append([InlineKeyboardButton("🛑 Stop Voting (Only Creator)", callback_data=f'stopvote_{poll_id}')])
    else:
        # Results buttons after voting stops
        for option in poll_info['vote_options']:
            count_display = results.get(option, 0)
            keyboard.append([InlineKeyboardButton(f"✅ {option}: {count_display} Votes", callback_data='ignore')])
        
    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        # Edit the message caption (Photo caption ko edit karna)
        await context.bot.edit_message_caption(
            chat_id=poll_info['channel_id'],
            message_id=poll_info['message_id'],
            caption=caption + vote_count_text, 
            parse_mode='Markdown',
            reply_markup=reply_markup
        )
    except Exception as e:
        logger.error(f"Failed to edit message {poll_info['message_id']} in {poll_info['channel_id']}: {e}")

# --- 2. COMMAND HANDLERS ---

async def start_command(update: Update, context) -> None:
    """Handles /start command, including deep-linking for post creation."""
    user_id = update.effective_user.id
    payload = context.args[0] if context.args else None

    # ADVANCED LOGIC: Deep-link check for /start create_poll_CHANNELID
    if payload and payload.startswith('create_poll_'):
        try:
            channel_id = int(payload.split('_')[2])
            if channel_id in MOCK_DB_CHANNELS.get(user_id, []):
                await create_poll_message(update, context, channel_id)
                return
            else:
                await update.message.reply_text(
                    "🚨 Aapne yeh channel abhi tak connect nahi kiya hai ya main admin nahi hoon. "
                    "Pehle '🔗 Connect Your Channel' button par click karein."
                )
        except (ValueError, IndexError):
            pass 
    
    # Default Welcome Message 
    welcome_text = (
        "⚡ *Welcome to the Advanced Vote Bot!* ⚡\n\n"
        "Yahan aap apne channel ke liye *subscriber-only* polls bana sakte hain. "
        "Sirf channel members hi vote kar payenge, aur channel chhodne par unka vote hat jayega."
    )
    
    keyboard = [
        [
            InlineKeyboardButton("➕ Add Me To Your Group/Channel", url=f"https://t.me/{context.bot.username}?startgroup=add_bot")
        ],
        [
            InlineKeyboardButton("🔗 Connect Your Channel", callback_data='connect_channel')
        ],
        [
            InlineKeyboardButton("💡 How It Works", callback_data='help')
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if POLL_IMAGE_URL:
         await update.message.reply_photo(
             photo=POLL_IMAGE_URL, 
             caption=welcome_text, 
             parse_mode='Markdown', 
             reply_markup=reply_markup
         )
    else:
         await update.message.reply_text(
             welcome_text,
             parse_mode='Markdown',
             reply_markup=reply_markup
         )

# --- 3. CALLBACK HANDLERS (Button Clicks) ---

async def button_handler(update: Update, context) -> None:
    """Handles all inline button clicks."""
    query = update.callback_query
    user_id = query.from_user.id
    data = query.data
    
    await query.answer() # Notification/loading indicator clear karna

    if data == 'connect_channel':
        await query.edit_message_text(
            "Apne channel ka username ya ID bhejo jismein aap mujhe (bot ko) *Admin* bana chuke hain "
            "(*Post/Edit/Delete messages* aur *Invite via Link* permissions ke saath)."
        )
        context.user_data['waiting_for_channel'] = True

    elif data.startswith('vote_'):
        # ADVANCED LOGIC: Subscriber-Only Voting
        parts = data.split('_', 2)
        poll_id = parts[1]
        vote_option = parts[2]
        poll_info = MOCK_DB_POLLS.get(poll_id)
        
        if not poll_info or not poll_info.get('is_active', True):
             await query.answer("🚨 Voting is closed for this poll!", show_alert=True)
             return
            
        channel_id = poll_info['channel_id']
        
        # 1. Subscriber Check (API CALL)
        member = await context.bot.get_chat_member(chat_id=channel_id, user_id=user_id)
        if member.status not in ('member', 'administrator', 'creator'):
            await query.answer("🚨 Vote sirf channel members ke liye hai!", show_alert=True)
            return

        # 2. Update Vote
        MOCK_DB_VOTES.setdefault(poll_id, {})
        MOCK_DB_VOTES[poll_id][user_id] = vote_option
        
        # 3. Update the Poll Message
        await update_poll_message(context, poll_id)
        await query.answer(f"Aapka vote '{vote_option}' darj kiya gaya.")
    
    elif data.startswith('stopvote_'):
        # ADVANCED FEATURE: Stop Voting logic
        poll_id = data.split('_')[1]
        poll_info = MOCK_DB_POLLS.get(poll_id)
        
        if not poll_info:
            await query.answer("Poll not found.", show_alert=True)
            return

        # Authorization check: Only creator can stop voting
        if poll_info['creator_id'] != user_id:
            await query.answer("❌ Aap voting nahi rok sakte. Sirf poll creator hi rok sakta hai.", show_alert=True)
            return
        
        # Stop voting and update poll status
        MOCK_DB_POLLS[poll_id]['is_active'] = False
        await update_poll_message(context, poll_id)
        
        await query.answer("Voting successfully rok di gayi hai! Poll band ho chuka hai.", show_alert=True)
        
    elif data == 'ignore':
        await query.answer("Voting band hai, yeh sirf result button hai.")

# --- 4. MESSAGE HANDLERS ---

async def channel_setup_handler(update: Update, context) -> None:
    """Handles channel ID/Username input during connection process and generates deep-link."""
    if context.user_data.get('waiting_for_channel'):
        channel_input = update.message.text.strip()
        user_id = update.effective_user.id
        
        try:
            chat = await context.bot.get_chat(channel_input)
            channel_id = chat.id
            
            # Bot admin status check
            bot_member = await context.bot.get_chat_member(channel_id, context.bot.id)
            if not bot_member.can_post_messages or not bot_member.can_edit_messages:
                 await update.message.reply_text("❌ Error: Bot ko zaroori Admin permissions nahi mili hain (Post/Edit/Delete).")
                 return

            # DB mein save
            if channel_id not in MOCK_DB_CHANNELS.get(user_id, []):
                MOCK_DB_CHANNELS.setdefault(user_id, []).append(channel_id)
            context.user_data['waiting_for_channel'] = False
            
            # Deep-link generation
            create_link = f"https://t.me/{context.bot.username}?start=create_poll_{channel_id}"

            await update.message.reply_text(
                "✅ Channel successfully **connect** ho gaya!\n\n"
                "**Post banane ke liye link**:\n"
                f"`{create_link}`\n\n"
                "Is link par click karte hi poll post channel mein chala jayega."
            )
            
        except Exception as e:
            logger.error(f"Channel setup error: {e}")
            await update.message.reply_text("❌ Error: Channel mila nahi ya main admin nahi hoon. Sahi username/ID bhejein.")


async def create_poll_message(update: Update, context, channel_id: int) -> None:
    """Creates and sends the poll post to the connected channel with the image-like format."""
    
    poll_id = str(uuid.uuid4())
    vote_options = ["I Support", "I Oppose", "Neutral"]
    creator = update.effective_user
    
    # ⚡ CAPTION taiyar karna (Dynamic details)
    caption_template = (
        "**VOTE BOT**\n\n"
        "[*⚡*] **PARTICIPANT DETAILS** [*⚡*]\n"
        "► USER: `{full_name}`\n"
        "► USER-ID: `{user_id}`\n"
        "► USERNAME: @{username}\n\n"
        "**NOTE: ONLY CHANNEL SUBSCRIBERS CAN VOTE.**\n"
        "© CREATED BY USING @{bot_username}"
    )

    creator_username = creator.username if creator.username else "N/A"
    caption = caption_template.format(
        full_name=creator.full_name,
        user_id=creator.id,
        username=creator_username,
        bot_username=context.bot.username
    )
    
    # Initial Keyboard (0 Votes + Stop Voting button)
    keyboard = []
    for option in vote_options:
        callback_data = f'vote_{poll_id}_{option}' 
        keyboard.append([InlineKeyboardButton(f"{option} (0 Votes)", callback_data=callback_data)])
        
    # ADVANCED FEATURE: Stop Voting Button for Creator
    keyboard.append([InlineKeyboardButton("🛑 Stop Voting (Only Creator)", callback_data=f'stopvote_{poll_id}')])

    reply_markup = InlineKeyboardMarkup(keyboard)

    try:
        # Send the message with Image/Text
        if POLL_IMAGE_URL:
            sent_message = await context.bot.send_photo(
                chat_id=channel_id,
                photo=POLL_IMAGE_URL,
                caption=caption + "\n\n**0** ⚡", 
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
        else:
             sent_message = await context.bot.send_message(
                chat_id=channel_id,
                text=caption + "\n\n**0** ⚡", 
                parse_mode='Markdown',
                reply_markup=reply_markup
            )
        
        # 5. Save Poll Data to DB (MOCK)
        MOCK_DB_POLLS[poll_id] = {
            'channel_id': channel_id,
            'message_id': sent_message.message_id,
            'vote_options': vote_options,
            'creator_id': creator.id,
            'is_active': True # New status field
        }
        
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="✅ Poll successfully channel mein publish ho gaya hai!"
        )

    except Exception as e:
        logger.error(f"Failed to post poll: {e}")
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"❌ Poll publish karte samay error hua. Ensure the Bot is an Admin with Post/Edit permissions."
        )


# --- 5. ADVANCED FEATURE: VOTE REVOCATION ON LEAVE ---

async def handle_chat_members_update(update: Update, context) -> None:
    """Monitors channel leave events and revokes votes."""
    result = update.chat_member
    
    if result.old_chat_member.status in ('member', 'administrator', 'creator') and \
       result.new_chat_member.status in ('left', 'banned'):
        
        user_id = result.from_user.id
        channel_id = result.chat.id
        
        logger.info(f"User {user_id} left channel {channel_id}. Revoking votes...")
        
        for poll_id, poll_info in list(MOCK_DB_POLLS.items()):
            if poll_info['channel_id'] == channel_id:
                votes = MOCK_DB_VOTES.get(poll_id, {})
                
                if user_id in votes:
                    del votes[user_id]
                    logger.info(f"Revoked vote for user {user_id} in poll {poll_id}.")
                    
                    # Update the poll message in the channel
                    await update_poll_message(context, poll_id)


# --- 6. MAIN FUNCTION ---

def main() -> None:
    """Start the bot using polling mode (V20 standard)."""
    
    # 🚨 FIX: Application.builder() is the correct V20 structure. 
    # Purane 'updater' method se bachein.
    application = Application.builder().token(BOT_TOKEN).build()

    # Handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CallbackQueryHandler(button_handler))
    application.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), channel_setup_handler))
    
    # Zaroori: Advanced feature for vote revocation
    application.add_handler(
        ChatMemberHandler(handle_chat_members_update, ChatMemberHandler.CHATMEMBER)
    )

    # Polling mode: Render/V20 ke liye PORT ki zaroorat nahi.
    application.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    # 🚨 FIX: Agar aapka purana code main function ko '_main' se chala raha tha, 
    # toh ise '__main__' karein
    main()
