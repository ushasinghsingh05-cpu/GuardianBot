from telegram import InlineKeyboardMarkup, InlineKeyboardButton

PM_START_TEXT = """
Hello {} 👋 I'm your Ayu Guardian Bot, designed to keep your Telegram groups safe, clean, and transparent.

🛡️ Banword Protection: I automatically detect and delete messages containing 18+, abusive, or prohibited content.

🚫 Edited Message Deletion: I remove edited messages and media captions to ensure complete transparency.

📣 Group Notifications: Groups are notified whenever prohibited content or unauthorized edits are removed.

Get Started:
1. Add me to your group as an administrator.
2. I will start protecting your group instantly.
"""
    
def start_buttons(bot_username):
    return [
        [
            InlineKeyboardButton(
                text="➕ Add Me To Your Group",
                url=f"https://t.me/{bot_username}?startgroup=true",
            ),
        ],
        [
            InlineKeyboardButton(text="Support", url="https://t.me/thakurayu2955"),
            InlineKeyboardButton(text="Source", url="https://github.com/urstark/Guardian"),
        ],    
        [
            InlineKeyboardButton(text="Owner", url="https://t.me/thakurayu2955"),
        ],
    ]

IMG = [
    "https://telegra.ph/file/73c9aa7b5e1a2e053d915.jpg",
    "https://telegra.ph/file/6cf4d7a5d07cdbc5c4c4f.jpg",
    "https://telegra.ph/file/3938993e7f83b9201d961.jpg",
    "https://telegra.ph/file/867bd553810ac3a4cf09f.jpg",
    "https://telegra.ph/file/d102719ef028b224e0842.jpg",
    "https://telegra.ph/file/63dbc9108dca4a91121af.jpg",
    "https://telegra.ph/file/5225ee47a9cbb9a0e85b1.jpg",
    "https://telegra.ph/file/ee9751a286fd983f08086.jpg",
    "https://telegra.ph/file/fbfa4262e467652e75d83.jpg",
    "https://telegra.ph/file/865ce3676d535ec83dce9.jpg",
]
PM_START_IMG = "https://files.catbox.moe/ifjtm3.jpg"
