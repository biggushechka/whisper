FROM python:3.10-slim

# Устанавливаем системные зависимости
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# Рабочая папка
WORKDIR /app

# Копируем зависимости
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код
COPY app.py .

# Создаем папку для кэша моделей, чтобы не качать их каждый раз
ENV XDG_CACHE_HOME=/app/cache
RUN mkdir -p /app/cache

# Открываем порт Gradio
EXPOSE 7860

# Запускаем
CMD ["python", "app.py"]