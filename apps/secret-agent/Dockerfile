FROM python:3.11-alpine
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY apps/secret-agent/secret_agent.py .
CMD ["python3", "secret_agent.py"]
