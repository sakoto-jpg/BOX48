FROM python:3.10-slim

WORKDIR /app

# ffmpeg/aria2/mkvtoolnix + Intel VAAPI 驱动（i5-8250U 的 UHD 620 硬件加速）
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg aria2 mkvtoolnix \
        intel-media-va-driver vainfo && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install --no-cache-dir --upgrade yt-dlp

COPY ./app /app

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
