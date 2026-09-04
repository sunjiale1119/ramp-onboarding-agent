# 爬坡 Ramp 运行镜像
#
# 为什么用容器：目标机是 CentOS 7.6，**连 python3 都没有**（只有 2.7），
# 而且 CentOS 7 已经 EOL，装新 Python 要么编译要么上第三方源，
# 两条路都会在这台机器上留下难以复现的状态。容器把这一层隔掉。
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 依赖单独一层：改代码不会触发重装依赖。
# 这台机器 2 核，装一次 langgraph + numpy 要几分钟，值得分层。
COPY deploy/requirements.txt .
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ \
        -r requirements.txt

COPY ramp/ ./ramp/
COPY seed/ ./seed/
COPY scripts/ ./scripts/

EXPOSE 8000

# 单 worker：这台机器 1.7G 内存，多 worker 每个都要各自加载
# 知识索引和向量，内存直接翻倍。演示场景并发量很低，一个够用。
CMD ["python", "-m", "uvicorn", "ramp.api:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
