FROM python:3.10-slim

ENV PYTHONUNBUFFERED=1
# Переменные для оптимизации памяти
ENV PYTHONMALLOC=malloc
ENV MALLOC_TRIM_THRESHOLD_=100000
ENV HF_HOME=/data/hf_cache


RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

RUN mkdir -p /data && chmod -R 777 /data

EXPOSE 7860

CMD ["python", "app.py"]