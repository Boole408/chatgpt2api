FROM python:3.10-slim

ENV OMP_NUM_THREADS=3 OPENBLAS_NUM_THREADS=3 MKL_NUM_THREADS=3 PYTHONUNBUFFERED=1
WORKDIR /worker

RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir numpy==1.26.4 pillow==10.4.0 \
    && pip install --no-cache-dir typing_extensions==4.12.2 \
    && pip install --no-cache-dir torch==2.1.2 torchvision==0.16.2 --index-url https://download.pytorch.org/whl/cpu --no-deps \
    && pip install --no-cache-dir realesrgan==0.3.0 opencv-python==4.10.0.84 numpy==1.26.4

RUN mkdir -p /models && python -c 'import urllib.request; urllib.request.urlretrieve("https://github.com/xinntao/Real-ESRGAN/releases/download/v0.2.1/RealESRGAN_x2plus.pth", "/models/RealESRGAN_x2plus.pth")'
COPY worker.py /worker/worker.py

EXPOSE 8091
CMD ["python", "/worker/worker.py"]
