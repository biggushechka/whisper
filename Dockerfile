FROM python:3.10-slim

# Показываем логи сразу
ENV PYTHONUNBUFFERED=1

# Ставим FFmpeg
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Копируем и ставим библиотеки
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Копируем код
COPY app.py .

# Даем полные права на папку
RUN chmod -R 777 /app

EXPOSE 7860

CMD ["python", "app.py"]