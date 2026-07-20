FROM python:3.11-slim

WORKDIR /app

# mediapipe가 의존하는 opencv-contrib-python이 libGL을 필요로 함(opencv-python-headless는
# 필요 없지만, mediapipe 설치 시 함께 끌려오는 opencv-contrib-python 때문에 필요).
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# CPU-only torch/torchvision 휠을 같은 인덱스에서 함께 설치 (버전 불일치 시
# "operator torchvision::nms does not exist" 같은 ABI 에러가 발생한다).
RUN pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch torchvision \
    && pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
