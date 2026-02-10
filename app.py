import gradio as gr
import whisper
import os
import time
import threading
import requests
import sqlite3
import uuid
import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from datetime import datetime
from docx import Document

# --- НАСТРОЙКИ ---
TG_BOT_TOKEN = "8504609196:AAE-AXIpfytvvDigddCHMvTT9ukPp9m-SWw"
TG_BOT_USERNAME = "whisper_log_bot"
SITE_URL = "https://whisper.chernienko.pro" # Ссылка для кнопки

STORAGE_DIR = "/app/storage"
DB_PATH = os.path.join(STORAGE_DIR, "users.db")
FILES_DIR = os.path.join(STORAGE_DIR, "files")

# Создаем папки
os.makedirs(FILES_DIR, exist_ok=True)

bot = telebot.TeleBot(TG_BOT_TOKEN)

# Глобальная модель
current_model = None
current_model_name = ""

# --- БАЗА ДАННЫХ ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS login_sessions
                 (token TEXT PRIMARY KEY, user_id TEXT, created_at REAL)''')
    c.execute('''CREATE TABLE IF NOT EXISTS tasks
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                  user_id TEXT, 
                  filename TEXT, 
                  status TEXT, 
                  result_path TEXT, 
                  created_at TEXT)''')
    conn.commit()
    conn.close()

init_db()

# --- БОТ: СЛУШАЕМ КОМАНДУ /start ---
def bot_polling():
    while True:
        try:
            bot.polling(none_stop=True, interval=2)
        except Exception as e:
            print(f"Bot error: {e}")
            time.sleep(5)

@bot.message_handler(commands=['start'])
def handle_start(message):
    try:
        args = message.text.split()
        if len(args) > 1:
            login_token = args[1]
            user_id = str(message.chat.id)
            
            conn = sqlite3.connect(DB_PATH)
            conn.execute("INSERT OR REPLACE INTO login_sessions (token, user_id, created_at) VALUES (?, ?, ?)", 
                         (login_token, user_id, time.time()))
            conn.commit()
            conn.close()
            
            bot.reply_to(message, "✅ Вы успешно авторизованы! Вернитесь на сайт.")
        else:
            bot.reply_to(message, "Привет! Это бот для транскрибации. Зайди на сайт, чтобы начать.")
    except Exception as e:
        print(e)

threading.Thread(target=bot_polling, daemon=True).start()

# --- ЛОГИКА АВТОРИЗАЦИИ ---
def generate_login_link():
    token = str(uuid.uuid4())
    link = f"https://t.me/{TG_BOT_USERNAME}?start={token}"
    return token, link

def check_login_status(token):
    if not token: return None
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM login_sessions WHERE token=?", (token,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None

# --- ТРАНСКРИБАЦИЯ ---
def load_model(model_size):
    global current_model, current_model_name
    if current_model_name != model_size:
        current_model = whisper.load_model(model_size)
        current_model_name = model_size
    return current_model

def send_file_to_tg(user_id, filepath, caption):
    try:
        markup = InlineKeyboardMarkup()
        btn = InlineKeyboardButton("📂 В кабинет", url=SITE_URL)
        markup.add(btn)
        
        with open(filepath, "rb") as f:
            bot.send_document(user_id, f, caption=caption, reply_markup=markup)
    except Exception as e:
        print(f"Ошибка отправки в ТГ: {e}")

# ОДИНОЧНЫЙ ФАЙЛ
def process_single_file(user_id, file_path, original_name, model_size, task_id):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", ("⏳ В работе...", task_id))
        conn.commit()
        conn.close()

        model = load_model(model_size)
        result = model.transcribe(file_path, language="Russian")
        
        full_text = []
        for s in result.get('segments', []):
            t_start = time.strftime("%M:%S", time.gmtime(s['start']))
            full_text.append(f"[{t_start}] — {s['text'].strip()}")
            
        doc = Document()
        doc.add_paragraph(f"Файл: {original_name}")
        doc.add_paragraph(f"Модель: {model_size}")
        doc.add_paragraph("\n".join(full_text))
        
        res_filename = f"Transcription_{int(time.time())}.docx"
        res_path = os.path.join(FILES_DIR, res_filename)
        doc.save(res_path)
        
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE tasks SET status = ?, result_path = ? WHERE id = ?", 
                     ("✅ Готово", res_path, task_id))
        conn.commit()
        conn.close()
        
        send_file_to_tg(user_id, res_path, f"Готово: {original_name}")
        
    except Exception as e:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (f"❌ Ошибка: {str(e)}", task_id))
        conn.commit()
        conn.close()
        bot.send_message(user_id, f"Ошибка с файлом {original_name}: {e}")

# ОБЪ