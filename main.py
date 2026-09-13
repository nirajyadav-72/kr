import os
import json
import re
import uuid
import logging
import random
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from dotenv import load_dotenv
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove, 
    KeyboardButton, KeyboardButtonPollType, ReplyKeyboardMarkup,
    InlineQueryResultArticle, InputTextMessageContent  
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, 
    filters, ContextTypes, ConversationHandler, CallbackQueryHandler, PollAnswerHandler,
    InlineQueryHandler  
)
from telegram.error import NetworkError
from telegram.request import HTTPXRequest
from google import genai
from pymongo import MongoClient

# Enable Logging
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

# 🇮🇳 India Standard Time (IST) Timezone
TIME_RE = re.compile(r'^\d{1,2}:\d{2}$')
IST = timezone(timedelta(hours=5, minutes=30))
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID")) if os.getenv("OWNER_ID") else None
SUPPORT_GROUP_ID = int(os.getenv("SUPPORT_GROUP_ID")) if os.getenv("SUPPORT_GROUP_ID") else None
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MONGODB_URL = os.getenv("MONGODB_URL")

if not MONGODB_URL:
    logging.critical("❌ MONGODB_URL not found in environment variables!")
    raise ValueError("MONGODB_URL is missing")

# MongoDB Global Database Connections
mongo_client = MongoClient(MONGODB_URL, serverSelectionTimeoutMS=5000)
db = mongo_client["quiz_bot_db"]

quizzes_col = db["quizzes"]
questions_col = db["questions"]
broadcast_users_col = db["broadcast_users"]
broadcast_groups_col = db["broadcast_groups"]
autoruns_col = db["autoruns"]
def init_db():
    try:
        quizzes_col.create_index("quiz_id", unique=True)
        quizzes_col.create_index("creator_id")
        questions_col.create_index("quiz_id")
        questions_col.create_index("id", unique=True)
        broadcast_users_col.create_index("chat_id", unique=True)
        broadcast_groups_col.create_index("chat_id", unique=True)
        autoruns_col.create_index("id", unique=True)
        logging.info("✅ MongoDB indexes verified successfully.")
    except Exception as e:
        logging.error(f"❌ Index initialization error: {e}")

def get_next_sequence(sequence_name):
    seq_col = db["counters"]
    ret = seq_col.find_one_and_update(
        {"_id": sequence_name},
        {"$inc": {"seq": 1}},
        upsert=True,
        return_document=True
    )
    return ret["seq"]

def get_allowed_ids():
    raw_str = os.environ.get("ALLOWED_USER_IDS", "")
    clean_str = raw_str.replace('"', '').replace("'", "").strip()
    if not clean_str: return []
    allowed_list = []
    for x in clean_str.split(","):
        x_clean = x.strip()
        if x_clean.isdigit(): allowed_list.append(int(x_clean))
        elif x_clean.startswith("-") and x_clean[1:].isdigit(): allowed_list.append(int(x_clean))
    return allowed_list

def is_authorized(update: Update):
    user_id = update.message.from_user.id
    chat_type = update.message.chat.type
    allowed_ids = get_allowed_ids()
    if chat_type in ["group", "supergroup"]:
        if user_id != OWNER_ID and user_id not in allowed_ids: return False
    return True

# Gemini API client injection configurations
ai_client = None
if GEMINI_API_KEY:
    ai_client = genai.Client(api_key=GEMINI_API_KEY)

# Memory Maps Allocation Structures
GROUP_GAMES = {}
AUTORUN_TASKS = {}
AUTORUN_SERIAL_LOCK = asyncio.Lock()

# States Mapping Framework Definitions
TITLE, DESCRIPTION, QUESTIONS, PRE_MESSAGE, TIMER, NEGATIVE = range(6)
EDIT_TITLE, EDIT_DESC, EDIT_TIMER, EDIT_NEGATIVE = range(6, 10)
EDIT_QUESTION_PRE_MESSAGE, EDIT_QUESTION_EXPLANATION = range(10, 12)
TOPIC, Q_COUNT, LANGUAGE, EXPLANATION, DIFFICULTY, OPTIONS_COUNT, TIME_LIMIT = range(12, 19)

def escape_markdown(text):
    if not text: return text
    special_chars = ['_', '*', '[', ']', '(', ')', '~', '`']
    for char in special_chars: text = text.replace(char, f'\\{char}')
    return text

def format_time(seconds):
    if seconds < 60: return f"{int(seconds)}s"
    return f"{int(seconds) // 60}m {int(seconds) % 60}s"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_id = update.message.chat.id
        chat_type = update.message.chat.type
        is_private = str(chat_type) == "private"
        
        if is_private:
            broadcast_users_col.update_one({"chat_id": chat_id}, {"$set": {"chat_id": chat_id}}, upsert=True)
        else:
            broadcast_groups_col.update_one({"chat_id": chat_id}, {"$set": {"chat_id": chat_id}}, upsert=True)

        if context.args and len(context.args) > 0:
            first_arg = context.args[0]  
            if first_arg.startswith("quiz_"):
                quiz_id = int(first_arg.split("_")[1])
                quiz_data = quizzes_col.find_one({"quiz_id": quiz_id})
                total_q = questions_col.count_documents({"quiz_id": quiz_id})
                
                if not quiz_data:
                    await update.message.reply_text("❌ Quiz data not found.")
                    return

                time_disp = f"{quiz_data.get('timer', 30)} sec"
                init_text = (
                    f"<blockquote>🎲 Get ready for the quiz!</blockquote>\n\n"
                    f"<blockquote>📚 Title: {escape_markdown(quiz_data['title'])}</blockquote>\n"
                    f"<blockquote>🔥 Description: {escape_markdown(quiz_data.get('description', ''))}</blockquote>\n"
                    f"<blockquote>🖊️ Questions: {total_q}</blockquote>\n"
                    f"<blockquote>⏱ Time per question: {time_disp}</blockquote>\n\n"
                    "🏁 Click 'I am ready!' to start the quiz."
                )
                raw_button = {"text": "I am ready!", "callback_data": f"ready_{quiz_id}", "style": "success"}
                await update.message.reply_text(init_text, reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(**raw_button)]]), parse_mode="HTML")
                return

        welcome_text = "<blockquote>👋 Welcome to Premium Cloud Quiz Bot!</blockquote>\n\nData is securely stored in MongoDB Cloud."
        kb = [[InlineKeyboardButton("🚀 Create New Quiz", callback_data="btn_newquiz")],
              [InlineKeyboardButton("📚 View My Quizzes", callback_data="btn_viewquizzes")]] if is_private else [[InlineKeyboardButton("➕ Add me in your group", url=f"https://t.me{context.bot.username}?startgroup=true")]]
        await update.message.reply_text(welcome_text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    except Exception as e:
        logging.error(f"Error in start routing panel: {e}")


  async def new_quiz_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg_obj = update.callback_query.message if update.callback_query else update.message
    if update.callback_query: await update.callback_query.answer()
    await msg_obj.reply_text("Send me the title of your quiz (128 chars max):", reply_markup=ReplyKeyboardRemove())
    context.user_data["quiz_build"] = {"title": "", "description": "", "questions": []}
    return TITLE

async def receive_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    title = update.message.text.strip()
    if len(title) > 128:
        await update.message.reply_text("Too long. Max 128 characters:")
        return TITLE
    context.user_data["quiz_build"]["title"] = title
    await update.message.reply_text("Send a description or type /skip:")
    return DESCRIPTION

async def receive_desc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    context.user_data["quiz_build"]["description"] = "" if text.lower() == "/skip" else text.strip()
    poll_button = KeyboardButton(text="Create a Question", request_poll=KeyboardButtonPollType(type="quiz"))
    await update.message.reply_text("Now send me a quiz mode poll question:", reply_markup=ReplyKeyboardMarkup([[poll_button]], resize_keyboard=True))
    return QUESTIONS

async def receive_poll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    poll = update.message.poll
    if poll.type != "quiz":
        await update.message.reply_text("Please use Quiz mode polls only:")
        return QUESTIONS
    opts = [o.text for o in poll.options]
    q_data = {
        "text": poll.question, "options": opts, "correct": poll.correct_option_id,
        "explanation": poll.explanation if poll.explanation else "", "pre_message": ""
    }
    context.user_data["quiz_build"]["questions"].append(q_data)
    await update.message.reply_text("✅ Question added! Send more polls or type /done to complete setup.")
    return QUESTIONS

async def finish_quiz_creation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    timer_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⏱️ 15s", callback_data="timer_15"), InlineKeyboardButton("⏱️ 30s", callback_data="timer_30")]])
    await update.message.reply_text("Select per-question timer:", reply_markup=timer_keyboard)
    return TIMER

async def handle_timer_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    val = int(query.data.replace("timer_", ""))
    context.user_data["quiz_build"]["timer"] = val
    neg_keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("No Negative", callback_data="neg_0.0"), InlineKeyboardButton("📉 -0.25", callback_data="neg_0.25")]])
    await query.message.reply_text("Choose negative marking scheme:", reply_markup=neg_keyboard)
    return NEGATIVE

async def handle_negative_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    neg_val = float(query.data.replace("neg_", ""))
    quiz_build = context.user_data.get("quiz_build")
    user_id = query.from_user.id
    
    quiz_id = get_next_sequence("quiz_id")
    quizzes_col.insert_one({
        "quiz_id": quiz_id, "creator_id": user_id, "title": quiz_build["title"],
        "description": quiz_build["description"], "timer": quiz_build["timer"], "negative_value": neg_val
    })
    
    for q in quiz_build["questions"]:
        q_id = get_next_sequence("question_id")
        questions_col.insert_one({
            "id": q_id, "quiz_id": quiz_id, "question_text": q["text"],
            "options": json.dumps(q["options"]), "correct_answer": q["correct"],
            "explanation": q["explanation"], "pre_message": q["pre_message"]
        })
        
    await query.message.reply_text(f"✅ Quiz Custom Build Saved Successfully! Quiz ID: `{quiz_id}`", parse_mode="Markdown")
    context.user_data.clear()
    return ConversationHandler.END

async def view_my_quizzes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    quizzes = list(quizzes_col.find({"creator_id": user_id}))
    if not quizzes:
        await query.edit_message_text("No quizzes found.")
        return
    text = "📚 Cloud saved quizzes:\n\n"
    kb = []
    for q in quizzes:
        text += f"• {q['title']} (ID: {q['quiz_id']})\n"
        kb.append([InlineKeyboardButton(f"Start Quiz {q['quiz_id']}", callback_data=f"ready_{q['quiz_id']}")])
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))

async def handle_ready_click(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = query.message.chat_id
    quiz_id = int(query.data.split("_")[1])
    
    if chat_id not in GROUP_GAMES:
        GROUP_GAMES[chat_id] = {
            "quiz_id": quiz_id, "joined_users": {}, "current_q": 0, "scores": {},
            "poll_map": {}, "user_answers": {}, "question_start_times": {}, "ready_users": set(),
            "quiz_started": False, "poll_message_ids": {}, "start_lock": asyncio.Lock()
        }
    game = GROUP_GAMES[chat_id]
    user_id = query.from_user.id
    game["ready_users"].add(user_id)
    game["joined_users"][user_id] = query.from_user.first_name
    
    if len(game["ready_users"]) >= 1:
        async with game["start_lock"]:
            if not game["quiz_started"]:
                game["quiz_started"] = True
                await query.edit_message_reply_markup(reply_markup=None)
                asyncio.create_task(send_next_group_poll(chat_id, context))

async def send_next_group_poll(chat_id, context):
    game = GROUP_GAMES.get(chat_id)
    if not game: return
    
    quiz_id = game["quiz_id"]
    quiz_data = quizzes_col.find_one({"quiz_id": quiz_id})
    questions = list(questions_col.find({"quiz_id": quiz_id}).sort("id", 1))
    
    if game["current_q"] >= len(questions):
        await compile_group_leaderboard(chat_id, context)
        return
        
    q = questions[game["current_q"]]
    options = json.loads(q["options"])
    
    game["question_start_times"][game["current_q"]] = datetime.now()
    poll_msg = await context.bot.send_poll(
        chat_id=chat_id, question=q["question_text"], options=options, type="quiz",
        correct_option_id=int(q["correct_answer"]), explanation=q.get("explanation"), is_anonymous=False
    )
    game["poll_map"][poll_msg.poll.id] = {"question_index": game["current_q"]}
    
    await asyncio.sleep(quiz_data.get("timer", 30))
    game["current_q"] += 1
    asyncio.create_task(send_next_group_poll(chat_id, context))

async def track_poll_answers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ans = update.poll_answer
    for cid, game in GROUP_GAMES.items():
        if ans.poll_id in game["poll_map"]:
            q_idx = game["poll_map"][ans.poll_id]["question_index"]
            if ans.user.id not in game["user_answers"]: game["user_answers"][ans.user.id] = {}
            game["user_answers"][ans.user.id][q_idx] = {"selected": ans.option_ids if ans.option_ids else -1, "timestamp": datetime.now()}

async def compile_group_leaderboard(chat_id, context):
    game = GROUP_GAMES.get(chat_id)
    if not game: return
    text = "🏁 **Quiz Ended! Final Leaderboard:**\n\n"
    for uid, answers in game["user_answers"].items():
        name = game["joined_users"].get(uid, "Player")
        text += f"👤 {name} - Answered {len(answers)} questions\n"
    await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
    GROUP_GAMES.pop(chat_id, None)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Operation Cancelled.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def main():
    init_db()
    request_config = HTTPXRequest(connect_timeout=35.0, read_timeout=45.0, write_timeout=35.0)
    app = Application.builder().token(BOT_TOKEN).request(request_config).build()
    
    new_quiz_handler = ConversationHandler(
        entry_points=[CommandHandler("newquiz", new_quiz_start), CallbackQueryHandler(new_quiz_start, pattern="^btn_newquiz$")],
        states={
            TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_title)],
            DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_desc), CommandHandler("skip", receive_desc)],
            QUESTIONS: [MessageHandler(filters.POLL, receive_poll), CommandHandler("done", finish_quiz_creation)],
            TIMER: [CallbackQueryHandler(handle_timer_text, pattern="^timer_")],
            NEGATIVE: [CallbackQueryHandler(handle_negative_selection, pattern="^neg_")]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(new_quiz_handler)
    app.add_handler(CallbackQueryHandler(view_my_quizzes, pattern="^btn_viewquizzes$"))
    app.add_handler(CallbackQueryHandler(handle_ready_click, pattern="^ready_"))
    app.add_handler(PollAnswerHandler(track_poll_answers))
    
    logging.info("🚀 Polling started securely with MongoDB.")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    await asyncio.Event().wait()

if __name__ == '__main__':
    asyncio.run(main())
  
