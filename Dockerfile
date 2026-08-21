FROM python:3.12-slim
WORKDIR /app
COPY platform/requirements.txt platform/requirements.txt
RUN pip install --no-cache-dir -r platform/requirements.txt
COPY platform platform
COPY console console
ENV PYTHONPATH=/app/platform
EXPOSE 8080
CMD ["uvicorn", "mantis_platform.main:app", "--host", "0.0.0.0", "--port", "8080"]
