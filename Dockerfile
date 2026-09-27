FROM python:3.12-slim

# Scripts commonly shell out to these; bash is not in slim by default.
RUN apt-get update && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application in (the image is self-contained; scripts are mounted).
COPY main.py ./
COPY dashboard.html ./
COPY partials/ ./partials/
COPY static/ ./static/

RUN mkdir -p /app/scripts /app/logs

# Run unprivileged. /app is chowned so SQLite, logs and uploaded scripts are writable.
RUN useradd --create-home --uid 1000 scriptrr \
    && chown -R scriptrr:scriptrr /app
USER scriptrr

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/v2/dashboard').status==200 else 1)"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
