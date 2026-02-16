FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1
# Ставим FFmpeg и необходимые системные библиотеки
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/* [cite: 2]

WORKDIR /app

COPY requirements.txt .
# Установка зависимостей без кэша для экономии места
RUN pip install --no-cache-dir -r requirements.txt 

COPY app.py .

# Папка /data должна быть доступна для записи (монтируется в Dokploy)
RUN mkdir -p /data && chmod -R 777 /data [cite: 4]

EXPOSE 7860

CMD ["python", "app.py"]