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
import subprocess
from datetime import datetime
from docx import Document
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton
from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import FileResponse
import uvicorn

# --- НАСТРОЙКИ ---
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "8504609196:AAE-AXIpfytvvDigddCHMvTT9ukPp9m-SWw")
TG_BOT_USERNAME = os.environ.get("TG_BOT_USERNAME", "whisper_log_bot")
SITE_URL = os.environ.get("SITE_URL", "https://whisper.chernienko.pro")

DATA_DIR = "/data"
if not os.path.exists(DATA_DIR):
    DATA_DIR = "/app/data_local"

DB_PATH = os.path.join(DATA_DIR, "users.db")
FILES_DIR = os.path.join(DATA_DIR, "files")
AUDIO_TEMP_DIR = os.path.join(DATA_DIR, "audio_cache")

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(FILES_DIR, exist_ok=True)
os.makedirs(AUDIO_TEMP_DIR, exist_ok=True)

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

def db_update_status(task_id, status_str, result_path=None):
    try:
        conn = sqlite3.connect(DB_PATH)
        if result_path:
            conn.execute("UPDATE tasks SET status = ?, result_path = ? WHERE id = ?", (status_str, result_path, task_id))
        else:
            conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status_str, task_id))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error updating task status: {e}")

def unload_memory(obj=None):
    import gc
    if obj:
        del obj
    gc.collect()

def send_file_to_tg(user_id, filepath, caption):
    try:
        if os.path.exists(filepath):
            with open(filepath, "rb") as f:
                bot.send_document(user_id, f, caption=caption)
    except Exception as e:
        print(f"Error sending file to TG: {e}")

def extract_audio(input_file_path):
    """
    Мгновенно извлекает аудиодорожку через ffmpeg в 16kHz mono WAV для Whisper.
    """
    base_name = os.path.splitext(os.path.basename(input_file_path))[0]
    out_wav = os.path.join(AUDIO_TEMP_DIR, f"{base_name}_{uuid.uuid4().hex[:6]}.wav")
    
    cmd = [
        "ffmpeg", "-y", "-i", input_file_path,
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-c:a", "pcm_s16le",
        out_wav
    ]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        if os.path.exists(out_wav) and os.path.getsize(out_wav) > 0:
            return out_wav
    except Exception as e:
        print(f"ffmpeg extraction fallback: {e}")
    return input_file_path

# --- ОБРАБОТКА ТРАНСКРИБАЦИИ ---
def process_single_file(user_id, file_path, original_name, model_size, task_id):
    model = None
    extracted_audio = None
    try:
        from faster_whisper import WhisperModel

        db_update_status(task_id, "⏳ 1/3 Извлечение звуковой дорожки...")
        extracted_audio = extract_audio(file_path)

        db_update_status(task_id, f"⏳ 2/3 Инициализация модели ({model_size})...")
        model = WhisperModel(model_size, device="cpu", compute_type="int8", cpu_threads=4)

        db_update_status(task_id, "⏳ 3/3 Расшифровка: 0%")
        segments, info = model.transcribe(
            extracted_audio,
            language="ru",
            beam_size=1,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500),
            condition_on_previous_text=False,
            repetition_penalty=1.2,
            no_speech_threshold=0.6
        )

        duration = info.duration
        full_text = []
        last_update = 0
        for s in segments:
            t_start = time.strftime("%M:%S", time.gmtime(s.start))
            text_chunk = s.text.strip()
            if text_chunk:
                full_text.append(f"[{t_start}] — {text_chunk}")

            curr_time = time.time()
            if duration > 0 and (curr_time - last_update > 3):
                percent = min(99, int((s.end / duration) * 100))
                status_str = f"⏳ 3/3 Расшифровано {percent}% ({int(s.end)}/{int(duration)} сек)"
                db_update_status(task_id, status_str)
                last_update = curr_time

        db_update_status(task_id, "⏳ Формирование Word-документа...")
        doc = Document()
        if full_text:
            content_str = "\n".join(full_text)
            caption = f"✅ Готово ({model_size}): {original_name}"
            final_status = "✅ Готово"
        else:
            content_str = "⚠️ В аудиофайле не обнаружена речь (тишина, фоновый шум или отключенный микрофон)."
            caption = f"⚠️ Готово (речь не обнаружена): {original_name}"
            final_status = "✅ Готово (тишина)"

        doc.add_paragraph(f"Файл: {original_name}\nМодель: {model_size}\nДата: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n" + content_str)

        res_filename = f"Transcription_{int(time.time())}_{task_id}.docx"
        res_path = os.path.join(FILES_DIR, res_filename)
        doc.save(res_path)

        db_update_status(task_id, "⏳ Отправка в Telegram...", res_path)
        send_file_to_tg(user_id, res_path, caption)
        db_update_status(task_id, final_status, res_path)

    except Exception as e:
        import traceback
        print(f"Error in process_single_file: {e}")
        traceback.print_exc()
        db_update_status(task_id, f"❌ Ошибка: {str(e)[:40]}")
    finally:
        if extracted_audio and extracted_audio != file_path and os.path.exists(extracted_audio):
            try:
                os.remove(extracted_audio)
            except Exception:
                pass
        unload_memory(model)

def process_merged_batch(user_id, file_list, model_size, task_id):
    model = None
    try:
        from faster_whisper import WhisperModel

        db_update_status(task_id, f"⏳ Инициализация модели ({model_size})...")
        model = WhisperModel(model_size, device="cpu", compute_type="int8", cpu_threads=4)
        doc = Document()
        doc.add_paragraph(f"Сводный отчет (Файлов: {len(file_list)})\nДата: {datetime.now().strftime('%Y-%m-%d %H:%M')}")

        last_update = 0
        for idx, (f_path, f_name) in enumerate(file_list):
            doc.add_page_break()
            doc.add_heading(f"Файл {idx+1}/{len(file_list)}: {f_name}", level=1)

            db_update_status(task_id, f"⏳ Обработка {idx+1}/{len(file_list)}: {f_name}...")
            extracted = extract_audio(f_path)

            segments, info = model.transcribe(
                extracted,
                language="ru",
                beam_size=1,
                vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=500),
                condition_on_previous_text=False,
                repetition_penalty=1.2,
                no_speech_threshold=0.6
            )
            duration = info.duration
            file_has_text = False
            for s in segments:
                t_start = time.strftime("%M:%S", time.gmtime(s.start))
                text_chunk = s.text.strip()
                if text_chunk:
                    doc.add_paragraph(f"[{t_start}] — {text_chunk}")
                    file_has_text = True

                curr_time = time.time()
                if duration > 0 and (curr_time - last_update > 3):
                    file_progress = s.end / duration
                    overall_progress = (idx + file_progress) / len(file_list)
                    percent = min(99, int(overall_progress * 100))
                    status_str = f"⏳ Файл {idx+1}/{len(file_list)} — {percent}% ({int(s.end)}/{int(duration)} сек)"
                    db_update_status(task_id, status_str)
                    last_update = curr_time

            if not file_has_text:
                doc.add_paragraph("⚠️ В аудиофайле не обнаружена речь (тишина или отключенный микрофон).")

            if extracted != f_path and os.path.exists(extracted):
                try:
                    os.remove(extracted)
                except Exception:
                    pass

        db_update_status(task_id, "⏳ Создание сводного отчета...")
        res_filename = f"MERGED_{int(time.time())}_{task_id}.docx"
        res_path = os.path.join(FILES_DIR, res_filename)
        doc.save(res_path)

        db_update_status(task_id, "⏳ Отправка в Telegram...", res_path)
        send_file_to_tg(user_id, res_path, "🔥 Сводный отчет готов")
        db_update_status(task_id, "✅ Пакет готов", res_path)

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
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute("SELECT id, user_id, filename, status FROM tasks WHERE status LIKE '⏳%' OR status = 'Очередь'")
        rows = c.fetchall()
        for task_id, user_id, original_name, status in rows:
            matched_file = None
            if os.path.exists(FILES_DIR):
                for fname in os.listdir(FILES_DIR):
                    if fname.endswith(original_name):
                        matched_file = os.path.join(FILES_DIR, fname)
                        break

            if matched_file and os.path.exists(matched_file):
                print(f"🔄 Восстановление задачи {task_id}: {original_name}")
                c.execute("UPDATE tasks SET status = 'Очередь' WHERE id = ?", (task_id,))
                conn.commit()
                task_queue.put({
                    "type": "single",
                    "user_id": user_id,
                    "file_path": matched_file,
                    "original_name": original_name,
                    "model_size": "medium",
                    "task_id": task_id
                })
            else:
                c.execute("UPDATE tasks SET status = '❌ Прервано' WHERE id = ?", (task_id,))
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
            model_size = task_info.get("model_size", "medium")

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

# --- TELEGRAM BOT ---
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
            
            cabinet_url = f"{SITE_URL}/?token={login_token}"
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("🚀 Открыть личный кабинет", url=cabinet_url))
            bot.reply_to(
                message,
                "✅ **Вход успешно подтверждён!**\n\n"
                "Ваш браузер уже перешёл в личный кабинет. Вы также можете нажать кнопку ниже, чтобы открыть кабинет на любом устройстве:",
                reply_markup=markup,
                parse_mode="Markdown"
            )
        else:
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("🌐 Открыть сайт Whisper Pro", url=SITE_URL))
            bot.reply_to(message, f"👋 Привет! Отправьте мне аудио или голосовое сообщение (до 20 МБ), либо перейдите на сайт {SITE_URL} для файлов любого размера (до 2 ГБ).", reply_markup=markup)
    except Exception as e:
        print(f"Error in handle_start: {e}")

@bot.message_handler(content_types=['audio', 'voice', 'video', 'document'])
def handle_incoming_file(message):
    try:
        user_id = str(message.chat.id)
        file_info = None
        file_name = "audio_file"

        if message.audio:
            file_info = bot.get_file(message.audio.file_id)
            file_name = message.audio.file_name or f"audio_{int(time.time())}.mp3"
        elif message.voice:
            file_info = bot.get_file(message.voice.file_id)
            file_name = f"voice_{int(time.time())}.ogg"
        elif message.video:
            file_info = bot.get_file(message.video.file_id)
            file_name = message.video.file_name or f"video_{int(time.time())}.mp4"
        elif message.document:
            file_info = bot.get_file(message.document.file_id)
            file_name = message.document.file_name or f"file_{int(time.time())}"

        if not file_info:
            bot.reply_to(message, "⚠️ Не удалось получить информацию о файле.")
            return

        bot.reply_to(message, f"📥 Файл «{file_name}» получен! Добавлен в очередь на транскрибацию (модель: medium)...")

        downloaded_bytes = bot.download_file(file_info.file_path)
        safe_name = f"{int(time.time())}_{uuid.uuid4().hex[:4]}_{file_name}"
        saved_path = os.path.join(FILES_DIR, safe_name)

        with open(saved_path, 'wb') as new_file:
            new_file.write(downloaded_bytes)

        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO tasks (user_id, filename, status, created_at) VALUES (?, ?, ?, ?)",
                       (user_id, file_name, "Очередь", datetime.now().strftime("%Y-%m-%d %H:%M")))
        task_id = cursor.lastrowid
        conn.commit()
        conn.close()

        task_queue.put({
            "type": "single",
            "user_id": user_id,
            "file_path": saved_path,
            "original_name": file_name,
            "model_size": "medium",
            "task_id": task_id
        })

    except Exception as e:
        import traceback
        print(f"Error in handle_incoming_file: {e}")
        traceback.print_exc()
        if "too big" in str(e).lower() or "file is too big" in str(e).lower():
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("🌐 Загрузить на сайте (до 2 ГБ)", url=SITE_URL))
            bot.reply_to(
                message,
                "⚠️ **Файл превышает лимит Telegram (20 МБ)**\n\n"
                f"Telegram ограничивает прямую отправку боту файлами до 20 МБ. Загрузите файл через наш сайт {SITE_URL} — там поддерживаются любые файлы до 2 ГБ, а готовый .docx отчёт сразу придёт сюда в Telegram!",
                reply_markup=markup,
                parse_mode="Markdown"
            )
        else:
            bot.reply_to(message, f"❌ Ошибка при приеме файла: {str(e)[:50]}")

def check_login_status(token):
    if not token: return None
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM login_sessions WHERE token=?", (token,))
    result = cursor.fetchone()
    conn.close()
    return result[0] if result else None

# --- ХЕЛПЕРЫ И СТАТУСЫ ---
def add_task(user_id, files, model_size, merge_mode):
    if not user_id:
        return "❌ Ошибка: Вы не авторизованы. Войдите через Telegram."
    if not files or len(files) == 0:
        return "⚠️ Выберите один или несколько аудио/видео файлов перед отправкой."

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
                       (user_id, f"ПАКЕТ ({len(saved_files)} файлов)", "Очередь", datetime.now().strftime("%Y-%m-%d %H:%M")))
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

    return f"🚀 Добавлено в обработку ({len(saved_files)} шт.)! Вы можете закрыть страницу — результат придёт в Telegram."

def format_status_progress_bar(status_text):
    if not status_text:
        return ""

    match = re.search(r'(\d+)%', status_text)
    if match and ("Расшифрован" in status_text or "Файл" in status_text or "%" in status_text):
        percent = int(match.group(1))
        percent = max(0, min(100, percent))
        return f'''
        <div style="background:#1e2330;border:1px solid #2d3748;border-radius:12px;width:100%;min-width:220px;height:28px;overflow:hidden;position:relative;box-shadow:inset 0 2px 4px rgba(0,0,0,0.5);">
          <div style="background:linear-gradient(90deg, #3b82f6, #06b6d4, #10b981);width:{percent}%;height:100%;transition:width 0.4s ease-in-out;box-shadow:0 0 12px rgba(6,182,212,0.5);"></div>
          <span style="position:absolute;top:0;left:0;width:100%;height:100%;text-align:center;line-height:28px;font-size:12px;font-weight:700;color:#ffffff;text-shadow:0 1px 3px rgba(0,0,0,0.9);">{status_text}</span>
        </div>
        '''
    elif "Очередь" in status_text or "Извлечение" in status_text or "Инициализация" in status_text:
        return f'''
        <div style="background:#2d2616;border:1px solid #78350f;border-radius:12px;width:100%;min-width:220px;height:28px;line-height:28px;text-align:center;font-size:12px;font-weight:700;color:#fbbf24;box-shadow:inset 0 1px 3px rgba(0,0,0,0.3);">
          {status_text}
        </div>
        '''
    elif "✅" in status_text or "Готово" in status_text:
        return f'''
        <div style="background:#132e1e;border:1px solid #166534;border-radius:12px;width:100%;min-width:220px;height:28px;line-height:28px;text-align:center;font-size:12px;font-weight:700;color:#4ade80;box-shadow:inset 0 1px 3px rgba(0,0,0,0.3);">
          {status_text}
        </div>
        '''
    elif "❌" in status_text or "Ошибка" in status_text or "Прервано" in status_text:
        return f'''
        <div style="background:#331919;border:1px solid #991b1b;border-radius:12px;width:100%;min-width:220px;height:28px;line-height:28px;text-align:center;font-size:12px;font-weight:700;color:#f87171;box-shadow:inset 0 1px 3px rgba(0,0,0,0.3);">
          {status_text}
        </div>
        '''
    else:
        return f'''
        <div style="background:#1e2330;border:1px solid #2d3748;border-radius:12px;width:100%;min-width:220px;height:28px;line-height:28px;text-align:center;font-size:12px;font-weight:700;color:#cbd5e1;">
          {status_text}
        </div>
        '''

def get_history(user_id):
    if not user_id: return []
    conn = sqlite3.connect(DB_PATH)
    tasks = conn.execute("SELECT created_at, filename, status, result_path FROM tasks WHERE user_id = ? ORDER BY id DESC LIMIT 20", (user_id,)).fetchall()
    conn.close()

    formatted = []
    for t in tasks:
        date_str = t[0]
        fname = t[1]
        raw_status = t[2]
        rpath = t[3]

        status_html = format_status_progress_bar(raw_status)

        if rpath and os.path.exists(rpath):
            basename = os.path.basename(rpath)
            download_url = f"/download/{basename}"
            file_link = f'<a href="{download_url}" target="_blank" download style="display:inline-block;background:#2563eb;color:#ffffff;font-size:12px;font-weight:600;padding:5px 12px;border-radius:8px;text-decoration:none;box-shadow:0 2px 4px rgba(0,0,0,0.2);">📥 Скачать .docx</a>'
        else:
            file_link = '<span style="color:#64748b;">—</span>'

        formatted.append([date_str, fname, status_html, file_link])

    return formatted

def get_active_task_progress(user_id):
    if not user_id:
        return ""
    conn = sqlite3.connect(DB_PATH)
    task = conn.execute("SELECT id, filename, status, result_path, created_at FROM tasks WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,)).fetchone()
    conn.close()

    if not task:
        return ""

    task_id, fname, raw_status, rpath, created_at = task
    bar_html = format_status_progress_bar(raw_status)

    download_button_html = ""
    if rpath and os.path.exists(rpath):
        basename = os.path.basename(rpath)
        download_button_html = f'''
        <div style="margin-top:10px;text-align:right;">
          <a href="/download/{basename}" target="_blank" download style="display:inline-block;background:linear-gradient(135deg, #10b981, #059669);color:#ffffff;font-weight:700;font-size:13px;padding:8px 16px;border-radius:8px;text-decoration:none;box-shadow:0 3px 6px rgba(0,0,0,0.3);">📥 Скачать готовый DOCX</a>
        </div>
        '''

    return f'''
    <div style="background:rgba(30, 41, 59, 0.7);border:1px solid rgba(59, 130, 246, 0.3);backdrop-filter:blur(10px);border-radius:14px;padding:16px;margin-bottom:16px;box-shadow:0 8px 24px rgba(0,0,0,0.35);">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;">
        <div style="font-size:14px;font-weight:700;color:#e2e8f0;display:flex;align-items:center;gap:6px;">
          <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:#38bdf8;box-shadow:0 0 8px #38bdf8;"></span>
          Текущая задача: <span style="color:#60a5fa;">{fname}</span>
        </div>
        <span style="font-size:12px;color:#94a3b8;">{created_at}</span>
      </div>
      {bar_html}
      {download_button_html}
    </div>
    '''

def build_login_html(token):
    link = f"https://t.me/{TG_BOT_USERNAME}?start={token}"
    return f'''
    <div style="text-align:center;padding:30px 10px;">
      <a href="{link}" target="_blank" style="background:linear-gradient(135deg, #2481cc, #0088cc);color:white;padding:16px 32px;text-decoration:none;border-radius:14px;font-weight:700;font-size:16px;display:inline-flex;align-items:center;gap:10px;box-shadow:0 6px 18px rgba(36,129,204,0.4);">
        ✈️ Войти через Telegram-бота (@{TG_BOT_USERNAME})
      </a>
      <p style="color:#94a3b8;font-size:13px;margin-top:14px;">Нажмите кнопку, нажмите «Start» в боте. Вход выполнится <b>автоматически</b>.</p>
    </div>
    '''

# --- GRADIO ИНТЕРФЕЙС ---
custom_css = """
body {
    background-color: #0b0f19 !important;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif !important;
}
.gradio-container {
    max-width: 1000px !important;
    margin: auto !important;
    padding-top: 20px !important;
}
.header-badge {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    background: rgba(30, 41, 59, 0.8);
    border: 1px solid rgba(255, 255, 255, 0.1);
    padding: 6px 14px;
    border-radius: 20px;
    font-size: 13px;
    color: #94a3b8;
    margin-bottom: 12px;
}
.status-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: #10b981;
    box-shadow: 0 0 8px #10b981;
}
.btn-primary {
    background: linear-gradient(135deg, #2563eb, #1d4ed8) !important;
    border: none !important;
    color: white !important;
    font-weight: 700 !important;
    font-size: 15px !important;
    border-radius: 10px !important;
    box-shadow: 0 4px 14px rgba(37, 99, 235, 0.4) !important;
    transition: all 0.2s ease-in-out !important;
}
.btn-primary:hover {
    transform: translateY(-1px) !important;
    box-shadow: 0 6px 20px rgba(37, 99, 235, 0.6) !important;
}
"""

with gr.Blocks(title="Whisper Pro — Студия транскрибации", css=custom_css) as demo:
    user_id_state = gr.State("")
    session_token = gr.State("")

    with gr.Row():
        gr.HTML("""
        <div style="display:flex;justify-content:space-between;align-items:center;width:100%;margin-bottom:15px;">
          <div>
            <h1 style="font-size:24px;font-weight:800;color:#f8fafc;margin:0 0 4px 0;">🎙 Whisper Pro Studio</h1>
            <p style="font-size:13px;color:#94a3b8;margin:0;">Сверхточная транскрибация аудио и видео с автоматической отправкой в Telegram</p>
          </div>
          <div class="header-badge">
            <span class="status-dot"></span>
            <span>Сервис онлайн</span>
          </div>
        </div>
        """)

    with gr.Group(visible=True) as login_screen:
        gr.Markdown("### 👋 Авторизация")
        gr.Markdown("Для доступа к системе и автоматической отправки расшифровок в ваш Telegram:")
        login_html = gr.HTML()

    with gr.Group(visible=False) as cabinet_screen:
        with gr.Row():
            gr.Markdown("### 📂 Личный кабинет")
            logout_btn = gr.Button("🚪 Выйти", elem_id="logout_btn", size="sm")

        with gr.Tabs():
            with gr.Tab("🚀 Новая транскрибация"):
                live_progress = gr.HTML(label="Статус")
                file_in = gr.File(
                    file_count="multiple",
                    label="📁 Выберите или перетащите файлы (MP4, WEBM, MP3, OGG, WAV, M4A, MOV до 2 ГБ)",
                    file_types=["audio", "video", ".webm", ".mp4", ".ogg", ".mp3", ".wav", ".m4a", ".mov", ".mkv"]
                )
                with gr.Row():
                    model_in = gr.Dropdown(["medium", "small"], value="medium", label="🧠 Модель нейросети (medium — максимальная точность, small — быстрее)")
                    merge_in = gr.Checkbox(label="📑 Объединить несколько файлов в один итоговый .docx отчет", value=False)
                run_btn = gr.Button("🚀 Запустить транскрибацию", elem_classes=["btn-primary"], size="lg")
                run_out = gr.Textbox(label="Уведомление", interactive=False, lines=2)

            with gr.Tab("📜 История и файлы"):
                refresh_btn = gr.Button("🔄 Обновить список")
                hist_table = gr.Dataframe(
                    headers=["Дата", "Файл", "Статус / Прогресс", "Скачать документ"],
                    datatype=["str", "str", "html", "html"],
                    interactive=False
                )

    refresh_timer = gr.Timer(2)

    # 1. Загрузка страницы: проверка токена из URL или localStorage через JS
    def on_page_load(client_token):
        if client_token:
            uid = check_login_status(client_token)
            if uid:
                history = get_history(uid)
                active_prog = get_active_task_progress(uid)
                return uid, client_token, gr.update(visible=False), gr.update(visible=True), history, active_prog, ""

        new_token = str(uuid.uuid4())
        html = build_login_html(new_token)
        return "", new_token, gr.update(visible=True), gr.update(visible=False), [], "", html

    demo.load(
        on_page_load,
        inputs=[session_token],
        outputs=[user_id_state, session_token, login_screen, cabinet_screen, hist_table, live_progress, login_html],
        js="""() => {
            try {
                const params = new URLSearchParams(window.location.search);
                const urlToken = params.get('token');
                const savedToken = localStorage.getItem('whisper_session_token');
                const token = urlToken || savedToken || '';
                if (urlToken) {
                    try { localStorage.setItem('whisper_session_token', urlToken); } catch(e){}
                    window.history.replaceState({}, document.title, window.location.pathname);
                }
                return token;
            } catch(e) { return ''; }
        }"""
    )

    # 2. Авто-проверка входа по таймеру: если пользователь подтвердил вход в Telegram, сразу переводим в кабинет
    def check_auto_login(current_uid, current_token):
        if current_uid:
            return current_uid, current_token, gr.update(), gr.update()
        if current_token:
            uid = check_login_status(current_token)
            if uid:
                return uid, current_token, gr.update(visible=False), gr.update(visible=True)
        return "", current_token, gr.update(), gr.update()

    refresh_timer.tick(
        check_auto_login,
        inputs=[user_id_state, session_token],
        outputs=[user_id_state, session_token, login_screen, cabinet_screen]
    ).then(
        fn=lambda token: token,
        inputs=[session_token],
        outputs=[],
        js="""(token) => {
            if (token) {
                try { localStorage.setItem('whisper_session_token', token); } catch(e){}
            }
        }"""
    ).then(
        get_history,
        inputs=[user_id_state],
        outputs=[hist_table]
    ).then(
        get_active_task_progress,
        inputs=[user_id_state],
        outputs=[live_progress]
    )

    run_btn.click(
        add_task,
        inputs=[user_id_state, file_in, model_in, merge_in],
        outputs=[run_out]
    ).then(get_active_task_progress, inputs=[user_id_state], outputs=[live_progress]).then(get_history, inputs=[user_id_state], outputs=[hist_table])

    refresh_btn.click(get_history, inputs=[user_id_state], outputs=[hist_table]).then(get_active_task_progress, inputs=[user_id_state], outputs=[live_progress])

    # 3. Выход
    def do_logout():
        new_token = str(uuid.uuid4())
        html = build_login_html(new_token)
        return "", new_token, gr.update(visible=True), gr.update(visible=False), html

    logout_btn.click(
        do_logout,
        outputs=[user_id_state, session_token, login_screen, cabinet_screen, login_html],
        js="() => { try { localStorage.removeItem('whisper_session_token'); } catch(e){} }"
    )

# --- FASTAPI ПРИЛОЖЕНИЕ ---
custom_app = FastAPI(title="Whisper Pro API")

@custom_app.get("/download/{filename}")
async def download_file(filename: str):
    safe_name = os.path.basename(filename)
    file_path = os.path.join(FILES_DIR, safe_name)
    if not os.path.exists(file_path):
        raise HTTPException(status_code=404, detail="Файл не найден")
    return FileResponse(
        file_path,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        filename=safe_name
    )

@custom_app.post("/asr")
async def api_asr(audio_file: UploadFile = File(...)):
    temp_path = os.path.join(DATA_DIR, f"n8n_{audio_file.filename}")
    with open(temp_path, "wb") as buffer:
        shutil.copyfileobj(audio_file.file, buffer)

    extracted_path = None
    try:
        from faster_whisper import WhisperModel
        extracted_path = extract_audio(temp_path)
        model = WhisperModel("medium", device="cpu", compute_type="int8", cpu_threads=4)
        segments, info = model.transcribe(
            extracted_path,
            language="ru",
            beam_size=1,
            vad_filter=True,
            vad_parameters=dict(min_silence_duration_ms=500),
            condition_on_previous_text=False,
            repetition_penalty=1.2,
            no_speech_threshold=0.6
        )
        text = "".join(s.text for s in segments)
        return {"text": text}
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.remove(temp_path)
            except Exception:
                pass
        if extracted_path and extracted_path != temp_path and os.path.exists(extracted_path):
            try:
                os.remove(extracted_path)
            except Exception:
                pass

custom_app = gr.mount_gradio_app(custom_app, demo.queue(), path="/")

def start_background_services():
    if getattr(start_background_services, "_started", False):
        return
    start_background_services._started = True
    print("🚀 Starting background worker loop & recovering tasks...")
    threading.Thread(target=worker_loop, daemon=True).start()
    recover_pending_tasks()
    try:
        print("Clearing active webhooks...")
        bot.remove_webhook()
    except Exception as e:
        print(f"Failed to remove webhook: {e}")
    print("🤖 Starting Telegram bot polling...")
    threading.Thread(target=bot_polling, daemon=True).start()

if __name__ == "__main__":
    start_background_services()
    uvicorn.run(custom_app, host="0.0.0.0", port=7860)
