FROM python:3.12-slim
# tesseract-ocr: the Scan feature (photo → slip OCR) needs the binary
RUN apt-get update && apt-get install -y --no-install-recommends tesseract-ocr && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN python manage.py migrate && python manage.py collectstatic --noinput
EXPOSE 8000
CMD ["sh", "-c", "python manage.py migrate --no-input && gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 2"]
