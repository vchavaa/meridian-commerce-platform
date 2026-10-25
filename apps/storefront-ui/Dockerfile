FROM python:3.11-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080 PYTHONPATH=/app:/app/apps
COPY apps/storefront-ui/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY apps/storefront-ui/app.py .
COPY apps/common /app/common
USER 1001
EXPOSE 8080
CMD ["python3", "app.py"]
