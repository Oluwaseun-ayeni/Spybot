from telegram import Bot, Update, Chat, MessageOriginChat
from telegram.constants import ChatType
import asyncio
import os
from dotenv import load_dotenv
import logging
import hashlib
from datetime import datetime, timedelta
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telegram.ext import ApplicationBuilder, CommandHandler, ConversationHandler, MessageHandler, filters
from sqlalchemy import create_engine, Column, Integer, String, JSON
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, scoped_session
from cryptography.fernet import Fernet

# Load environment variables
load_dotenv()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# Configure encryption and logging
KEY = Fernet.generate_key()
cipher = Fernet(KEY)
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Database setup
Base = declarative_base()
engine = create_engine('sqlite:///users.db')
Session = scoped_session(sessionmaker(bind=engine))

class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    telegram_id = Column(Integer, unique=True)
    encrypted_session = Column(String)
    monitored_groups = Column(JSON, default=[])

Base.metadata.create_all(engine)

class MessageTracker:
    def __init__(self):
        self.message_store = {}
        self.cooldown = timedelta(minutes=30)

    def add_message(self, text: str, chat_id: int, user_id: int):
        # Normalize text before hashing
        clean_text = " ".join(text.strip().lower().split())
        message_hash = hashlib.sha256(clean_text.encode()).hexdigest()
        
        if message_hash not in self.message_store:
            self.message_store[message_hash] = {
                'chats': set(),
                'last_notified': None
            }
        
        self.message_store[message_hash]['chats'].add(chat_id)
        return self.check_duplicates(message_hash, user_id)

    def check_duplicates(self, message_hash: str, user_id: int):
        record = self.message_store.get(message_hash)
        if not record or len(record['chats']) < 2:
            return None

        if record['last_notified'] and (datetime.now() - record['last_notified']) < self.cooldown:
            return None

        record['last_notified'] = datetime.now()
        return list(record['chats'])

tracker = MessageTracker()

async def start_bot(update: Update, context):
    await update.message.reply_text(
        "🔐 Welcome to Cross-Group Monitor!\n\n"
        "1. Use /register to connect your account\n"
        "2. Use /selectgroups to choose groups to monitor\n"
        "3. Use /startmonitor to begin watching"
    )

async def register_user(update: Update, context):
    user_id = update.effective_user.id
    session = Session()
    
    if session.query(User).filter_by(telegram_id=user_id).first():
        await update.message.reply_text("ℹ️ You're already registered!")
        session.close()
        return

    await update.message.reply_text(
        "📝 Send your Telegram credentials as:\n"
        "/auth API_ID API_HASH PHONE_NUMBER\n"
        "Get API credentials from https://my.telegram.org"
    )
    session.close()

async def handle_auth(update: Update, context):
    try:
        _, api_id, api_hash, phone = update.message.text.split()
        api_id = int(api_id)
        user_id = update.effective_user.id
    except ValueError:
        await update.message.reply_text("❌ Invalid format. Use: /auth API_ID API_HASH PHONE")
        return

    session = Session()
    try:
        client = TelegramClient(StringSession(), api_id, api_hash)
        await client.start(phone=phone)
        session_string = cipher.encrypt(client.session.save().encode())
        
        if session.query(User).filter_by(telegram_id=user_id).first():
            await update.message.reply_text("ℹ️ You're already registered!")
            return

        session.add(User(
            telegram_id=user_id,
            encrypted_session=session_string.decode(),
            monitored_groups=[]
        ))
        session.commit()
        await update.message.reply_text("✅ Registration successful!\nUse /selectgroups to choose groups")
    except Exception as e:
        session.rollback()
        await update.message.reply_text(f"⚠️ Registration failed: {str(e)}")
    finally:
        session.close()
        await client.disconnect()

async def select_groups(update: Update, context):
    user_id = update.effective_user.id
    session = Session()
    try:
        user = session.query(User).filter_by(telegram_id=user_id).first()
        if not user:
            await update.message.reply_text("❌ Please register first with /register")
            return ConversationHandler.END

        context.user_data['selecting'] = True
        await update.message.reply_text(
            "👥 Forward messages from groups to monitor:\n"
            "• Each forwarded message must be from a different group\n"
            "• Send /done when finished\n"
            "• Send /cancel to abort"
        )
        return "GROUP_SELECTION"
    finally:
        session.close()
        context.user_data.pop('selecting', None)
        return ConversationHandler.END

        
async def handle_group_forward(update: Update, context):
    user_id = update.effective_user.id
    session = Session()
    try:
        user = session.query(User).filter_by(telegram_id=user_id).first()
        if not user:
            await update.message.reply_text("❌ Please start with /start")
            return

        # Check if message is properly forwarded from a group
        if not update.message.forward_origin or not isinstance(update.message.forward_origin, MessageOriginChat):
            await update.message.reply_text(
                "⚠️ Please forward messages directly from group chats using Telegram's forward feature\n\n"
                "How to do it:\n"
                "1. Open the group\n"
                "2. Long-press a message\n"
                "3. Select 'Forward'\n"
                "4. Choose this bot as recipient"
            )
            return

        # Extract group info directly from forward origin
        origin = update.message.forward_origin
        chat_id = origin.chat.id
        chat_title = origin.sender_chat.title if origin.sender_chat else "Unknown Group"

        # Check for duplicates
        if chat_id in user.monitored_groups:
            await update.message.reply_text(f"ℹ️ Already monitoring {chat_title}")
            return

        # Add to monitored groups
        user.monitored_groups.append(chat_id)
        session.commit()
        await update.message.reply_text(f"✅ Added group: {chat_title} (ID: {chat_id})")

    except Exception as e:
        session.rollback()
        logger.error(f"Forward handling error: {str(e)}", exc_info=True)
        await update.message.reply_text("⚠️ Error processing this message")
    finally:
        session.close()
    return "GROUP_SELECTION"

async def finish_selection(update: Update, context):
    user_id = update.effective_user.id
    session = Session()
    try:
        user = session.query(User).filter_by(telegram_id=user_id).first()
        if not user:
            await update.message.reply_text("❌ Registration required")
            return ConversationHandler.END

        if not user.monitored_groups:
            await update.message.reply_text("⚠️ No groups selected")
            return ConversationHandler.END

        await update.message.reply_text(
            f"✅ Monitoring {len(user.monitored_groups)} groups\n"
            "Use /startmonitor to begin"
        )
        return ConversationHandler.END
    finally:
        context.user_data.pop('selecting', None)
        session.close()

async def cancel_selection(update: Update, context):
    context.user_data.pop('selecting', None)
    await update.message.reply_text("❌ Selection canceled")
    return ConversationHandler.END

async def start_monitoring(update: Update, context):
    user_id = update.effective_user.id
    session = Session()
    try:
        user = session.query(User).filter_by(telegram_id=user_id).first()
        if not user or not user.monitored_groups:
            await update.message.reply_text("❌ No groups selected. Use /selectgroups first")
            return

        asyncio.create_task(monitor_user_groups(user, BOT_TOKEN))
        await update.message.reply_text("🕵️ Monitoring started! You'll receive alerts here.")
    finally:
        session.close()

async def monitor_user_groups(user: User, bot_token: str):
    session_string = cipher.decrypt(user.encrypted_session.encode()).decode()
    client = TelegramClient(StringSession(session_string), 0, 0)
    
    try:
        await client.start()
        
        @client.on(events.NewMessage(chats=user.monitored_groups))
        async def message_handler(event):
            if not event.message.text:
                return

            try:
                chat = await event.get_chat()
                text = " ".join(event.message.text.strip().split()).lower()
                
                if duplicate_chats := tracker.add_message(text, chat.id, user.telegram_id):
                    bot = Bot(bot_token)
                    names = []
                    for cid in duplicate_chats:
                        entity = await client.get_entity(cid)
                        names.append(entity.title)
                    
                    await bot.send_message(
                        chat_id=user.telegram_id,
                        text=f"🚨 Message in {len(names)} groups:\n\n"
                             f"{event.message.text}\n\n"
                             f"Groups: {', '.join(names)}"
                    )
            except Exception as e:
                logger.error(f"Error handling message: {str(e)}")

        await client.run_until_disconnected()
    except Exception as e:
        logger.error(f"Monitoring failed for {user.telegram_id}: {str(e)}")
    finally:
        await client.disconnect()

if __name__ == '__main__':
    bot_app = ApplicationBuilder().token(BOT_TOKEN).build()
    
    # Conversation handler
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler('selectgroups', select_groups)],
        states={
            "GROUP_SELECTION": [
                MessageHandler(filters.FORWARDED & filters.ChatType.PRIVATE, handle_group_forward),
                CommandHandler('done', finish_selection),
                CommandHandler('cancel', cancel_selection)
            ]
        },
        fallbacks=[CommandHandler('cancel', cancel_selection)],
        allow_reentry=True
    )

    # Register handlers
    bot_app.add_handler(CommandHandler("start", start_bot))
    bot_app.add_handler(CommandHandler("register", register_user))
    bot_app.add_handler(CommandHandler("auth", handle_auth))
    bot_app.add_handler(CommandHandler("startmonitor", start_monitoring))
    bot_app.add_handler(conv_handler)

    # Start bot
    bot_app.run_polling()




# import asyncio
# import logging
# import random
# import sqlite3
# from datetime import datetime, timedelta
# from typing import Dict, Set, Optional
# from telegram import Update
# from dotenv import load_dotenv
# import os
# from telegram.ext import (
#     ApplicationBuilder,
#     CommandHandler,
#     MessageHandler,
#     filters,
#     ContextTypes,
# )

# load_dotenv()
# BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# # Configure logging
# logging.basicConfig(
#     format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
# )
# logger = logging.getLogger(__name__)

# # Database setup
# DB_NAME = "elite_monitor.db"
# NOTIFICATION_COOLDOWN = timedelta(minutes=30)

# class EliteDatabase:
#     """SQLite database manager with async support"""
    
#     def __init__(self):
#         self.conn = sqlite3.connect(DB_NAME)
#         self._init_db()
        
#     def _init_db(self):
#         with self.conn:
#             self.conn.execute("""
#                 CREATE TABLE IF NOT EXISTS subscriptions (
#                     user_id INTEGER,
#                     group_id INTEGER,
#                     PRIMARY KEY (user_id, group_id)
#                 )""")
#             self.conn.execute("""
#                 CREATE TABLE IF NOT EXISTS tracked_messages (
#                     user_id INTEGER,
#                     normalized_text TEXT,
#                     group_id INTEGER,
#                     timestamp DATETIME,
#                     last_notified DATETIME,
#                     PRIMARY KEY (user_id, normalized_text, group_id)
#                 )""")

#     async def execute(self, query: str, params: tuple = ()):
#         loop = asyncio.get_running_loop()
#         await loop.run_in_executor(None, lambda: self.conn.execute(query, params))
#         await loop.run_in_executor(None, self.conn.commit)
        
#     async def fetch(self, query: str, params: tuple = ()):
#         loop = asyncio.get_running_loop()
#         cursor = await loop.run_in_executor(
#             None, lambda: self.conn.execute(query, params)
#         )
#         return await loop.run_in_executor(None, cursor.fetchall)

# db = EliteDatabase()
# class NotificationManager:
#     """Handles alert logic with cooldowns and templating"""
    
#     TEMPLATES = [
#         "👀 Heads up! \"{}\" just popped up again!",
#         "🚨 Cross-group alert: \"{}\" detected!",
#         "🤖 AI Warning: \"{}\" is trending!",
#         "🌍 Global notice: \"{}\" spotted in {} groups!",
#         "🔔 Personalized alert: \"{}\" reappeared!"
#     ]
    
#     @classmethod
#     def random_template(cls, text: str, count: int) -> str:
#         template = random.choice(cls.TEMPLATES)
#         if "{}" in template:
#             return template.format(text)
#         return template  # For templates without placeholders

# async def monitor_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     """Enhanced monitoring with database persistence"""
#     user_id = update.effective_user.id
#     chat = update.effective_chat
    
#     if chat.type not in ("group", "supergroup"):
#         await update.message.reply_text("❗ Use /monitor in group chats only.")
#         return
    
#     try:
#         await db.execute(
#             "INSERT INTO subscriptions VALUES (?, ?)",
#             (user_id, chat.id)
#         )
#         await update.message.reply_text(
#             f"✅ Monitoring activated for {chat.title}!\n"
#             f"You'll receive cross-group alerts here."
#         )
#     except sqlite3.IntegrityError:
#         await update.message.reply_text("ℹ️ Already monitoring this group.")

# async def unmonitor_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     """Advanced unsubscribe with database cleanup"""
#     user_id = update.effective_user.id
#     chat = update.effective_chat
    
#     if chat.type not in ("group", "supergroup"):
#         await update.message.reply_text("❗ Use /unmonitor in group chats only.")
#         return
    
#     await db.execute(
#         "DELETE FROM subscriptions WHERE user_id = ? AND group_id = ?",
#         (user_id, chat.id)
#     )
    
#     if self.conn.total_changes > 0:
#         await update.message.reply_text("🛑 Monitoring stopped for this group.")
#         # Cleanup tracked messages
#         await db.execute(
#             """DELETE FROM tracked_messages 
#             WHERE user_id = ? AND group_id = ?""",
#             (user_id, chat.id)
#         )
#     else:
#         await update.message.reply_text("ℹ️ Not currently monitoring this group.")

# async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     """Elite message processing with cooldown management"""
#     chat = update.effective_chat
#     text = update.message.text.strip().lower()
    
#     if not text or chat.type not in ("group", "supergroup"):
#         return
    
#     # Get all users monitoring this group
#     subscribers = await db.fetch(
#         "SELECT user_id FROM subscriptions WHERE group_id = ?",
#         (chat.id,)
#     )
    
#     for (user_id,) in subscribers:
#         # Track message with expiration (24h TTL)
#         await db.execute(
#             """INSERT OR IGNORE INTO tracked_messages 
#             VALUES (?, ?, ?, ?, ?)""",
#             (user_id, text, chat.id, datetime.now(), None)
#         )
        
#         # Check for cross-group duplicates
#         result = await db.fetch(
#             """SELECT COUNT(DISTINCT group_id) 
#             FROM tracked_messages 
#             WHERE user_id = ? AND normalized_text = ? 
#             AND timestamp > datetime('now', '-1 day')""",
#             (user_id, text)
#         )
        
#         group_count = result[0][0] if result else 0
        
#         if group_count >= 2:
#             # Check notification cooldown
#             last_alert = await db.fetch(
#                 """SELECT last_notified 
#                 FROM tracked_messages 
#                 WHERE user_id = ? AND normalized_text = ? 
#                 ORDER BY timestamp DESC LIMIT 1""",
#                 (user_id, text)
#             )
            
#             if last_alert and last_alert[0][0]:
#                 last_time = datetime.fromisoformat(last_alert[0][0])
#                 if datetime.now() - last_time < NOTIFICATION_COOLDOWN:
#                     continue
            
#             # Send notification
#             try:
#                 alert = NotificationManager.random_template(
#                     update.message.text, group_count
#                 )
#                 await context.bot.send_message(
#                     chat_id=user_id,
#                     text=alert,
#                     parse_mode="Markdown"
#                 )
#                 # Update last notified time
#                 await db.execute(
#                     """UPDATE tracked_messages 
#                     SET last_notified = ? 
#                     WHERE user_id = ? AND normalized_text = ?""",
#                     (datetime.now(), user_id, text)
#                 )
#             except Exception as e:
#                 logger.error(f"Failed to notify {user_id}: {e}")
#                 # Remove problematic subscriptions
#                 await db.execute(
#                     "DELETE FROM subscriptions WHERE user_id = ?",
#                     (user_id,)
#                 )

# async def cleanup_task(context: ContextTypes.DEFAULT_TYPE):
#     """Daily maintenance: purge old messages and inactive users"""
#     await db.execute(
#         "DELETE FROM tracked_messages WHERE timestamp < datetime('now', '-1 day')"
#     )
#     logger.info("Performed daily database cleanup")

# async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     """Send welcome message and basic instructions"""
#     user = update.effective_user
#     await update.message.reply_text(
#         f"👋 Hello {user.mention_markdown()}!\n\n"
#         "🔍 I'm the Cross-Group Monitor Bot!\n\n"
#         "📌 How to use:\n"
#         "1. In any group chat: /monitor - enable tracking\n"
#         "2. I'll alert you when same messages appear in different groups\n"
#         "3. Use /unmonitor in a group to stop tracking\n\n"
#         "⚠️ Note: I need to be added as group admin to monitor messages",
#         parse_mode="Markdown"
#     )

# async def monitor_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     """Enhanced monitoring with database persistence"""
#     user_id = update.effective_user.id
#     chat = update.effective_chat
    
#     if chat.type not in ("group", "supergroup"):
#         await update.message.reply_text("❗ Use /monitor in group chats only.")
#         return
    
#     try:
#         await db.execute(
#             "INSERT INTO subscriptions VALUES (?, ?)",
#             (user_id, chat.id)
#         )
#         await update.message.reply_text(
#             f"✅ Monitoring activated for {chat.title}!\n"
#             f"You'll receive cross-group alerts here."
#         )
#     except sqlite3.IntegrityError:
#         await update.message.reply_text("ℹ️ Already monitoring this group.")

# async def unmonitor_group(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     """Advanced unsubscribe with database cleanup"""
#     user_id = update.effective_user.id
#     chat = update.effective_chat
    
#     if chat.type not in ("group", "supergroup"):
#         await update.message.reply_text("❗ Use /unmonitor in group chats only.")
#         return
    
#     await db.execute(
#         "DELETE FROM subscriptions WHERE user_id = ? AND group_id = ?",
#         (user_id, chat.id)
#     )    
    
#     if db.conn.total_changes > 0:  # Fixed self.conn to db.conn
#         await update.message.reply_text("🛑 Monitoring stopped for this group.")
#         await db.execute(
#             """DELETE FROM tracked_messages 
#             WHERE user_id = ? AND group_id = ?""",
#             (user_id, chat.id))
#     else:
#         await update.message.reply_text("ℹ️ Not currently monitoring this group.")



# def main():
#     app = ApplicationBuilder().token(BOT_TOKEN).build()
    
#     # Command handlers
#     app.add_handler(CommandHandler("start", start))  # Now correctly references the top-level function
#     app.add_handler(CommandHandler("monitor", monitor_group))
#     app.add_handler(CommandHandler("unmonitor", unmonitor_group))
    

    
#     # Message processing
#     app.add_handler(MessageHandler(
#         filters.TEXT & filters.ChatType.GROUPS, handle_message)
#     )
    
#     # Scheduled tasks
#     job_queue = app.job_queue
#     if job_queue:
#         job_queue.run_repeating(cleanup_task, interval=86400, first=10)
    
#     # Error handling
#     app.add_error_handler(error_handler)
    
#     logger.info("Elite Monitor Bot is now running")
#     app.run_polling()

# async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
#     """Sophisticated error management"""
#     logger.error(msg="Exception while handling update:", exc_info=context.error)
    
#     if update and update.effective_message:
#         await update.effective_message.reply_text(
#             "⚠️ Elite system encountered an anomaly. Engineers notified."
#         )

# if __name__ == "__main__":
#     main()
