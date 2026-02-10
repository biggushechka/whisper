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
SITE_URL = "https://whisper.chernienko.pro" 

# !!! ИСПРАВЛЕНИЕ: ПИШЕМ В КОРЕНЬ, ЧТОБЫ НЕ БЫЛО ОШИБОК ПРАВ !!!
STORAGE_DIR = "/app"
DB_PATH = os.path.join(STORAGE_DIR, "users.db")
FILES_DIR = os.path.join(STORAGE_DIR, "files")

# Создаем папку для файлов
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

# --- БОТ ---
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
            bot.reply_to(message, "Привет! Зайди на сайт, чтобы начать.")
    except Exception as e:
        print(e)

threading.Thread(target=bot_polling, daemon=True).start()

# --- АВТОРИЗАЦИЯ ---
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

def process_merged_batch(user_id, file_list, model_size, task_id):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", ("⏳ Обработка пакета...", task_id))
        conn.commit()
        conn.close()
        
        model = load_model(model_size)
        doc = Document()
        doc.add_paragraph(f"Сводная транскрибация (Файлов: {len(file_list)})")
        
        for f_path, f_name in file_list:
            doc.add_page_break()
            doc.add_heading(f"Файл: {f_name}", level=1)
            result = model.transcribe(f_path, language="Russian")
            for s in result.get('segments', []):
                t_start = time.strftime("%M:%S", time.gmtime(s['start']))
                doc.add_paragraph(f"[{t_start}] — {s['text'].strip()}")
        
        res_filename = f"MERGED_{int(time.time())}.docx"
        res_path = os.path.join(FILES_DIR, res_filename)
        doc.save(res_path)
        
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE tasks SET status = ?, result_path = ? WHERE id = ?", 
                     ("✅ Пакет готов", res_path, task_id))
        conn.commit()
        conn.close()
        send_file_to_tg(user_id, res_path, f"🔥 Сводный отчет готов ({len(file_list)} файлов)")
    except Exception as e:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (f"❌ Ошибка: {str(e)}", task_id))
        conn.commit()
        conn.close()
        bot.send_message(user_id, f"Ошибка пакета: {e}")

def add_task(user_id, files, model_size, merge_mode):
    if not user_id: return "❌ Ошибка: Вы не авторизованы (попробуйте обновить страницу)"
    if not files: return "❌ Файлы не выбраны"
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    saved_files = []
    
    for f in files:
        original_name = os.path.basename(f.name) if hasattr(f, 'name') else os.path.basename(f)
        saved_path = os.path.join(FILES_DIR, f"{int(time.time())}_{original_name}")
        import shutil
        src = f.name if hasattr(f, 'name') else f
        shutil.copy(src, saved_path)
        saved_files.append((saved_path, original_name))

    msg = []
    if merge_mode and len(saved_files) > 1:
        cursor.execute("INSERT INTO tasks (user_id, filename, status, created_at) VALUES (?, ?, ?, ?)",
                       (user_id, f"ПАКЕТ ({len(saved_files)} шт.)", "Очередь", datetime.now().strftime("%Y-%m-%d %H:%M")))
        task_id = cursor.lastrowid
        threading.Thread(target=process_merged_batch, args=(user_id, saved_files, model_size, task_id)).start()
        msg.append("Запущено объединение файлов...")
    else:
        for path, name in saved_files:
            cursor.execute("INSERT INTO tasks (user_id, filename, status, created_at) VALUES (?, ?, ?, ?)",
                           (user_id, name, "Очередь", datetime.now().strftime("%Y-%m-%d %H:%M")))
            task_id = cursor.lastrowid
            threading.Thread(target=process_single_file, args=(user_id, path, name, model_size, task_id)).start()
            msg.append(f"В очереди: {name}")
    conn.commit()
    conn.close()
    return "\n".join(msg)

def get_history(user_id):
    if not user_id: return []
    conn = sqlite3.connect(DB_PATH)
    tasks = conn.execute("SELECT created_at, filename, status, result_path FROM tasks WHERE user_id = ? ORDER BY id DESC LIMIT 20", (user_id,)).fetchall()
    conn.close()
    out = []
    for t in tasks:
        path = t[3] if (t[3] and os.path.exists(t[3])) else None
        out.append([t[0], t[1], t[2], path])
    return out

js_save_session = """(user_id) => { if (user_id) { localStorage.setItem("whisper_user_id", user_id); } return user_id; }"""
js_load_session = """() => { return localStorage.getItem("whisper_user_id"); }"""

with gr.Blocks(title="Whisper Pro", css=".login-btn { font-size: 20px; }", allowed_paths=[STORAGE_DIR]) as demo:
    session_token = gr.State("") 
    user_id_state = gr.State("") 
    
    with gr.Group(visible=True) as login_screen:
        gr.Markdown("# 👋 Добро пожаловать")
        login_html = gr.HTML()
        check_login_btn = gr.Button("🔄 Я вошел в Telegram", variant="primary")
    
    with gr.Group(visible=False) as cabinet_screen:
        with gr.Row():
            gr.Markdown("# 📂 Личный кабинет")
            logout_btn = gr.Button("Выйти", size="sm")
        with gr.Tabs():
            with gr.Tab("Загрузка"):
                file_in = gr.File(file_count="multiple", label="Файлы")
                with gr.Row():
                    model_in = gr.Dropdown(["small", "medium", "large"], value="small", label="Модель")
                    merge_in = gr.Checkbox(label="📎 Объединить результат в один DOCX файл", value=False)
                run_btn = gr.Button("🚀 Отправить в работу", variant="primary")
                run_out = gr.Textbox(label="Статус")
            with gr.Tab("История"):
                refresh_hist = gr.Button("🔄 Обновить список")
                hist_table = gr.Dataframe(headers=["Дата", "Файл", "Статус", "Скачать"], datatype=["str", "str", "str", "file"], interactive=False)

    def on_load():
        token, link = generate_login_link()
        html = f"""<div style="text-align: center; padding: 20px;"><a href="{link}" target="_blank" style="background-color: #2481cc; color: white; padding: 15px 30px; text-decoration: none; border-radius: 25px; font-size: 18px; font-family: sans-serif;">✈️ Войти через Telegram</a><p style="margin-top: 10px; color: #666;">Нажмите кнопку, запустите бота и возвращайтесь.</p></div>"""
        return token, html
    
    demo.load(on_load, outputs=[session_token, login_html]).then(fn=None, inputs=None, outputs=user_id_state, js=js_load_session).then(
        fn=lambda uid: (gr.update(visible=False), gr.update(visible=True)) if uid else (gr.update(visible=True), gr.update(visible=False)),
        inputs=[user_id_state], outputs=[login_screen, cabinet_screen]
    ).then(fn=get_history, inputs=[user_id_state], outputs=[hist_table])

    def try_login(token):
        uid = check_login_status(token)
        if uid: return uid, gr.update(visible=False), gr.update(visible=True)
        else: return "", gr.update(visible=True), gr.update(visible=False)

    check_login_btn.click(try_login, inputs=[session_token], outputs=[user_id_state, login_screen, cabinet_screen]).then(
        fn=None, inputs=[user_id_state], outputs=None, js=js_save_session
    ).then(fn=get_history, inputs=[user_id_state], outputs=[hist_table])

    run_btn.click(add_task, inputs=[user_id_state, file_in, model_in, merge_in], outputs=[run_out])
    refresh_hist.click(get_history, inputs=[user_id_state], outputs=[hist_table])
    logout_btn.click(lambda: (gr.update(visible=True), gr.update(visible=False), ""), outputs=[login_screen, cabinet_screen, user_id_state]).then(fn=None, js="() => localStorage.removeItem('whisper_user_id')")

demo.queue().launch(server_name="0.0.0.0", server_port=7860)