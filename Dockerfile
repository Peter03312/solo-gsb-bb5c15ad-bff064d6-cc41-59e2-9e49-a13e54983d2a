FROM python:3.11-slim

# 不生成 .pyc、不缓冲 stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TRACE_DB_PATH=/data/trace.db \
    TRACE_HOST=0.0.0.0 \
    TRACE_PORT=8080

WORKDIR /app

# 先装依赖，利用层缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 拷贝应用代码
COPY app ./app

# 持久化 SQLite 的数据目录
RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8080

# 简单健康检查：进程起来后即可查询
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/v1/health', timeout=2).status==200 else 1)"

# 容器启动时 uvicorn 会在 lifespan 中自动建表初始化数据库
CMD ["sh", "-c", "uvicorn app.main:app --host ${TRACE_HOST} --port ${TRACE_PORT}"]
