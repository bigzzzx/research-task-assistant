# syntax=docker/dockerfile:1

# 1) 用 Vite 构建前端静态资源
FROM node:22-alpine AS web
WORKDIR /web
ENV npm_config_update_notifier=false npm_config_fund=false npm_config_audit=false
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# 2) 运行后端，并由同一个进程提供上一步构建的前端
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    LANG=C.UTF-8

WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install -r requirements.txt

COPY backend ./backend
COPY scripts ./scripts
COPY data ./data
COPY --from=web /web/dist ./frontend/dist

# 运行用户与数据目录：SQLite、上传文件和任务日志都落在 /app/var
RUN groupadd --gid 10001 app \
    && useradd --create-home --uid 10001 --gid app --shell /usr/sbin/nologin app \
    && mkdir -p /app/var \
    && chown -R app:app /app/var
USER app

EXPOSE 8767
HEALTHCHECK --interval=15s --timeout=3s --start-period=30s --retries=4 \
    CMD ["python", "-c", "import socket; socket.create_connection(('127.0.0.1', 8767), 3).close()"]

# 只监听容器内的 127.0.0.1 之外的地址，便于端口映射；应用本身仍按 Host 头只接受 localhost/127.0.0.1。
CMD ["python", "-m", "uvicorn", "backend.app.api:app", "--host", "0.0.0.0", "--port", "8767", "--timeout-graceful-shutdown", "3"]
