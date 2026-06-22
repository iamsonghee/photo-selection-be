FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .

# CPU-only torch wheel로 설치해 이미지 용량과 메모리 사용을 줄인다.
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
