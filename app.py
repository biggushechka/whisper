import gradio as gr
import whisper
import os
import time
import threading
import requests
import sqlite3
import uuid
import telebot # Новая библиотека
from datetime import datetime
from docx import Document

# --- НАСТРОЙКИ (ЗАПОЛНИ ЭТО!) ---
TG_BOT_TOKEN = "8504609196:AAE-AXIpfytvvDigddCHMvTT9ukPp9m-SWw"  
TG_BOT_USERNAME = "whisper_log_bot" # Например: Sergey_Whisper_Bot (не ссылка, просто имя)
STORAGE_DIR = "/app/storage"

# --- Инициализация ---
DB_PATH = os.path.join(STORAGE_DIR, "users.db")
FILES_DIR = os.path.join(STORAGE_DIR, "files")
os.makedirs(FILES_DIR, exist_ok=True)

bot = telebot.TeleBot(TG_BOT_TOKEN)

# Глобальная модель
current_model = None
current_model_name = ""

# --- БАЗА ДАННЫХ ---
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Таблица сессий (связь: секретный_код <-> telegram_id)
    c.execute('''CREATE TABLE IF NOT EXISTS login_sessions
                 (token TEXT PRIMARY KEY, user_id TEXT, created_at REAL)''')
    # Таблица задач
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

# Обработка команды /start токен
@bot.message_handler(commands=['start'])
def handle_start(message):
    try:
        # Получаем текст после /start (это наш токен)
        args = message.text.split()
        if len(args) > 1:
            login_token = args[1]
            user_id = str(message.chat.id)
            
            # Записываем в базу, что этот токен принадлежит этому юзеру
            conn = sqlite3.connect(DB_PATH)
            conn.execute("INSERT OR REPLACE INTO login_sessions (token, user_id, created_at) VALUES (?, ?, ?)", 
                         (login_token, user_id, time.time()))
            conn.commit()
            conn.close()
            
            bot.reply_to(message, "✅ Вы успешно авторизованы! Вернитесь на сайт.")
        else:
            bot.reply_to(message, "Привет! Это бот для транскрибации. Используйте сайт для работы.")
    except Exception as e:
        print(e)

# Запускаем бота в отдельном потоке
threading.Thread(target=bot_polling, daemon=True).start()


# --- ЛОГИКА АВТОРИЗАЦИИ НА САЙТЕ ---
def generate_login_link():
    # Генерируем уникальный токен
    token = str(uuid.uuid4())
    # Ссылка вида: https://t.me/MyBot?start=token
    link = f"https://t.me/{TG_BOT_USERNAME}?start={token}"
    return token, link

def check_login_status(token):
    if not token: return None
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM login_sessions WHERE token=?", (token,))
    result = cursor.fetchone()
    conn.close()
    
    if result:
        return result[0] # Возвращаем user_id
    return None

# --- ТРАНСКРИБАЦИЯ (ФОНОВАЯ) ---
def load_model(model_size):
    global current_model, current_model_name
    if current_model_name != model_size:
        current_model = whisper.load_model(model_size)
        current_model_name = model_size
    return current_model

def process_file_background(user_id, file_path, original_name, model_size, task_id):
    try:
        # Обновляем статус
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
        
        # Успех
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE tasks SET status = ?, result_path = ? WHERE id = ?", 
                     ("✅ Готово", res_path, task_id))
        conn.commit()
        conn.close()
        
        # Шлем файл в личку
        with open(res_path, "rb") as f:
            bot.send_document(user_id, f, caption=f"Ваша расшифровка: {original_name}")
        
    except Exception as e:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (f"❌ Ошибка: {str(e)}", task_id))
        conn.commit()
        conn.close()
        bot.send_message(user_id, f"Ошибка с файлом {original_name}: {e}")

def add_task(user_id, files, model_size):
    if not files: return "Файлы не выбраны"
    conn = sqlite3.connect(DB_PATH)
    msg = []
    for f in files:
        original_name = os.path.basename(f.name)
        saved_path = os.path.join(FILES_DIR, f"{int(time.time())}_{original_name}")
        import shutil
        shutil.copy(f.name, saved_path)
        
        cursor = conn.cursor()
        cursor.execute("INSERT INTO tasks (user_id, filename, status, created_at) VALUES (?, ?, ?, ?)",
                       (user_id, original_name, "Очередь", datetime.now().strftime("%Y-%m-%d %H:%M")))
        task_id = cursor.lastrowid
        conn.commit()
        
        threading.Thread(target=process_file_background, 
                         args=(user_id, saved_path, original_name, model_size, task_id)).start()
        msg.append(f"В очереди: {original_name}")
    conn.close()
    return "\n".join(msg)

def get_history(user_id):
    conn = sqlite3.connect(DB_PATH)
    tasks = conn.execute("SELECT created_at, filename, status, result_path FROM tasks WHERE user_id = ? ORDER BY id DESC LIMIT 20", (user_id,)).fetchall()
    conn.close()
    out = []
    for t in tasks:
        path = t[3] if (t[3] and os.path.exists(t[3])) else None
        out.append([t[0], t[1], t[2], path])
    return out

# --- ИНТЕРФЕЙС ---
with gr.Blocks(title="Whisper Pro", css=".login-btn { font-size: 20px; }") as demo:
    
    # ПЕРЕМЕННЫЕ СОСТОЯНИЯ
    session_token = gr.State("") # Секретный код для входа
    user_id_state = gr.State("") # ID юзера после входа

    # 1. ЭКРАН ВХОДА
    with gr.Group(visible=True) as login_screen:
        gr.Markdown("# 👋 Добро пожаловать")
        gr.Markdown("Нажмите кнопку ниже, чтобы войти через Telegram.")
        
        # Кнопка-ссылка (HTML)
        login_html = gr.HTML()
        
        # Скрытая кнопка для проверки статуса
        check_login_btn = gr.Button("🔄 Я нажал 'Запустить', проверить вход", variant="primary")
        login_status_text = gr.Textbox(label="Статус", interactive=False)

    # 2. КАБИНЕТ
    with gr.Group(visible=False) as cabinet_screen:
        with gr.Row():
            gr.Markdown("# 📂 Личный кабинет")
            logout_btn = gr.Button("Выйти", size="sm")
        
        with gr.Tabs():
            with gr.Tab("Загрузка"):
                file_in = gr.File(file_count="multiple", label="Файлы")
                model_in = gr.Dropdown(["small", "medium", "large"], value="small", label="Модель")
                run_btn = gr.Button("🚀 Отправить в работу", variant="primary")
                run_out = gr.Textbox(label="Инфо")
            
            with gr.Tab("История"):
                refresh_hist = gr.Button("🔄 Обновить")
                hist_table = gr.Dataframe(headers=["Дата", "Файл", "Статус", "Скачать"], interactive=False)

    # --- СОБЫТИЯ ---

    # При открытии страницы генерируем ссылку
    def on_load():
        token, link = generate_login_link()
        # Красивая HTML кнопка
        html = f"""
        <div style="text-align: center; padding: 20px;">
            <a href="{link}" target="_blank" style="background-color: #2481cc; color: white; padding: 15px 30px; text-decoration: none; border-radius: 25px; font-size: 18px; font-family: sans-serif;">
                ✈️ Войти через Telegram
            </a>
            <p style="margin-top: 10px; color: #666;">(Откроется Telegram, нажмите кнопку <b>Start</b>)</p>
        </div>
        """
        return token, html
    
    demo.load(on_load, outputs=[session_token, login_html])

    # Проверка входа
    def try_login(token):
        uid = check_login_status(token)
        if uid:
            return uid, gr.update(visible=False), gr.update(visible=True) # Вход успешен
        else:
            return "", gr.update(visible=True), gr.update(visible=False) # Еще ждем

    check_login_btn.click(try_login, inputs=[session_token], outputs=[user_id_state, login_screen, cabinet_screen])

    # Запуск задачи
    run_btn.click(add_task, inputs=[user_id_state, file_in, model_in], outputs=[run_out])

    # История
    refresh_hist.click(get_history, inputs=[user_id_state], outputs=[hist_table])
    
    # Выход
    logout_btn.click(lambda: (gr.update(visible=True), gr.update(visible=False)), outputs=[login_screen, cabinet_screen])

demo.queue().launch(server_name="0.0.0.0", server_port=7860)