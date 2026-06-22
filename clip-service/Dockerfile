FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .

# CPU-only torch/torchvision 휠을 같은 인덱스에서 함께 설치 (버전 불일치 시
# "operator torchvision::nms does not exist" 같은 ABI 에러가 발생한다).
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch torchvision \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
