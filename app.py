import gradio as gr
import os
import time
import threading
import queue
import sqlite3
import uuid
import telebot
import shutil
import re
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from datetime import datetime
from docx import Document

# --- НАСТРОЙКИ ---
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "8504609196:AAE-AXIpfytvvDigddCHMvTT9ukPp9m-SWw")
TG_BOT_USERNAME = os.environ.get("TG_BOT_USERNAME", "whisper_log_bot")
SITE_URL = os.environ.get("SITE_URL", "https://whisper.chernienko.pro") 

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

def db_update_status(task_id, status_str):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status_str, task_id))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error updating task status: {e}")

def unload_memory(obj=None):
    """Глубокая очистка памяти после задачи"""
    import gc
    if obj:
        del obj
    gc.collect()
    print("🧹 Память полностью очищена")

def send_file_to_tg(user_id, filepath, caption):
    try:
        if os.path.exists(filepath):
            with open(filepath, "rb") as f:
                bot.send_document(user_id, f, caption=caption)
    except Exception as e:
        print(f"Error sending file to TG: {e}")

# --- ФУНКЦИИ ОБРАБОТКИ ТРАНСКРИБАЦИИ ---
def process_single_file(user_id, file_path, original_name, model_size, task_id):
    model = None
    try:
        from faster_whisper import WhisperModel
        
        db_update_status(task_id, "⏳ Загрузка модели...")
        model = WhisperModel(model_size, device="cpu", compute_type="int8")
        
        db_update_status(task_id, "⏳ Расшифровка 0%")
        segments, info = model.transcribe(file_path, language="ru", beam_size=5)
        
        duration = info.duration
        full_text = []
        last_update = 0
        for s in segments:
            t_start = time.strftime("%M:%S", time.gmtime(s.start))
            full_text.append(f"[{t_start}] — {s.text.strip()}")
            
            curr_time = time.time()
            if duration > 0 and (curr_time - last_update > 3):
                percent = min(99, int((s.end / duration) * 100))
                status_str = f"⏳ Расшифровано {percent}% ({int(s.end)}/{int(duration)} сек)"
                db_update_status(task_id, status_str)
                last_update = curr_time
            
        db_update_status(task_id, "⏳ Создание документа...")
        doc = Document()
        doc.add_paragraph(f"Файл: {original_name}\nМодель: {model_size}\n\n" + "\n".join(full_text))
        
        res_path = os.path.join(FILES_DIR, f"Transcription_{int(time.time())}_{task_id}.docx")
        doc.save(res_path)
        
        db_update_status(task_id, "⏳ Отправка в Telegram...")
        
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE tasks SET status = ?, result_path = ? WHERE id = ?", ("✅ Готово", res_path, task_id))
        conn.commit()
        conn.close()
        
        send_file_to_tg(user_id, res_path, f"Готово: {original_name}")

    except Exception as e:
        import traceback
        print(f"Error in process_single_file: {e}")
        traceback.print_exc()
        db_update_status(task_id, f"❌ Ошибка: {str(e)[:40]}")
    finally:
        unload_memory(model)

def process_merged_batch(user_id, file_list, model_size, task_id):
    model = None
    try:
        from faster_whisper import WhisperModel
        
        db_update_status(task_id, "⏳ Загрузка модели...")
        model = WhisperModel(model_size, device="cpu", compute_type="int8")
        doc = Document()
        doc.add_paragraph(f"Сводный отчет (Файлов: {len(file_list)})")
        
        last_update = 0
        for idx, (f_path, f_name) in enumerate(file_list):
            doc.add_page_break()
            doc.add_heading(f"Файл: {f_name}", level=1)
            
            db_update_status(task_id, f"⏳ Файл {idx+1}/{len(file_list)}: {f_name}...")
            
            segments, info = model.transcribe(f_path, language="ru", beam_size=5)
            duration = info.duration
            for s in segments:
                t_start = time.strftime("%M:%S", time.gmtime(s.start))
                doc.add_paragraph(f"[{t_start}] — {s.text.strip()}")
                
                curr_time = time.time()
                if duration > 0 and (curr_time - last_update > 3):
                    file_progress = s.end / duration
                    overall_progress = (idx + file_progress) / len(file_list)
                    percent = min(99, int(overall_progress * 100))
                    status_str = f"⏳ Файл {idx+1}/{len(file_list)} — {percent}% ({int(s.end)}/{int(duration)} сек)"
                    db_update_status(task_id, status_str)
                    last_update = curr_time
        
        db_update_status(task_id, "⏳ Создание сводного отчета...")
        res_path = os.path.join(FILES_DIR, f"MERGED_{int(time.time())}_{task_id}.docx")
        doc.save(res_path)
        
        db_update_status(task_id, "⏳ Отправка в Telegram...")
        
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE tasks SET status = ?, result_path = ? WHERE id = ?", ("✅ Пакет готов", res_path, task_id))
        conn.commit()
        conn.close()
        
        send_file_to_tg(user_id, res_path, "🔥 Сводный отчет готов")
            
    except Exception as e:
        import traceback
        print(f"Error in process_merged_batch: {e}")
        traceback.print_exc()
        db_update_status(task_id, f"❌ Ошибка: {str(e)[:40]}")
    finally:
        unload_memory(model)

# --- ФОНОВАЯ ОЧЕРЕДЬ ЗАДАЧ ---
task_queue = queue.Queue()

def recover_pending_tasks():
    """Автоматически восстанавливает и перезапускает нерасшифрованные задачи при старте контейнера"""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT id, user_id, filename, status FROM tasks WHERE status LIKE '⏳%' OR status = 'Очередь' OR status LIKE '%Прервано%'")
        rows = c.fetchall()
        for task_id, user_id, original_name, status in rows:
            matched_file = None
            if os.path.exists(FILES_DIR):
                for fname in os.listdir(FILES_DIR):
                    if fname.endswith(original_name):
                        matched_file = os.path.join(FILES_DIR, fname)
                        break
            
            if matched_file and os.path.exists(matched_file):
                print(f"🔄 Автоматическое восстановление задачи {task_id}: {original_name}")
                c.execute("UPDATE tasks SET status = 'Очередь' WHERE id = ?", (task_id,))
                conn.commit()
                task_queue.put({
                    "type": "single",
                    "user_id": user_id,
                    "file_path": matched_file,
                    "original_name": original_name,
                    "model_size": "small",
                    "task_id": task_id
                })
            else:
                print(f"⚠️ Файл для задачи {task_id} не найден: {original_name}")
                c.execute("UPDATE tasks SET status = '❌ Файл не найден' WHERE id = ?", (task_id,))
                conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error in recover_pending_tasks: {e}")

def worker_loop():
    while True:
        try:
            task_info = task_queue.get()
            if task_info is None:
                break
            
            task_type = task_info.get("type")
            user_id = task_info.get("user_id")
            task_id = task_info.get("task_id")
            model_size = task_info.get("model_size")
            
            if task_type == "single":
                file_path = task_info.get("file_path")
                original_name = task_info.get("original_name")
                process_single_file(user_id, file_path, original_name, model_size, task_id)
            elif task_type == "batch":
                file_list = task_info.get("file_list")
                process_merged_batch(user_id, file_list, model_size, task_id)
                
            task_queue.task_done()
        except Exception as e:
            print(f"Error in background worker loop: {e}")
            time.sleep(2)

threading.Thread(target=worker_loop, daemon=True).start()
recover_pending_tasks()

# --- БОТ ---
def bot_polling():
    while True:
        try:
            bot.polling(none_stop=True, interval=2, timeout=20)
        except Exception as e:
            print(f"Bot polling exception: {e}")
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
    except Exception as e:
        print(f"Error in handle_start: {e}")

try:
    print("Clearing active webhooks to resolve 409 Conflict...")
    bot.remove_webhook()
except Exception as e:
    print(f"Failed to remove webhook: {e}")

threading.Thread(target=bot_polling, daemon=True).start()

def check_login_status(token):
    if not token: return None
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM login_sessions WHERE token=?", (token,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None

def add_task(user_id, files, model_size, merge_mode):
    if not user_id or not files: return "❌ Ошибка: Пользователь не авторизован или файлы не выбраны"
    
    saved_files = []
    for f in files:
        f_path = f.name if hasattr(f, 'name') else f
        safe_name = f"{int(time.time())}_{uuid.uuid4().hex[:4]}_{os.path.basename(f_path)}"
        saved_path = os.path.join(FILES_DIR, safe_name)
        shutil.copy(f_path, saved_path)
        saved_files.append((saved_path, os.path.basename(f_path)))

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
    if merge_mode and len(saved_files) > 1:
        cursor.execute("INSERT INTO tasks (user_id, filename, status, created_at) VALUES (?, ?, ?, ?)",
                       (user_id, f"ПАКЕТ ({len(saved_files)})", "Очередь", datetime.now().strftime("%Y-%m-%d %H:%M")))
        task_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        task_queue.put({
            "type": "batch",
            "user_id": user_id,
            "file_list": saved_files,
            "model_size": model_size,
            "task_id": task_id
        })
    else:
        conn.commit()
        conn.close()
        for path, name in saved_files:
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("INSERT INTO tasks (user_id, filename, status, created_at) VALUES (?, ?, ?, ?)",
                           (user_id, name, "Очередь", datetime.now().strftime("%Y-%m-%d %H:%M")))
            task_id = cursor.lastrowid
            conn.commit()
            conn.close()
            
            task_queue.put({
                "type": "single",
                "user_id": user_id,
                "file_path": path,
                "original_name": name,
                "model_size": model_size,
                "task_id": task_id
            })
            
    return "✅ Добавлено в очередь! Вы можете закрыть браузер, результаты придут в Telegram."

def format_status_progress_bar(status_text):
    """Преобразует текстовый статус в красивый горизонтальный HTML Progress Bar"""
    if not status_text:
        return ""
    
    match = re.search(r'(\d+)%', status_text)
    if match and ("Расшифровано" in status_text or "Файл" in status_text or "%" in status_text):
        percent = int(match.group(1))
        percent = max(0, min(100, percent))
        return f'''
        <div style="background:#2a2e39;border-radius:10px;width:100%;min-width:200px;height:26px;overflow:hidden;position:relative;box-shadow:inset 0 1px 3px rgba(0,0,0,0.5);">
          <div style="background:linear-gradient(90deg, #2481cc, #00d2ff);width:{percent}%;height:100%;transition:width 0.4s ease-in-out;"></div>
          <span style="position:absolute;top:0;left:0;width:100%;height:100%;text-align:center;line-height:26px;font-size:12px;font-weight:bold;color:#ffffff;text-shadow:0 1px 2px rgba(0,0,0,0.9);">{status_text}</span>
        </div>
        '''
    elif "Очередь" in status_text:
        return '''
        <div style="background:#3a321e;border-radius:10px;width:100%;min-width:200px;height:26px;line-height:26px;text-align:center;font-size:12px;font-weight:bold;color:#ffc107;box-shadow:inset 0 1px 3px rgba(0,0,0,0.3);">
          ⏳ В очереди...
        </div>
        '''
    elif "✅" in status_text:
        return f'''
        <div style="background:#1e3a29;border-radius:10px;width:100%;min-width:200px;height:26px;line-height:26px;text-align:center;font-size:12px;font-weight:bold;color:#4caf50;box-shadow:inset 0 1px 3px rgba(0,0,0,0.3);">
          {status_text}
        </div>
        '''
    elif "❌" in status_text:
        return f'''
        <div style="background:#3a1e1e;border-radius:10px;width:100%;min-width:200px;height:26px;line-height:26px;text-align:center;font-size:12px;font-weight:bold;color:#f44336;box-shadow:inset 0 1px 3px rgba(0,0,0,0.3);">
          {status_text}
        </div>
        '''
    else:
        return f'''
        <div style="background:#2a2e39;border-radius:10px;width:100%;min-width:200px;height:26px;line-height:26px;text-align:center;font-size:12px;font-weight:bold;color:#e0e0e0;">
          {status_text}
        </div>
        '''

def get_history(user_id):
    if not user_id: return []
    conn = sqlite3.connect(DB_PATH)
    tasks = conn.execute("SELECT created_at, filename, status, result_path FROM tasks WHERE user_id = ? ORDER BY id DESC LIMIT 15", (user_id,)).fetchall()
    conn.close()
    
    formatted = []
    for t in tasks:
        date_str = t[0]
        fname = t[1]
        raw_status = t[2]
        rpath = t[3]
        
        status_html = format_status_progress_bar(raw_status)
        
        if rpath and os.path.exists(rpath):
            file_link = f"📄 Готово ({os.path.basename(rpath)})"
        else:
            file_link = "—"
            
        formatted.append([date_str, fname, status_html, file_link])
        
    return formatted

def get_active_task_progress(user_id):
    """Возвращает прогресс-бар активной задачи для вкладки Загрузка"""
    if not user_id:
        return ""
    conn = sqlite3.connect(DB_PATH)
    task = conn.execute("SELECT filename, status FROM tasks WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
    conn.close()
    
    if not task:
        return ""
        
    fname, raw_status = task
    bar_html = format_status_progress_bar(raw_status)
    return f'''
    <div style="margin-top:15px;padding:12px;background:#1a1d24;border:1px solid #333948;border-radius:10px;">
      <div style="font-size:13px;font-weight:bold;color:#a0a6b8;margin-bottom:6px;">📊 Текущий статус транскрибации ({fname}):</div>
      {bar_html}
    </div>
    '''

# --- ИНТЕРФЕЙС GRADIO ---
session_script_html = """
<script>
(function() {
    function autoCheckSession() {
        try {
            const saved = localStorage.getItem("whisper_session_token");
            if (saved) {
                const btn = document.querySelector("#check_login_btn");
                if (btn) {
                    btn.click();
                }
            }
        } catch(e) { console.error("Auto check error:", e); }
    }

    setTimeout(autoCheckSession, 400);
    window.addEventListener("load", autoCheckSession);
})();
</script>
"""

with gr.Blocks(title="Whisper Pro") as demo:
    user_id_state = gr.State("")
    session_token = gr.State("")
    
    # Внедряем JS прямо в страницу
    gr.HTML(session_script_html, visible=True)
    
    with gr.Group(visible=True) as login_screen:
        gr.Markdown("# 👋 Вход")
        login_html = gr.HTML()
        check_login_btn = gr.Button("🔄 Проверить вход", elem_id="check_login_btn", variant="primary")
    
    with gr.Group(visible=False) as cabinet_screen:
        with gr.Row():
            gr.Markdown("# 📂 Кабинет")
            logout_btn = gr.Button("Выйти", elem_id="logout_btn", size="sm")
        with gr.Tabs():
            with gr.Tab("Загрузка"):
                file_in = gr.File(file_count="multiple", label="Аудио/Видео", file_types=["audio", "video", ".webm"])
                model_in = gr.Dropdown(["small", "medium"], value="small", label="Модель")
                merge_in = gr.Checkbox(label="Объединить в один файл", value=False)
                run_btn = gr.Button("🚀 Начать транскрибацию", variant="primary")
                run_out = gr.Textbox(label="Результат")
                live_progress = gr.HTML(label="Прогресс задачи")
            with gr.Tab("История"):
                refresh_btn = gr.Button("🔄 Обновить")
                hist_table = gr.Dataframe(
                    headers=["Дата", "Файл", "Прогресс / Статус", "Файл отчета"],
                    datatype=["str", "str", "html", "str"],
                    interactive=False
                )

    refresh_timer = gr.Timer(3)

    def on_load():
        token = str(uuid.uuid4())
        link = f"https://t.me/{TG_BOT_USERNAME}?start={token}"
        return token, f'<div style="text-align:center;padding:20px;"><a href="{link}" target="_blank" style="background:#2481cc;color:white;padding:15px 25px;text-decoration:none;border-radius:20px;font-weight:bold;">✈️ Войти через Telegram</a></div>'
    
    demo.load(on_load, outputs=[session_token, login_html])

    def try_login(token_param, current_user_id):
        # Если пользователь уже авторизован
        if current_user_id:
            return current_user_id, token_param, gr.update(visible=False), gr.update(visible=True)
            
        uid = check_login_status(token_param)
        if uid:
            return uid, token_param, gr.update(visible=False), gr.update(visible=True)
        return "", token_param, gr.update(visible=True), gr.update(visible=False)

    check_login_btn.click(
        try_login, 
        inputs=[session_token, user_id_state], 
        outputs=[user_id_state, session_token, login_screen, cabinet_screen],
        js="""(token, current_uid) => {
            const saved = localStorage.getItem("whisper_session_token");
            const targetToken = saved ? saved : token;
            if (targetToken) {
                try { localStorage.setItem("whisper_session_token", targetToken); } catch(e){}
            }
            return [targetToken, current_uid];
        }"""
    ).then(get_history, inputs=[user_id_state], outputs=[hist_table]).then(get_active_task_progress, inputs=[user_id_state], outputs=[live_progress])

    run_btn.click(add_task, inputs=[user_id_state, file_in, model_in, merge_in], outputs=[run_out]).then(get_active_task_progress, inputs=[user_id_state], outputs=[live_progress])
    
    refresh_btn.click(get_history, inputs=[user_id_state], outputs=[hist_table]).then(get_active_task_progress, inputs=[user_id_state], outputs=[live_progress])
    
    refresh_timer.tick(get_history, inputs=[user_id_state], outputs=[hist_table]).then(get_active_task_progress, inputs=[user_id_state], outputs=[live_progress])

    def do_logout():
        return "", "", gr.update(visible=True), gr.update(visible=False)

    logout_btn.click(
        do_logout, 
        outputs=[user_id_state, session_token, login_screen, cabinet_screen],
        js="() => { try { localStorage.removeItem('whisper_session_token'); } catch(e){} }"
    )

import uvicorn
from fastapi import FastAPI, UploadFile, File

custom_app = FastAPI()

@custom_app.post("/asr")
async def api_asr(audio_file: UploadFile = File(...)):
    temp_path = os.path.join(DATA_DIR, f"n8n_{audio_file.filename}")
    with open(temp_path, "wb") as buffer:
        shutil.copyfileobj(audio_file.file, buffer)
    
    try:
        from faster_whisper import WhisperModel
        model = WhisperModel("small", device="cpu", compute_type="int8")
        segments, info = model.transcribe(temp_path, language="ru", beam_size=5)
        text = "".join(s.text for s in segments)
        return {"text": text}
    finally:
        if os.path.exists(temp_path):
            os.remove(temp_path)

custom_app = gr.mount_gradio_app(custom_app, demo.queue(), path="/")

if __name__ == "__main__":
    uvicorn.run(custom_app, host="0.0.0.0", port=7860)
