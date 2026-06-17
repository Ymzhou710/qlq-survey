# Dockerfile — 支持部署到任何支持 Docker 的云平台
# (Render, Fly.io, Railway, 阿里云容器服务 等)

FROM python:3.12-slim

WORKDIR /app

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用文件
COPY survey_app.py .
COPY QLQ-Combined-Survey.html .

# 数据目录
RUN mkdir -p /data
ENV DATA_DIR=/data

# 云端部署时设置 BASE_URL 环境变量为实际域名
# ENV BASE_URL=https://your-app.onrender.com/

EXPOSE 8080
ENV PORT=8080

CMD ["python", "survey_app.py"]
