FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY static ./static
COPY bench/tasks.jsonl bench/run_bench.py bench/__init__.py ./bench/
COPY bench/results ./bench/results
COPY runs/featured ./runs/featured
COPY examples ./examples
COPY docs ./docs
# The server keeps the API key; model-written code runs as this separate unprivileged uid,
# so it can't read the server's environment through /proc.
RUN useradd -M -u 10001 -s /usr/sbin/nologin sandbox
ENV LADDER_SANDBOX_UID=10001
EXPOSE 8000
CMD ["uvicorn", "app.server:app", "--host", "0.0.0.0", "--port", "8000"]
