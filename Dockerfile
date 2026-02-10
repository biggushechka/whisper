FROM python:3.10-slim

# Ставим системные библиотеки (ffmpeg нужен для Whisper)
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# Рабочая папка
WORKDIR /app

# --- МАГИЯ ЗДЕСЬ ---
# Создаем папки и ДАЕМ ПРАВА, чтобы не было ошибки Access Denied
RUN mkdir -p /app/storage /app/cache /app/storage/files && \
    chmod -R 777 /app/storage && \
    chmod -R 777 /app/cache

# Копируем список библиотек
COPY requirements.txt .
# Ставим библиотеки
RUN pip install --no-cache-dir -r requirements.txt

# Копируем сам код
COPY app.py .

# Настраиваем кэш для моделей Whisper (чтобы не качал каждый раз)
ENV XDG_CACHE_HOME=/app/cache

# Открываем порт
EXPOSE 7860

# Запускаем
CMD ["python", "app.py"]