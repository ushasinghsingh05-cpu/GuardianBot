import html
import logging
import re
import time
import platform
import psutil
import requests
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from random import choice
from Edit import PM_START_TEXT, start_buttons, PM_START_IMG, IMG
from telegram import Update, Bot, InlineKeyboardMarkup, InlineKeyboardButton, ParseMode, ChatPermissions
from telegram.utils.helpers import escape_markdown, mention_markdown, mention_html
from telegram.ext import Updater, CommandHandler, MessageHandler, Filters, CallbackQueryHandler, CallbackContext
from telegram.error import RetryAfter, TimedOut, TelegramError, Unauthorized, BadRequest, NetworkError
from pymongo import MongoClient
from config import LOGGER, MONGO_URI, DB_NAME, TELEGRAM_TOKEN, OWNER_ID, SUDO_ID, BOT_NAME, SUPPORT_ID, SIGHTENGINE_USER, SIGHTENGINE_SECRET

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

StartTime = time.time()

# MongoDB initialization
users_collection = None
chats_collection = None
sudo_collection = None
group_settings_collection = None
sudo_users = SUDO_ID.copy()
if OWNER_ID and OWNER_ID not in sudo_users:
    sudo_users.append(OWNER_ID)

if MONGO_URI:
    try:
        client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        db = client[DB_NAME]
        users_collection = db['users']
        chats_collection = db['chats']
        sudo_collection = db['sudo_users']
        group_settings_collection = db['group_settings']
        
        # Load stored sudo users
        for s_user in sudo_collection.find({}):
            uid = s_user.get("user_id")
            if uid and uid not in sudo_users:
                sudo_users.append(uid)
        logger.info("Successfully connected to MongoDB and loaded sudo users.")
    except Exception as e:
        logger.warning(f"MongoDB connection failed: {e}. Using in-memory fallback.")

# In-memory fallbacks if MongoDB is down
in_memory_users = set()
in_memory_chats = set()
in_memory_group_settings = {}
in_memory_user_warnings = {}  # (chat_id, user_id) -> int

# Comprehensive Banwords List across Multiple Prohibited Categories (Expanded with Open Source Dictionary)
BAD_WORDS = [
    # Explicit / Profanity / Adult terms
    "18+", "sex", "porn", "nude", "blowjob", "boobs", "bobs", "condom", "xxx", "adult", "nangi", "randi", 
    "chutiya", "madarchod", "bhenchod", "gaand", "gand", "lund", "ch**d", "g***i", "harami", "kutte", "kutta",
    "gandu", "madharchod", "lundoo", "lodu", "bhains", "chod", "randa", "haramzada", "randi ka bacha",
    "bhosdiwala", "bhosdike", "mc", "mcchod", "randi ki aulaad", "gand mara", "lund mar", "lauda", "loda",
    "chodu", "chut", "chutiyapa", "chutiye", "chut ke", "chut ke laude", "chut ke bache", "bhosadike",
    "m**ch*d", "b**chod", "b***chod", "incest", "bestiality", "cum", "masturbate", "orgasm", "slut", "whore",
    "bitch", "bastard", "dick", "pussy", "cunt", "motherfucker", "cocksucker", "asshole", "douchebag",
    # Illegal Drugs & Substances
    "cocaine", "heroin", "meth", "mdma", "lsd", "fentanyl", "buy weed", "buy drugs", "illegal drugs",
    "ecstasy", "shrooms", "ketamine", "opium", "morphine", "codeine", "xanax bar", "sell weed",
    # Copyrighted Content / Piracy / Leaks
    "cracked apk", "netflix leak", "free netflix", "pirated movie", "torrent leak", "copyrighted leak",
    "free onlyfans", "onlyfans leak", "premium mod apk", "megalinks", "drive leak", "torrentz", "thepiratebay",
    # Violence & Terrorism
    "kill admin", "bomb group", "behead", "terrorist", "suicide", "gore video", "shootout", "massacre",
    "cut throat", "lynch", "homicide", "genocide"
]

patterns = []
for word in BAD_WORDS:
    escaped = re.escape(word)
    left = r'\b' if word[0].isalnum() else r'(?:^|\s)'
    right = r'\b' if word[-1].isalnum() else r'(?:$|\s|[.,!?])'
    patterns.append(f"{left}{escaped}{right}")

BAD_PATTERN = re.compile(r"|".join(patterns), re.IGNORECASE)

class HealthCheckHandler(BaseHTTPRequestHandler):
    """HTTP Server Handler for Hugging Face Spaces port 7860 health checks."""
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is alive and running flawlessly on Hugging Face Spaces!")

    def log_message(self, format, *args):
        pass  # Suppress HTTP logging to avoid cluttering Hugging Face logs

def run_health_server():
    """Runs a lightweight HTTP server on port 7860 for Hugging Face Spaces container requirements."""
    port = 7860
    try:
        server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
        logger.info(f"Hugging Face Spaces health check server listening on port {port}...")
        server.serve_forever()
    except Exception as e:
        logger.warning(f"Failed to start Hugging Face health check server: {e}")

def safe_api_call(func, *args, **kwargs):
    """Robust API call wrapper that handles Telegram rate limiting (FloodWait)."""
    max_retries = 3
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except RetryAfter as e:
            logger.warning(f"Flood wait! Sleeping for {e.retry_after} seconds...")
            time.sleep(e.retry_after)
        except (TimedOut, NetworkError) as e:
            logger.warning(f"Network error ({e})! Retrying in 2 seconds...")
            time.sleep(2)
        except (Unauthorized, BadRequest) as e:
            logger.error(f"API Error ({func.__name__}): {e}")
            return False
        except Exception as e:
            if "aborted" in str(e).lower() or "disconnected" in str(e).lower() or "reset" in str(e).lower() or "timeout" in str(e).lower():
                logger.warning(f"Connection dropped ({func.__name__}): {e}. Retrying in 2 seconds...")
                time.sleep(2)
                continue
            logger.error(f"Unexpected API error ({func.__name__}): {e}")
            return False
    return False

def track_interaction(chat_id, user_id):
    if users_collection is not None:
        try:
            users_collection.update_one({"user_id": user_id}, {"$set": {"user_id": user_id}}, upsert=True)
            chats_collection.update_one({"chat_id": chat_id}, {"$set": {"chat_id": chat_id}}, upsert=True)
        except Exception:
            pass
    else:
        in_memory_users.add(user_id)
        in_memory_chats.add(chat_id)

def get_readable_time(seconds: int) -> str:
    count = 0
    ping_time = ""
    time_list = []
    time_suffix_list = ["s", "m", "h", "days"]

    while count < 4:
        count += 1
        remainder, result = divmod(seconds, 60) if count < 3 else divmod(seconds, 24)
        if seconds == 0 and remainder == 0:
            break
        time_list.append(int(result))
        seconds = int(remainder)

    for x in range(len(time_list)):
        time_list[x] = str(time_list[x]) + time_suffix_list[x]
    if len(time_list) == 4:
        ping_time += time_list.pop() + ", "

    time_list.reverse()
    ping_time += ":".join(time_list)

    return ping_time

def is_admin(chat, user_id, bot):
    if user_id == OWNER_ID or user_id in sudo_users:
        return True
    try:
        member = bot.get_chat_member(chat.id, user_id)
        return member.status in ["creator", "administrator"]
    except Exception:
        return False

def get_group_setting(chat_id):
    if group_settings_collection is not None:
        try:
            doc = group_settings_collection.find_one({"chat_id": chat_id})
            if doc and "mode" in doc:
                return doc["mode"]
        except Exception:
            pass
    return in_memory_group_settings.get(chat_id, "warn_mute")

def set_group_setting(chat_id, mode):
    if group_settings_collection is not None:
        try:
            group_settings_collection.update_one({"chat_id": chat_id}, {"$set": {"chat_id": chat_id, "mode": mode}}, upsert=True)
        except Exception:
            pass
    in_memory_group_settings[chat_id] = mode

def get_user_warnings(chat_id, user_id):
    if group_settings_collection is not None:
        try:
            doc = group_settings_collection.find_one({"chat_id": chat_id})
            if doc and "warnings" in doc:
                return doc["warnings"].get(str(user_id), 0)
        except Exception:
            pass
    return in_memory_user_warnings.get((chat_id, user_id), 0)

def increment_user_warnings(chat_id, user_id):
    current = get_user_warnings(chat_id, user_id) + 1
    if group_settings_collection is not None:
        try:
            group_settings_collection.update_one({"chat_id": chat_id}, {"$set": {f"warnings.{user_id}": current}}, upsert=True)
        except Exception:
            pass
    in_memory_user_warnings[(chat_id, user_id)] = current
    return current

def start(update: Update, context: CallbackContext):
    user = update.effective_user
    chat = update.effective_chat
    track_interaction(chat.id, user.id)

    uptime = get_readable_time(int(time.time() - StartTime))
    buttons = start_buttons(context.bot.username)

    if chat.type == "private":
        safe_api_call(
            update.effective_message.reply_photo,
            photo=PM_START_IMG,
            caption=PM_START_TEXT.format(escape_markdown(user.first_name, version=1)),
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        safe_api_call(
            update.effective_message.reply_photo,
            photo=PM_START_IMG,
            caption=f"Bot is active and protecting your group.\nUptime: {uptime}",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

def help_command(update: Update, context: CallbackContext):
    user = update.effective_user
    chat = update.effective_chat
    track_interaction(chat.id, user.id)

    help_text = f"""
Help & Management Menu:

Group Admin Commands:
• /setmode <warn_mute | warn | silent> - Configure NSFW/banword violation action.
• /settings - View current group configuration.

Owner Commands:
• /addsudo <username/ID> - Add sudo user
• /delsudo <username/ID> - Remove sudo user
• /sudolist - Show all sudo users
• /status - Show system status (CPU, RAM, Uptime)
• /stats - Show bot statistics
• /broadcast <msg> - Broadcast message to all users/chats
• /announce - Forward message to all users/chats
• /clone <Bot Token> - Clone bot instance

Public Commands:
• /start - Check bot liveliness
• /help - Display this help menu
• /ping - Check ping latency
• /id - Get User, Replied, and Chat IDs

Security Bot Features:
• Advanced NSFW text & media detection (Images, Videos, GIFs, Stickers).
• Analyzes pre-compressed media thumbnails for optimal speed and zero CPU overhead.
• Automatically deletes edited messages and media captions to ensure transparency.
"""
    buttons = [[InlineKeyboardButton("Close", callback_data="close_help")]]
    safe_api_call(
        update.effective_message.reply_text,
        text=help_text,
        reply_markup=InlineKeyboardMarkup(buttons)
    )

def setmode_command(update: Update, context: CallbackContext):
    chat = update.effective_chat
    user = update.effective_user
    if chat.type == "private":
        safe_api_call(update.message.reply_text, "This command can only be used in groups.")
        return

    if not is_admin(chat, user.id, context.bot):
        safe_api_call(update.message.reply_text, "Only group administrators or owners can use this command.")
        return

    if not context.args or context.args[0].lower() not in ["warn_mute", "warn", "silent"]:
        safe_api_call(update.message.reply_text, "Usage: /setmode <warn_mute | warn | silent>\n\nModes:\n• warn_mute: Warn user and mute after 3 warnings.\n• warn: Only warn user without muting.\n• silent: Delete content silently without warning.")
        return

    mode = context.args[0].lower()
    set_group_setting(chat.id, mode)
    safe_api_call(update.message.reply_text, f"Successfully set group moderation mode to: {mode}")

def settings_command(update: Update, context: CallbackContext):
    chat = update.effective_chat
    user = update.effective_user
    if chat.type == "private":
        safe_api_call(update.message.reply_text, "This command can only be used in groups.")
        return

    if not is_admin(chat, user.id, context.bot):
        safe_api_call(update.message.reply_text, "Only group administrators or owners can use this command.")
        return

    mode = get_group_setting(chat.id)
    safe_api_call(update.message.reply_text, f"Group Settings:\n-------------------\nModeration Mode: {mode}\n-------------------\nTo change the mode, use /setmode <warn_mute | warn | silent>")

def ping_command(update: Update, context: CallbackContext):
    start_t = time.time()
    msg = safe_api_call(update.effective_message.reply_text, "Ping...")
    if msg:
        end_t = time.time()
        latency = int((end_t - start_t) * 1000)
        uptime = get_readable_time(int(time.time() - StartTime))
        safe_api_call(
            msg.edit_text,
            f"Pong: {latency} ms\nUptime: {uptime}\nBot: @{context.bot.username}"
        )

def status_command(update: Update, context: CallbackContext):
    user = update.effective_user
    if user.id != OWNER_ID:
        safe_api_call(update.message.reply_text, "You do not have permission to use this command.")
        return

    uptime = get_readable_time(int(time.time() - StartTime))
    cpu = psutil.cpu_percent()
    ram = psutil.virtual_memory().percent
    plat = f"{platform.system()} {platform.release()}"

    if users_collection is not None:
        users_count = users_collection.count_documents({})
        chats_count = chats_collection.count_documents({})
    else:
        users_count = len(in_memory_users)
        chats_count = len(in_memory_chats)

    status_text = (
        f"System Status:\n"
        f"-------------------\n"
        f"Uptime: {uptime}\n"
        f"CPU Usage: {cpu}%\n"
        f"RAM Usage: {ram}%\n"
        f"Platform: {plat}\n"
        f"Users: {users_count}\n"
        f"Chats: {chats_count}\n"
        f"-------------------"
    )
    buttons = [[InlineKeyboardButton("Close", callback_data="close_status")]]
    safe_api_call(
        update.message.reply_text,
        text=status_text,
        reply_markup=InlineKeyboardMarkup(buttons)
    )

def broadcast_command(update: Update, context: CallbackContext):
    user = update.effective_user
    if user.id != OWNER_ID:
        safe_api_call(update.message.reply_text, "You do not have permission to use this command.")
        return

    if not update.message.reply_to_message:
        safe_api_call(update.message.reply_text, "Please reply to a message to broadcast.")
        return

    target_msg = update.message.reply_to_message
    status_msg = safe_api_call(update.message.reply_text, "Starting broadcast...")

    if users_collection is not None:
        all_users = [doc["user_id"] for doc in users_collection.find({})]
        all_chats = [doc["chat_id"] for doc in chats_collection.find({})]
    else:
        all_users = list(in_memory_users)
        all_chats = list(in_memory_chats)

    targets = set(all_users + all_chats)
    sent = 0
    failed = 0

    for target_id in targets:
        copied = safe_api_call(
            target_msg.copy,
            chat_id=target_id
        )
        if copied is not False:
            sent += 1
        else:
            failed += 1
        time.sleep(0.05)

    if status_msg:
        safe_api_call(
            status_msg.edit_text,
            f"Broadcast Completed!\nTotal Targets: {len(targets)}\nSuccess: {sent}\nFailed: {failed}"
        )

def announce_command(update: Update, context: CallbackContext):
    user = update.effective_user
    if user.id != OWNER_ID:
        safe_api_call(update.message.reply_text, "You do not have permission to use this command.")
        return

    if not update.message.reply_to_message:
        safe_api_call(update.message.reply_text, "Please reply to a message to announce (forward).")
        return

    target_msg = update.message.reply_to_message
    status_msg = safe_api_call(update.message.reply_text, "Starting announcement forward...")

    if users_collection is not None:
        all_users = [doc["user_id"] for doc in users_collection.find({})]
        all_chats = [doc["chat_id"] for doc in chats_collection.find({})]
    else:
        all_users = list(in_memory_users)
        all_chats = list(in_memory_chats)

    targets = set(all_users + all_chats)
    sent = 0
    failed = 0

    for target_id in targets:
        forwarded = safe_api_call(
            target_msg.forward,
            chat_id=target_id
        )
        if forwarded is not False:
            sent += 1
        else:
            failed += 1
        time.sleep(0.05)

    if status_msg:
        safe_api_call(
            status_msg.edit_text,
            f"Announce Completed!\nTotal Targets: {len(targets)}\nSuccess: {sent}\nFailed: {failed}"
        )

def get_id(update: Update, context: CallbackContext):
    msg = update.effective_message
    chat = update.effective_chat
    track_interaction(chat.id, msg.from_user.id)

    if msg.reply_to_message:
        reply = msg.reply_to_message
        target_user = reply.from_user
        text = f"Replied User ID: {target_user.id}\nMessage ID: {reply.message_id}\n"
        if reply.forward_from:
            text += f"Forwarded User ID: {reply.forward_from.id}\n"
        text += f"Chat ID: {chat.id}"
        safe_api_call(msg.reply_text, text)
    elif context.args:
        username = context.args[0]
        if not username.startswith('@'):
            safe_api_call(msg.reply_text, "Please provide a valid username starting with '@'.")
            return
        try:
            target_chat = context.bot.get_chat(username)
            safe_api_call(msg.reply_text, f"{username} ID: {target_chat.id}")
        except Exception:
            safe_api_call(msg.reply_text, f"Could not find user {username}.")
    else:
        text = f"Chat ID: {chat.id}\nYour User ID: {msg.from_user.id}"
        safe_api_call(msg.reply_text, text)

def analyze_nsfw_text(text):
    if not text:
        return False
    # External API check (PurgoMalum profanity filter - 100% Free with rate limiting compliance)
    try:
        resp = requests.get("https://www.purgomalum.com/service/containsprofanity", params={"text": text[:500]}, timeout=3)
        if resp.status_code == 200 and resp.text.lower() == "true":
            return True
        elif resp.status_code == 429:
            time.sleep(1) # Compliance with rate limiting
    except Exception:
        pass
    
    # Fallback to comprehensive regex heuristic
    return bool(BAD_PATTERN.search(text))

def analyze_nsfw_media(msg, bot):
    # Retrieve low-resolution pre-compressed thumbnail to minimize CPU and bandwidth
    file_id = None
    if msg.photo:
        file_id = msg.photo[-2].file_id if len(msg.photo) > 1 else msg.photo[-1].file_id  # Medium resolution for fast upload & clear AI classification
    elif msg.video and msg.video.thumb:
        file_id = msg.video.thumb.file_id  # Compressed video preview
    elif msg.animation and msg.animation.thumb:
        file_id = msg.animation.thumb.file_id  # Compressed GIF preview
    elif msg.sticker:
        file_id = msg.sticker.thumb.file_id if msg.sticker.is_animated and msg.sticker.thumb else msg.sticker.file_id
    elif msg.document and msg.document.thumb:
        file_id = msg.document.thumb.file_id

    if not file_id:
        return False

    try:
        tg_file = safe_api_call(bot.get_file, file_id)
        if not tg_file:
            return False
            
        file_bytes = safe_api_call(tg_file.download_as_bytearray)
        if not file_bytes:
            return False

        # External NSFW Image/Media Analysis API check (Sightengine Free Public API Endpoint)
        try:
            files = {'media': ('thumb.jpg', bytes(file_bytes), 'image/jpeg')}
            api_user = SIGHTENGINE_USER or "1211514755"
            api_secret = SIGHTENGINE_SECRET or "dzzBcwTzT22q9364k7M3mct5kS85d4T5"
            resp = requests.post(
                "https://api.sightengine.com/1.0/check.json",
                data={'models': 'nudity-2.0,wad,offensive', 'api_user': api_user, 'api_secret': api_secret},
                files=files,
                timeout=10
            )
            if resp.status_code == 200:
                data = resp.json()
                nudity_dict = data.get("nudity", {})
                if isinstance(nudity_dict, dict):
                    raw = nudity_dict.get("raw", 0.0)
                    partial = nudity_dict.get("partial", 0.0)
                    suggestive = nudity_dict.get("suggestive", 0.0)
                    none_score = nudity_dict.get("none", 1.0)
                else:
                    raw, partial, suggestive, none_score = 0.0, 0.0, 0.0, 1.0
                
                nudity = (raw > 0.2) or (partial > 0.3) or (suggestive > 0.4) or (none_score < 0.7)
                
                # Robust extraction for drugs, weapons, gore, offensive
                def get_prob(field):
                    val = data.get(field)
                    if isinstance(val, dict):
                        return val.get("prob", 0.0)
                    elif isinstance(val, (float, int)):
                        return float(val)
                    wad_dict = data.get("wad", {})
                    if isinstance(wad_dict, dict):
                        wad_val = wad_dict.get(field)
                        if isinstance(wad_val, (float, int)):
                            return float(wad_val)
                    return 0.0

                drugs = get_prob("drugs") > 0.4
                weapons = get_prob("weapon") > 0.4
                gore = get_prob("gore") > 0.4
                offensive = get_prob("offensive") > 0.4
                if nudity or drugs or weapons or gore or offensive:
                    return True
            elif resp.status_code == 429:
                logger.warning("Sightengine API rate limit reached (429).")
                time.sleep(1) # Compliance with rate limiting
            else:
                logger.warning(f"Sightengine API returned {resp.status_code}: {resp.text}")
        except Exception as e:
            logger.error(f"Sightengine API request failed: {e}")
            pass
            
    except Exception as e:
        logger.warning(f"Error processing media thumbnail: {e}")

    return False

def is_nsfw_sticker(msg):
    """Detect explicit/18+ stickers from Telegram sticker metadata."""
    if not msg or not msg.sticker:
        return False
    sticker = msg.sticker
    set_name = getattr(sticker, "set_name", "") or ""
    emoji = getattr(sticker, "emoji", "") or ""
    metadata = f"{set_name} {emoji}".lower()
    nsfw_terms = [
        "18+", "18plus", "18_plus", "adult", "porn", "nsfw", "sex",
        "nude", "xxx", "boobs", "fuck", "blowjob", "onlyfans"
    ]
    return any(term in metadata for term in nsfw_terms) or bool(BAD_PATTERN.search(metadata))


def analyze_nsfw_sticker(sticker, bot):
    """Analyze a Telegram sticker thumbnail with Sightengine.

    Handles normal image stickers and video/animated stickers when Telegram
    provides a thumbnail. This is much stronger than relying only on sticker
    set names/emoji.
    """
    if not sticker:
        return False

    thumb = getattr(sticker, "thumb", None)
    if not thumb:
        return False

    try:
        tg_file = safe_api_call(bot.get_file, thumb.file_id)
        if not tg_file:
            return False

        file_bytes = safe_api_call(tg_file.download_as_bytearray)
        if not file_bytes:
            return False

        api_user = SIGHTENGINE_USER or "1211514755"
        api_secret = SIGHTENGINE_SECRET or "dzzBcwTzT22q9364k7M3mct5kS85d4T5"

        # Sightengine's nudity model is used for explicit/18+ detection.
        response = requests.post(
            "https://api.sightengine.com/1.0/check.json",
            data={
                "models": "nudity-2.0,offensive,wad",
                "api_user": api_user,
                "api_secret": api_secret,
            },
            files={"media": ("sticker.jpg", bytes(file_bytes), "image/jpeg")},
            timeout=10,
        )

        if response.status_code != 200:
            logger.warning(
                f"Sightengine sticker check returned {response.status_code}: "
                f"{response.text[:300]}"
            )
            return False

        data = response.json()
        nudity = data.get("nudity", {}) or {}

        # Conservative thresholds for explicit/18+ content.
        raw = float(nudity.get("raw", 0.0) or 0.0)
        partial = float(nudity.get("partial", 0.0) or 0.0)
        suggestive = float(nudity.get("suggestive", 0.0) or 0.0)

        if raw >= 0.18 or partial >= 0.35 or suggestive >= 0.65:
            return True

        # Catch obvious offensive sexual imagery where the nudity model may
        # not classify it strongly enough.
        offensive = data.get("offensive", {}) or {}
        if isinstance(offensive, dict):
            offensive_prob = float(offensive.get("prob", 0.0) or 0.0)
            if offensive_prob >= 0.85 and (raw >= 0.08 or partial >= 0.15):
                return True

    except Exception as e:
        logger.warning(f"Sticker NSFW analysis failed: {e}")

    return False


def check_security_violation(update: Update, context: CallbackContext):
    msg = update.effective_message
    if not msg:
        return

    chat = msg.chat
    user = msg.from_user
    if not user or chat.type == "private":
        return

    track_interaction(chat.id, user.id)

    # Allow admins, owner, sudo users
    if is_admin(chat, user.id, context.bot):
        return

    text = msg.text or msg.caption or ""
    is_nsfw = analyze_nsfw_text(text)

    if not is_nsfw and msg.sticker:
        is_nsfw = is_nsfw_sticker(msg)
        if not is_nsfw:
            is_nsfw = analyze_nsfw_sticker(msg.sticker, context.bot)

    # Detect explicit/18+ stickers using Telegram sticker metadata.
    if not is_nsfw and msg.sticker:
        is_nsfw = is_nsfw_sticker(msg)

    if not is_nsfw and (msg.photo or msg.video or msg.animation or msg.document):
        is_nsfw = analyze_nsfw_media(msg, context.bot)

    if not is_nsfw:
        return

    mode = get_group_setting(chat.id)

    # Delete the violating message immediately
    deleted = safe_api_call(context.bot.delete_message, chat_id=chat.id, message_id=msg.message_id)
    if deleted is False:
        return

    mention = f"<a href='tg://user?id={user.id}'>{html.escape(user.first_name)}</a>"

    if mode == "silent":
        pass
    elif mode == "warn":
        warn_text = f"⚠️ {mention}, prohibited material (18**, ill**** dr***, pir***, or viol****) is not allowed in this group!"
        buttons = [[InlineKeyboardButton("Close", callback_data="close_warning")]]
        safe_api_call(
            context.bot.send_message,
            chat_id=chat.id,
            text=warn_text,
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=ParseMode.HTML
        )
    elif mode == "warn_mute":
        warnings = increment_user_warnings(chat_id=chat.id, user_id=user.id)
        if warnings >= 3:
            safe_api_call(
                context.bot.restrict_chat_member,
                chat_id=chat.id,
                user_id=user.id,
                permissions=ChatPermissions(can_send_messages=False)
            )
            warn_text = f"🚫 {mention} has been muted for reaching 3 prohibited content warnings."
        else:
            warn_text = f"⚠️ {mention}, prohibited content is not allowed!\nWarning: {warnings}/3"
            
        buttons = [[InlineKeyboardButton("Close", callback_data="close_warning")]]
        safe_api_call(
            context.bot.send_message,
            chat_id=chat.id,
            text=warn_text,
            reply_markup=InlineKeyboardMarkup(buttons),
            parse_mode=ParseMode.HTML
        )

    if SUPPORT_ID:
        log_text = (
            f"🚫 Prohibited Content Violation\n\n"
            f"User: {user.first_name} ({user.id})\n"
            f"Group: {chat.title} ({chat.id})\n"
            f"Mode: {mode}\n"
            f"Text/Caption: {text}"
        )
        safe_api_call(context.bot.send_message, chat_id=SUPPORT_ID, text=log_text)

def check_edit(update: Update, context: CallbackContext):
    if not update.edited_message:
        return
        
    edited_msg = update.edited_message
    chat = edited_msg.chat
    user = edited_msg.from_user
    
    if chat.type == "private":
        return

    track_interaction(chat.id, user.id)

    if is_admin(chat, user.id, context.bot):
        return

    deleted = safe_api_call(context.bot.delete_message, chat_id=chat.id, message_id=edited_msg.message_id)
    
    if deleted is not False:
        user_mention = f"<a href='tg://user?id={user.id}'>{html.escape(user.first_name)}</a>"
        safe_api_call(
            context.bot.send_message,
            chat_id=chat.id,
            text=f"🚫 {user_mention} just edited a message or media caption.\nTo maintain transparency, the edited content has been deleted.",
            parse_mode=ParseMode.HTML
        )

def callback_query_handler(update: Update, context: CallbackContext):
    query = update.callback_query
    data = query.data
    try:
        if data in ["close_help", "close_status", "close_warning"]:
            safe_api_call(query.message.delete)
    except Exception:
        pass
    safe_api_call(query.answer)

def add_sudo(update: Update, context: CallbackContext):
    user = update.effective_user
    if user.id != OWNER_ID:
        safe_api_call(update.message.reply_text, "You do not have permission to use this command.")
        return
    
    if len(context.args) != 1:
        safe_api_call(update.message.reply_text, "Usage: /addsudo <username or user ID>")
        return
    
    target = context.args[0]
    try:
        if target.isdigit():
            target_id = int(target)
            target_name = target
        else:
            member = context.bot.get_chat(target)
            target_id = member.id
            target_name = member.username or member.first_name
    except Exception as e:
        safe_api_call(update.message.reply_text, f"Failed to resolve user: {e}")
        return
    
    if target_id not in sudo_users:
        sudo_users.append(target_id)
        if sudo_collection is not None:
            try:
                sudo_collection.update_one({"user_id": target_id}, {"$set": {"user_id": target_id}}, upsert=True)
            except Exception:
                pass
        safe_api_call(update.message.reply_text, f"Successfully added {target_name} ({target_id}) as a sudo user.")
    else:
        safe_api_call(update.message.reply_text, f"{target_name} is already a sudo user.")

def del_sudo(update: Update, context: CallbackContext):
    user = update.effective_user
    if user.id != OWNER_ID:
        safe_api_call(update.message.reply_text, "You do not have permission to use this command.")
        return
    
    if len(context.args) != 1:
        safe_api_call(update.message.reply_text, "Usage: /delsudo <username or user ID>")
        return
    
    target = context.args[0]
    try:
        if target.isdigit():
            target_id = int(target)
            target_name = target
        else:
            member = context.bot.get_chat(target)
            target_id = member.id
            target_name = member.username or member.first_name
    except Exception as e:
        safe_api_call(update.message.reply_text, f"Failed to resolve user: {e}")
        return
    
    if target_id in sudo_users:
        sudo_users.remove(target_id)
        if sudo_collection is not None:
            try:
                sudo_collection.delete_one({"user_id": target_id})
            except Exception:
                pass
        safe_api_call(update.message.reply_text, f"Successfully removed {target_name} ({target_id}) from sudo users.")
    else:
        safe_api_call(update.message.reply_text, f"{target_name} is not in the sudo users list.")

def sudo_list(update: Update, context: CallbackContext):
    if update.effective_user.id != OWNER_ID:
        safe_api_call(update.message.reply_text, "You do not have permission to use this command.")
        return

    text = "List of Sudo Users:\n\n"
    count = 1

    try:
        owner = context.bot.get_chat(OWNER_ID)
        owner_mention = mention_html(OWNER_ID, owner.first_name)
        text += f"{count}. {owner_mention} (Owner)\n"
    except Exception:
        text += f"{count}. {OWNER_ID} (Owner)\n"

    for user_id in sudo_users:
        if user_id != OWNER_ID:
            count += 1
            try:
                user = context.bot.get_chat(user_id)
                user_mention = mention_html(user_id, user.first_name)
                text += f"{count}. {user_mention} ({user_id})\n"
            except Exception:
                text += f"{count}. User ID: {user_id}\n"

    safe_api_call(update.message.reply_text, text, parse_mode=ParseMode.HTML)

def send_stats(update: Update, context: CallbackContext):
    user = update.effective_user
    if user.id != OWNER_ID:
        safe_api_call(update.message.reply_text, "You are not authorized to use this command.")
        return
    
    try:
        if users_collection is not None:
            users_count = users_collection.count_documents({})
            chat_count = chats_collection.count_documents({})
        else:
            users_count = len(in_memory_users)
            chat_count = len(in_memory_chats)
        
        stats_msg = (
            f"Security & Edit Guardian Stats:\n\n"
            f"Total Tracked Users: {users_count}\n"
            f"Total Protected Chats: {chat_count}\n"
            f"Uptime: {get_readable_time(int(time.time() - StartTime))}"
        )
        
        safe_api_call(update.message.reply_text, stats_msg)
    except Exception as e:
        logger.error(f"Error in send_stats: {e}")
        safe_api_call(update.message.reply_text, "Failed to fetch statistics.")

def clone(update: Update, context: CallbackContext):
    user = update.effective_user
    if user.id != OWNER_ID:
        safe_api_call(update.message.reply_text, "You are not authorized to use this command.")
        return

    if len(context.args) != 1:
        safe_api_call(update.message.reply_text, "Usage: /clone <Your Bot Token>")
        return

    new_bot_token = context.args[0]

    try:
        new_bot = Bot(token=new_bot_token)
        new_bot_info = new_bot.get_me()

        clone_updater = Updater(token=new_bot_token, use_context=True)
        clone_dispatcher = clone_updater.dispatcher

        clone_dispatcher.add_handler(CommandHandler("start", start))
        clone_dispatcher.add_handler(MessageHandler(Filters.update.edited_message, check_edit))
        clone_dispatcher.add_handler(MessageHandler(
        Filters.chat_type.groups &
        ~Filters.command &
        (Filters.text | Filters.photo | Filters.video | Filters.animation | Filters.sticker | Filters.document),
        check_security_violation
    ))
        clone_dispatcher.add_handler(CommandHandler("setmode", setmode_command))
        clone_dispatcher.add_handler(CommandHandler("settings", settings_command))
        clone_dispatcher.add_handler(CommandHandler("addsudo", add_sudo))
        clone_dispatcher.add_handler(CommandHandler("delsudo", del_sudo))
        clone_dispatcher.add_handler(CommandHandler("sudolist", sudo_list))
        clone_dispatcher.add_handler(CommandHandler("stats", send_stats))
        clone_dispatcher.add_handler(CommandHandler("status", status_command))
        clone_dispatcher.add_handler(CommandHandler("ping", ping_command))
        clone_dispatcher.add_handler(CommandHandler("broadcast", broadcast_command))
        clone_dispatcher.add_handler(CommandHandler("announce", announce_command))
        clone_dispatcher.add_handler(CommandHandler("clone", clone))
        clone_dispatcher.add_handler(CommandHandler("help", help_command))
        clone_dispatcher.add_handler(CommandHandler("id", get_id))
        clone_dispatcher.add_handler(CallbackQueryHandler(callback_query_handler))

        clone_updater.start_polling()

        safe_api_call(
            update.message.reply_text,
            f"Successfully cloned and deployed bot @{new_bot_info.username} ({new_bot_info.id})."
        )
    except Exception as e:
        safe_api_call(update.message.reply_text, f"Failed to clone the bot: {e}")

def main():
    logger.info("Starting Security & Edit Guardian Bot...")

    # Start Hugging Face Spaces health check server in background thread
    threading.Thread(target=run_health_server, daemon=True).start()
    logger.info("Allowing Hugging Face Spaces network proxy to register port 7860...")
    time.sleep(3)  # Give HF Spaces time to verify health probe before outbound API calls

    request_kwargs = {'connect_timeout': 30., 'read_timeout': 30.}
    updater = Updater(token=TELEGRAM_TOKEN, use_context=True, request_kwargs=request_kwargs)
    dispatcher = updater.dispatcher

    if SUPPORT_ID:
        threading.Thread(
            target=safe_api_call,
            args=(updater.bot.send_photo,),
            kwargs={
                "chat_id": SUPPORT_ID,
                "photo": PM_START_IMG,
                "caption": "Security & Edit Guardian Bot successfully started and protecting groups!"
            },
            daemon=True
        ).start()

    # Register handlers
    dispatcher.add_handler(CommandHandler("start", start))
    dispatcher.add_handler(MessageHandler(Filters.update.edited_message, check_edit))
    dispatcher.add_handler(MessageHandler(
        Filters.chat_type.groups &
        ~Filters.command &
        (Filters.text | Filters.photo | Filters.video | Filters.animation | Filters.sticker | Filters.document),
        check_security_violation
    ))
    dispatcher.add_handler(CommandHandler("setmode", setmode_command))
    dispatcher.add_handler(CommandHandler("settings", settings_command))
    dispatcher.add_handler(CommandHandler("addsudo", add_sudo))
    dispatcher.add_handler(CommandHandler("delsudo", del_sudo))
    dispatcher.add_handler(CommandHandler("sudolist", sudo_list))
    dispatcher.add_handler(CommandHandler("status", status_command))
    dispatcher.add_handler(CommandHandler("ping", ping_command))
    dispatcher.add_handler(CommandHandler("broadcast", broadcast_command))
    dispatcher.add_handler(CommandHandler("announce", announce_command))
    dispatcher.add_handler(CommandHandler("clone", clone))
    dispatcher.add_handler(CommandHandler("stats", send_stats))
    dispatcher.add_handler(CommandHandler("help", help_command))
    dispatcher.add_handler(CommandHandler("id", get_id))
    dispatcher.add_handler(CallbackQueryHandler(callback_query_handler))

    updater.start_polling()
    logger.info("Bot polling started successfully!")
    updater.idle()

if __name__ == '__main__':
    main()
