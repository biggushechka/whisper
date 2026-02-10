import gradio as gr
import whisper
import os
import time
from docx import Document

# Глобальная переменная для модели, чтобы не грузить её каждый раз
current_model = None
current_model_name = ""

def load_model_if_needed(model_size):
    global current_model, current_model_name
    if current_model is None or current_model_name != model_size:
        print(f"Загружаю модель {model_size}...")
        current_model = whisper.load_model(model_size)
        current_model_name = model_size
    return current_model

def transcribe_audio(audio_path, model_size):
    if audio_path is None:
        return None, "Файл не выбран!"

    try:
        # 1. Загрузка модели
        model = load_model_if_needed(model_size)

        # 2. Транскрибация
        print(f"Начинаю обработку файла: {audio_path}")
        # verbose=False чтобы не спамить в логи
        result = model.transcribe(audio_path, language="Russian")
        
        # 3. Формирование текста
        full_text = []
        text_preview = ""
        
        for segment in result.get('segments', []):
            start = time.strftime("%M:%S", time.gmtime(segment['start']))
            text = segment['text'].strip()
            line = f"[{start}] — {text}"
            full_text.append(line)
        
        final_text = "\n".join(full_text)
        
        # 4. Сохранение в DOCX
        # Создаем имя файла
        original_name = os.path.basename(audio_path)
        docx_filename = f"Transcription_{int(time.time())}.docx"
        
        doc = Document()
        doc.add_paragraph(f"Транскрипция: {original_name}")
        doc.add_paragraph(f"Модель: {model_size}")
        doc.add_paragraph("\n" + final_text)
        doc.save(docx_filename)
        
        return docx_filename, final_text

    except Exception as e:
        return None, f"Ошибка: {str(e)}"

# Интерфейс
with gr.Blocks(title="Whisper Web") as demo:
    gr.Markdown("# 🎙️ Whisper Web Transcriber")
    
    with gr.Row():
        with gr.Column():
            # Входные данные
            audio_input = gr.Audio(type="filepath", label="Загрузите аудио или видео")
            model_dropdown = gr.Dropdown(
                choices=["tiny", "base", "small", "medium", "large"], 
                value="small", 
                label="Модель (Medium - точнее, но дольше)"
            )
            submit_btn = gr.Button("🚀 Запустить", variant="primary")
        
        with gr.Column():
            # Выходные данные
            output_file = gr.File(label="Скачать DOCX")
            output_text = gr.Textbox(label="Предпросмотр текста", lines=10)

    # Логика кнопки
    submit_btn.click(
        fn=transcribe_audio, 
        inputs=[audio_input, model_dropdown], 
        outputs=[output_file, output_text]
    )

# Запуск сервера на всех интерфейсах (важно для Docker)
demo.queue().launch(server_name="0.0.0.0", server_port=7860)