FROM python:3.11-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8081 PYTHONPATH=/app:/app/apps
COPY apps/order-service/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY apps/order-service/app.py .
COPY apps/order-service/order_store.py .
COPY apps/common /app/common
USER 1001
EXPOSE 8081
CMD ["python3", "app.py"]
