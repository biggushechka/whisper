import gradio as gr
import os
import time
import threading
import sqlite3
import uuid
import telebot
import shutil
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from datetime import datetime
from docx import Document

# --- НАСТРОЙКИ ---
TG_BOT_TOKEN = "8504609196:AAE-AXIpfytvvDigddCHMvTT9ukPp9m-SWw"
TG_BOT_USERNAME = "whisper_log_bot"
SITE_URL = "https://whisper.chernienko.pro" 

DATA_DIR = "/data"
if not os.path.exists(DATA_DIR):
    DATA_DIR = "/app/data_local" 

DB_PATH = os.path.join(DATA_DIR, "users.db")
FILES_DIR = os.path.join(DATA_DIR, "files")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(FILES_DIR, exist_ok=True)

bot = telebot.TeleBot(TG_BOT_TOKEN)

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
            bot.polling(none_stop=True, interval=2, timeout=20)
        except Exception:
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
            bot.reply_to(message, "✅ Авторизовано! Вернитесь на сайт.")
    except Exception:
        pass

threading.Thread(target=bot_polling, daemon=True).start()

def check_login_status(token):
    if not token: return None
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM login_sessions WHERE token=?", (token,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None

def unload_memory(obj=None):
    """Глубокая очистка памяти после задачи"""
    import gc
    import torch
    if obj:
        del obj
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    gc.collect()
    print("🧹 Память полностью очищена")

def send_file_to_tg(user_id, filepath, caption):
    try:
        if os.path.exists(filepath):
            with open(filepath, "rb") as f:
                bot.send_document(user_id, f, caption=caption)
    except Exception:
        pass

# --- ТРАНСКРИБАЦИЯ ---
def process_single_file(user_id, file_path, original_name, model_size, task_id):
    model = None
    try:
        # Динамический импорт только при запуске задачи
        import whisper
        
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", ("⏳ Загрузка библиотек и модели...", task_id))
        conn.commit()
        conn.close()

        model = whisper.load_model(model_size)
        result = model.transcribe(file_path, language="Russian")
        
        full_text = []
        for s in result.get('segments', []):
            t_start = time.strftime("%M:%S", time.gmtime(s['start']))
            full_text.append(f"[{t_start}] — {s['text'].strip()}")
            
        doc = Document()
        doc.add_paragraph(f"Файл: {original_name}\nМодель: {model_size}\n\n" + "\n".join(full_text))
        
        res_path = os.path.join(FILES_DIR, f"Transcription_{int(time.time())}_{task_id}.docx")
        doc.save(res_path)
        
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE tasks SET status = ?, result_path = ? WHERE id = ?", ("✅ Готово", res_path, task_id))
        conn.commit()
        conn.close()
        send_file_to_tg(user_id, res_path, f"Готово: {original_name}")

    except Exception as e:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (f"❌ Ошибка: {str(e)[:40]}", task_id))
        conn.commit()
        conn.close()
    finally:
        unload_memory(model)

def process_merged_batch(user_id, file_list, model_size, task_id):
    model = None
    try:
        import whisper
        
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", ("⏳ Работа с пакетом...", task_id))
        conn.commit()
        conn.close()
        
        model = whisper.load_model(model_size)
        doc = Document()
        doc.add_paragraph(f"Сводный отчет (Файлов: {len(file_list)})")
        
        for f_path, f_name in file_list:
            doc.add_page_break()
            doc.add_heading(f"Файл: {f_name}", level=1)
            result = model.transcribe(f_path, language="Russian")
            for s in result.get('segments', []):
                t_start = time.strftime("%M:%S", time.gmtime(s['start']))
                doc.add_paragraph(f"[{t_start}] — {s['text'].strip()}")
        
        res_path = os.path.join(FILES_DIR, f"MERGED_{int(time.time())}_{task_id}.docx")
        doc.save(res_path)
        
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE tasks SET status = ?, result_path = ? WHERE id = ?", ("✅ Пакет готов", res_path, task_id))
        conn.commit()
        conn.close()
        send_file_to_tg(user_id, res_path, "🔥 Сводный отчет готов")
    finally:
        unload_memory(model)

def add_task(user_id, files, model_size, merge_mode):
    if not user_id or not files: return "❌ Ошибка"
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    saved_files = []
    
    for f in files:
        f_path = f.name if hasattr(f, 'name') else f
        safe_name = f"{int(time.time())}_{uuid.uuid4().hex[:4]}_{os.path.basename(f_path)}"
        saved_path = os.path.join(FILES_DIR, safe_name)
        shutil.copy(f_path, saved_path)
        saved_files.append((saved_path, os.path.basename(f_path)))

    if merge_mode and len(saved_files) > 1:
        cursor.execute("INSERT INTO tasks (user_id, filename, status, created_at) VALUES (?, ?, ?, ?)",
                       (user_id, f"ПАКЕТ ({len(saved_files)})", "Очередь", datetime.now().strftime("%Y-%m-%d %H:%M")))
        threading.Thread(target=process_merged_batch, args=(user_id, saved_files, model_size, cursor.lastrowid)).start()
    else:
        for path, name in saved_files:
            cursor.execute("INSERT INTO tasks (user_id, filename, status, created_at) VALUES (?, ?, ?, ?)",
                           (user_id, name, "Очередь", datetime.now().strftime("%Y-%m-%d %H:%M")))
            threading.Thread(target=process_single_file, args=(user_id, path, name, model_size, cursor.lastrowid)).start()
    
    conn.commit()
    conn.close()
    return "✅ Добавлено в очередь"

def get_history(user_id):
    if not user_id: return []
    conn = sqlite3.connect(DB_PATH)
    tasks = conn.execute("SELECT created_at, filename, status, result_path FROM tasks WHERE user_id = ? ORDER BY id DESC LIMIT 15", (user_id,)).fetchall()
    conn.close()
    return [[t[0], t[1], t[2], t[3] if (t[3] and os.path.exists(t[3])) else None] for t in tasks]

# --- ИНТЕРФЕЙС ---
with gr.Blocks(title="Whisper Pro") as demo:
    user_id_state = gr.State("")
    session_token = gr.State("")
    
    with gr.Group(visible=True) as login_screen:
        gr.Markdown("# 👋 Вход")
        login_html = gr.HTML()
        check_login_btn = gr.Button("🔄 Проверить вход", variant="primary")
    
    with gr.Group(visible=False) as cabinet_screen:
        with gr.Row():
            gr.Markdown("# 📂 Кабинет")
            logout_btn = gr.Button("Выйти", size="sm")
        with gr.Tabs():
            with gr.Tab("Загрузка"):
                file_in = gr.File(file_count="multiple", label="Аудио/Видео")
                model_in = gr.Dropdown(["small", "medium"], value="small", label="Модель")
                merge_in = gr.Checkbox(label="Объединить в один файл", value=False)
                run_btn = gr.Button("🚀 Начать транскрибацию", variant="primary")
                run_out = gr.Textbox(label="Результат")
            with gr.Tab("История"):
                refresh_btn = gr.Button("🔄 Обновить")
                hist_table = gr.Dataframe(headers=["Дата", "Файл", "Статус", "Путь"], interactive=False)

    def on_load():
        token = str(uuid.uuid4())
        link = f"https://t.me/{TG_BOT_USERNAME}?start={token}"
        return token, f'<div style="text-align:center;padding:20px;"><a href="{link}" target="_blank" style="background:#2481cc;color:white;padding:15px 25px;text-decoration:none;border-radius:20px;font-weight:bold;">✈️ Войти через Telegram</a></div>'
    
    demo.load(on_load, outputs=[session_token, login_html])

    def try_login(token):
        uid = check_login_status(token)
        if uid: return uid, gr.update(visible=False), gr.update(visible=True)
        return "", gr.update(visible=True), gr.update(visible=False)

    check_login_btn.click(try_login, inputs=[session_token], outputs=[user_id_state, login_screen, cabinet_screen]).then(get_history, inputs=[user_id_state], outputs=[hist_table])
    run_btn.click(add_task, inputs=[user_id_state, file_in, model_in, merge_in], outputs=[run_out])
    refresh_btn.click(get_history, inputs=[user_id_state], outputs=[hist_table])
    logout_btn.click(lambda: ("", gr.update(visible=True), gr.update(visible=False)), outputs=[user_id_state, login_screen, cabinet_screen])

from fastapi import FastAPI, UploadFile, File
import whisper

# --- ОТДЕЛЬНЫЙ ВХОД ДЛЯ n8n (НЕ ТРОГАЕТ САЙТ) ---
from fastapi import UploadFile, File

app = demo.app 

@app.post("/asr")
async def api_asr(audio_file: UploadFile = File(...)):
    import whisper
    # Сохраняем во временную папку, чтобы не мусорить
    temp_path = os.path.join(DATA_DIR, f"n8n_{audio_file.filename}")
    with open(temp_path, "wb") as buffer:
        shutil.copyfileobj(audio_file.file, buffer)
    
    try:
        model = whisper.load_model("small")
        result = model.transcribe(temp_path, language="Russian")
        # ВОЗВРАЩАЕМ ТЕКСТ ОБРАТНО В n8n
        return {"text": result["text"]}
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)
            
demo.queue().launch(server_name="0.0.0.0", server_port=7860, allowed_paths=[DATA_DIR])
