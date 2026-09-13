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
from pymongo import MongoClient, UpdateOne

TIME_RE = re.compile(r'^\d{1,2}:\d{2}$')
logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)

# 🇮🇳 India Standard Time (IST) Zone Declarations Configuration Matrix
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

# MongoDB Cloud Engine Cluster Instance Declarations Mapping Architecture
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
        logging.info("✅ MongoDB indices and storage collections verified successfully.")
    except Exception as e:
        logging.error(f"❌ Index optimizer failure logs: {e}")

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
    if not clean_str:
        return []
    allowed_list = []
    for x in clean_str.split(","):
        x_clean = x.strip()
        if x_clean.isdigit():
            allowed_list.append(int(x_clean))
        elif x_clean.startswith("-") and x_clean[1:].isdigit():
            allowed_list.append(int(x_clean))
    return allowed_list

def is_authorized(update: Update):
    if not update.message or not update.message.from_user: return True
    user_id = update.message.from_user.id
    chat_type = update.message.chat.type
    allowed_ids = get_allowed_ids()
    if chat_type in ["group", "supergroup"]:
        if user_id != OWNER_ID and user_id not in allowed_ids:
            return False
    return True

def escape_markdown(text):
    if not text: return text
    special_chars = ['_', '*', '[', ']', '(', ')', '~', '`']
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    return text

def format_time(seconds):
    if seconds < 60:
        return f"{int(seconds)}s"
    minutes = int(seconds) // 60
    secs = int(seconds) % 60
    return f"{minutes}m {secs}s"

# Global System Configurations Registry Allocation
GROUP_GAMES = {}
AUTORUN_TASKS = {}
AUTORUN_SERIAL_LOCK = asyncio.Lock()
# Fully operational conversation state sequences definitions
TITLE, DESCRIPTION, QUESTIONS, PRE_MESSAGE, TIMER, NEGATIVE = range(6)
EDIT_TITLE, EDIT_DESC, EDIT_TIMER, EDIT_NEGATIVE = range(6, 10)
EDIT_QUESTION_TEXT, EDIT_QUESTION_OPTIONS, EDIT_QUESTION_CORRECT, EDIT_QUESTION_EXPLANATION, EDIT_QUESTION_PRE_MESSAGE = range(10, 15)
TOPIC, Q_COUNT, LANGUAGE, EXPLANATION, DIFFICULTY, OPTIONS_COUNT, TIME_LIMIT = range(15, 22)

ai_client = None
if GEMINI_API_KEY:
    ai_client = genai.Client(api_key=GEMINI_API_KEY)

def generate_bulk_questions_ai(topic, count, lang, difficulty, options_cnt):
    if not ai_client: return None
    prompt = f"""Generate exactly {count} unique quiz questions ONLY in {lang} language about "{topic}".
Focus on latest 2026 data arrays if required.
Return ONLY valid JSON array mapping layouts (no markdown labels or wrappers):
[
  {{
    "question": "What is..?", 
    "options": ["A", "B", "C", "D"], 
    "correct": 2, 
    "explanation": "Short descriptive verification context metrics info string"
  }}
]"""
    try:
        response = ai_client.models.generate_content(model='gemini-2.5-flash', contents=prompt)
        clean_text = response.text.strip().replace("```json", "").replace("```", "").strip()
        match = re.search(r'\[.*\]', clean_text, re.DOTALL)
        if match: clean_text = match.group(0)
        questions = json.loads(clean_text)
        valid_questions = []
        for q in questions:
            if not q.get("question") or not q.get("options"): continue
            correct_idx = int(q.get("correct", 0))
            q["correct"] = correct_idx
            if "explanation" not in q or not q["explanation"]:
                q["explanation"] = f"The correct answer is option {correct_idx + 1}."
            valid_questions.append(q)
        return valid_questions[:count]
    except Exception as e:
        logging.error(f"❌ AI Generative system parsing error logs: {e}")
        return None

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

                init_text = (
                    f"<blockquote>🎲 Get ready for the quiz!</blockquote>\n\n"
                    f"<blockquote>📚 Title: {escape_markdown(quiz_data['title'])}</blockquote>\n"
                    f"<blockquote>🔥 Description: {escape_markdown(quiz_data.get('description', 'No description'))}</blockquote>\n"
                    f"<blockquote>🖊️ Questions: {total_q}</blockquote>\n"
                    f"<blockquote>⏱ Time per question: {quiz_data.get('timer', 30)}s</blockquote>\n"
                    f"<blockquote>📉 Negative Marking: `-{quiz_data.get('negative_value', 0.0)} Marks` per wrong answer</blockquote>\n\n"
                    "🏁 Click 'I am ready!' to start the quiz."
                )
                
                # Raw dictionary compilation method to preserve success color patterns
                raw_button = {"text": "I am ready!", "callback_data": f"ready_{quiz_id}", "style": "success"}
                await update.message.reply_text(
                    init_text, 
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(**raw_button)]]), 
                    parse_mode="HTML"
                )
                return

        welcome_text = (
            "<blockquote>👋 Welcome to Premium Quiz Bot!</blockquote>\n\n"
            "Aap is bot se quizzes bana kar apne dosto ke sath groups me realtime khel sakte hain.\n\n"
            "💡 Available Commands Layout:\n"
            "➤ `/newquiz` - Create manual forms builder quiz\n"
            "➤ `/autoquiz` - Setup AI instant auto generated quiz matrix\n"
            "➤ `/quizzes` - View your active repository records panel"
        )
        kb = [[InlineKeyboardButton("🚀 Create New Quiz", callback_data="btn_newquiz")],
              [InlineKeyboardButton("📚 View My Quizzes", callback_data="btn_viewquizzes")]] if is_private else [[InlineKeyboardButton("➕ Add me in your group", url=f"https://t.me{context.bot.username}?startgroup=true")]]
        await update.message.reply_text(welcome_text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    except Exception as e:
        logging.error(f"Error in start operations command mapping: {e}")

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "<blockquote>Help Center Control Center Menu</blockquote>\n\n"
        "➤ `/newquiz` – Step by step builder custom form\n"
        "➤ `/autoquiz` – Launch generative AI model wizard\n"
        "➤ `/quizzes` – View complete editing configuration grid dashboard\n"
        "➤ `/stop` – Kill active running polling group session\n"
        "➤ `/cancel` – Wipe current conversation buffer records"
    )
    await update.message.reply_text(help_text, parse_mode="HTML")

async def new_quiz_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    msg_obj = update.callback_query.message if update.callback_query else update.message
    if update.callback_query: await update.callback_query.answer()
    await msg_obj.reply_text("Let's create a new quiz. First, send me the title of your quiz (128 characters max):", reply_markup=ReplyKeyboardRemove())
    context.user_data["quiz_build"] = {"title": "", "description": "", "questions": []}
    return TITLE

async def receive_title(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    title = update.message.text.strip()
    if len(title) > 128:
        await update.message.reply_text("⚠️ Title exceeds boundary limits constraint. Please provide short name:")
        return TITLE
    context.user_data["quiz_build"]["title"] = title
    await update.message.reply_text("Good. Now send a description of your quiz. This is optional, you can `/skip` this step:")
    return DESCRIPTION

async def receive_desc(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    context.user_data["quiz_build"]["description"] = "" if text.lower() == "/skip" else text.strip()
    
    poll_button = KeyboardButton(text="Create a Question", request_poll=KeyboardButtonPollType(type="quiz"))
    bottom_container = ReplyKeyboardMarkup([[poll_button]], resize_keyboard=True, one_time_keyboard=False)
    
    await update.message.reply_text(
        f"Good. Your quiz now has 0 questions.\n\n"
        "💡 Now click bottom utility button and send me a quiz mode poll object question structure card layout:", 
        reply_markup=bottom_container
    )
    return QUESTIONS
async def receive_poll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    poll = update.message.poll
    if poll.type != "quiz":
        await update.message.reply_text("❌ Please pass valid Quiz mode verified poll structure card only:")
        return QUESTIONS
    if len(poll.options) > 7:
        await update.message.reply_text("❌ Configuration constraint warning: Max 7 options allowed.")
        return QUESTIONS

    opts = [o.text for o in poll.options]
    q_data = {
        "text": poll.question, 
        "options": opts, 
        "correct": int(poll.correct_option_id), 
        "explanation": poll.explanation if poll.explanation else "", 
        "pre_message": ""
    }
    context.user_data["quiz_build"]["questions"].append(q_data)
    
    await update.message.reply_text(
        f"✅ Question successfully appended! Count: {len(context.user_data['quiz_build']['questions'])}\n\n"
        "Send another poll question card layout matrix or type `/done` execution command parameters to finalize configurations."
    )
    return QUESTIONS

async def finish_quiz_creation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Closing wizard form panels...", reply_markup=ReplyKeyboardRemove())
    timer_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("⏱️ 15s", callback_data="timer_15"), InlineKeyboardButton("⏱️ 30s", callback_data="timer_30")],
        [InlineKeyboardButton("⏱️ 40s", callback_data="timer_40"), InlineKeyboardButton("⏱️ 60s", callback_data="timer_60")]
    ])
    await update.message.reply_text("Please choose duration limit constraints parameters configuration mapping per card:", reply_markup=timer_keyboard)
    return TIMER
async def handle_timer_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    val = int(query.data.replace("timer_", ""))
    context.user_data["quiz_build"]["timer"] = val
    
    neg_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ No Negative (0.0)", callback_data="neg_0.0"), InlineKeyboardButton("📉 1/4th (-0.25)", callback_data="neg_0.25")],
        [InlineKeyboardButton("📉 Half (-0.5)", callback_data="neg_0.5"), InlineKeyboardButton("📉 Single (-1.0)", callback_data="neg_1.0")]
    ])
    await query.message.reply_text("Select Negative marking value parameters logic schema criteria:", reply_markup=neg_keyboard)
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
        questions_col.insert_one({
            "id": get_next_sequence("question_id"), "quiz_id": quiz_id, "question_text": q["text"],
            "options": json.dumps(q["options"]), "correct_answer": int(q["correct"]),
            "explanation": q["explanation"], "pre_message": q["pre_message"]
        })
        
    await query.message.reply_text(f"✅ Quiz structural definitions successfully secure deployed inside Cloud clusters database! Access ID Key: `quiz_{quiz_id}`", parse_mode="Markdown")
    context.user_data.clear()
    return ConversationHandler.END

async def quizzes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type in ['group', 'supergroup']:
        await update.message.reply_text("⚠️ Error constraints: Run this dashboard command in private DM configuration channel context.")
        return
    user_id = update.message.from_user.id
    quizzes = list(quizzes_col.find({"creator_id": user_id}))
    if not quizzes:
        await update.message.reply_text("❌ Cloud data structures index reports completely empty configuration profiles records logs.")
        return
    text = "📚 **Your Account Created Stored Data Quizzes Index Maps:**\n\n"
    kb = []
    for idx, q in enumerate(quizzes, 1):
        text += f"{idx}. **{q['title']}** | Key ID: `quiz_{q['quiz_id']}`\n"
        kb.append([InlineKeyboardButton(f"📖 Manage Q{idx}", callback_data=f"viewq_{q['quiz_id']}")])
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")

async def view_my_quizzes(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    quizzes = list(quizzes_col.find({"creator_id": user_id}))
    if not quizzes:
        await query.edit_message_text("No records.")
        return
    text = "📚 **Cloud Repositories Viewer Index:**\n\n"
    kb = []
    for q in quizzes:
        kb.append([InlineKeyboardButton(f"Launch Quiz ID: {q['quiz_id']}", callback_data=f"ready_{q['quiz_id']}")])
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
    game["joined_users"][user_id] = query.from_user.first_name or "Player"
    
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
        chat_id=chat_id, question=f"[{game['current_q']+1}/{len(questions)}] {q['question_text']}", options=options, type="quiz",
        correct_option_id=int(q["correct_answer"]), explanation=q.get("explanation"), is_anonymous=False
    )
    game["poll_map"][poll_msg.poll.id] = {"question_index": game["current_q"]}
    
    await asyncio.sleep(quiz_data.get("timer", 30))
    game["current_q"] += 1
    asyncio.create_task(send_next_group_poll(chat_id, context))

async def stop_quiz(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id not in GROUP_GAMES:
        await update.message.reply_text("❌ Error configuration data loops records: No running matches active in this room logs.")
        return
    await update.message.reply_text("Force halting active calculations parameters sequence...")
    await compile_group_leaderboard(chat_id, context)
async def autoquiz_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_authorized(update): return ConversationHandler.END
    await update.message.reply_text("🤖 **AI Auto-Quiz Pipeline Configurator Engine Wizard:**\n\nProvide core data subject topic strings context directly:")
    return TOPIC

async def handle_topic(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['topic'] = update.message.text.strip()
    await update.message.reply_text("🔢 Define target card elements configuration values limit constraints index count:")
    return Q_COUNT

async def handle_q_count(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try: context.user_data['q_count'] = int(update.message.text.strip())
    except: context.user_data['q_count'] = 5
    await update.message.reply_text("🌐 Choose structural parsing language rules context parameter (English / Hindi):")
    return LANGUAGE

async def handle_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['language'] = update.message.text.strip()
    topic = context.user_data['topic']
    count = context.user_data['q_count']
    lang = context.user_data['language']
    
    load = await update.message.reply_text("🤖 **Google Gemini core models calculating vectors mappings pipelines data logs details...**")
    ai_questions = generate_bulk_questions_ai(topic, count, lang, "Medium", 4)
    await load.delete()
    
    if not ai_questions:
        await update.message.reply_text("❌ API framework response generation crash log timeout. Rerun `/autoquiz` command.")
        return ConversationHandler.END
        
    quiz_id = get_next_sequence("quiz_id")
    quizzes_col.insert_one({"quiz_id": quiz_id, "creator_id": update.message.from_user.id, "title": f"AI Quiz: {topic}", "description": f"AI generated data about {topic}", "timer": 30, "negative_value": 0.0})
    
    for q in ai_questions:
        questions_col.insert_one({"id": get_next_sequence("question_id"), "quiz_id": quiz_id, "question_text": q["question"], "options": json.dumps(q["options"]), "correct_answer": int(q["correct"]), "explanation": q["explanation"], "pre_message": ""})
        
    await update.message.reply_text(f"💯 **AI AutoQuiz Deployed!** Share connection deep-link sequence structure maps: `https://t.me{context.bot.username}?start=quiz_{quiz_id}`", parse_mode="Markdown")
    context.user_data.clear()
    return ConversationHandler.END

async def track_poll_answers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ans = update.poll_answer
    for cid, game in GROUP_GAMES.items():
        if ans.poll_id in game["poll_map"]:
            q_idx = game["poll_map"][ans.poll_id]["question_index"]
            if ans.user.id not in game["user_answers"]: game["user_answers"][ans.user.id] = {}
            game["user_answers"][ans.user.id][q_idx] = {"selected": ans.option_ids[0] if ans.option_ids else -1, "timestamp": datetime.now()}

async def compile_group_leaderboard(chat_id, context):
    game = GROUP_GAMES.get(chat_id)
    if not game: return
    
    roasts = [
        "[टॉपर भाई] भाई तुमने तो सीधे किताब ही रट मारी थी क्या? टॉपर बनने का इरादा प्रमाणित है!",
        "[गूगल का दामाद] भाई गूगल से सीधा कनेक्शन है क्या तुम्हारा? या फिर अंतर्यामी हो?",
        "[सिर्फ हाजिरी] आप सिर्फ परीक्षा हॉल की हवा खाने आए थे क्या? इतना कम स्कोर देखकर हैरानी हुई!",
        "[दानवीर कर्ण] अपने सारे नंबर गलत जवाबों के रास्ते परीक्षक को दान कर आए। इसी को कहते हैं दान!"
    ]
    
    text = "🏁 **The Match Has Ended! Dynamic Savage Board Matrix Calculations Logs Results Summary:**\n\n"
    for idx, (uid, answers) in enumerate(game["user_answers"].items(), 1):
        name = game["joined_users"].get(uid, "Player")
        text += f"🏅 **Rank {idx}:** {name}\n   ➻ Parsed items verified count logs: `{len(answers)}` cards database.\n   ➻ Savage feedback commentary: *{random.choice(roasts)}*\n\n"
        
    await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
    GROUP_GAMES.pop(chat_id, None)

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("❌ Temporary wizard operations buffer flushed reset completely clean.", reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END

async def main():
    init_db()
    request_config = HTTPXRequest(connect_timeout=35.0, read_timeout=45.0, write_timeout=35.0)
    app = Application.builder().token(BOT_TOKEN).request(request_config).build()
    
    new_quiz_handler = ConversationHandler(
        entry_points=[CommandHandler("newquiz", new_quiz_start), CallbackQueryHandler(new_quiz_start, pattern="^btn_newquiz$")],
        states={
            TITLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_title)],
            DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_desc)],
            QUESTIONS: [MessageHandler(filters.POLL, receive_poll), CommandHandler("done", finish_quiz_creation)],
            TIMER: [CallbackQueryHandler(handle_timer_text, pattern="^timer_")],
            NEGATIVE: [CallbackQueryHandler(handle_negative_selection, pattern="^neg_")]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    
    auto_quiz_handler = ConversationHandler(
        entry_points=[CommandHandler("autoquiz", autoquiz_start)],
        states={
            TOPIC: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_topic)],
            Q_COUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_q_count)],
            LANGUAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_language)]
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )
    
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("quizzes", quizzes_command))
    app.add_handler(CommandHandler("stop", stop_quiz))
    app.add_handler(new_quiz_handler)
    app.add_handler(auto_quiz_handler)
    app.add_handler(CallbackQueryHandler(view_my_quizzes, pattern="^btn_viewquizzes$"))
    app.add_handler(CallbackQueryHandler(handle_ready_click, pattern="^ready_"))
    app.add_handler(PollAnswerHandler(track_poll_answers))
    
    logging.info("🚀 System initialized with absolute original parameter sets securely live.")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    await asyncio.Event().wait()

if __name__ == '__main__':
    asyncio.run(main())
