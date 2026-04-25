FROM python:3.10-slim

# 设置工作目录
WORKDIR /app

# 安装 ffmpeg (yt-dlp 合并视频和音频必须依赖它)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# 先复制并安装稳定的依赖库 (利用 Docker 缓存加速构建)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 强制拉取最新版的 yt-dlp (构建时加上 --no-cache 参数可确保这一步永远执行)
RUN pip install --no-cache-dir --upgrade yt-dlp

# 复制业务代码
COPY ./app /app

# 启动 FastAPI 服务
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]