FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py ./

EXPOSE 8000

CMD ["uvicorn", "bot:app", "--host", "0.0.0.0", "--port", "8000"]
