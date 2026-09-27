# Use a slim Python 3.12 image
FROM python:3.12-slim

# Set the working directory
WORKDIR /app

# Install system dependencies (optional, but good if your scripts need git or curl)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements list
COPY requirements.txt .

# Sanitize inputs and install requirements
RUN apt-get update && \
    sed -i 's/\r$//' requirements.txt && \
    while IFS= read -r pkg || [ -n "$pkg" ]; do \
      [ -z "$pkg" ] && continue; \
      if echo "$pkg" | grep -q '^pip-'; then \
        name="${pkg#pip-}"; \
        echo "📦 Installing pip package: $name"; \
        pip install --no-cache-dir "$name" || { echo "❌ pip install failed: $name"; exit 1; }; \
      elif echo "$pkg" | grep -q '^apt-'; then \
        name="${pkg#apt-}"; \
        echo "🔧 Installing apt package: $name"; \
        apt-get install -y "$name" || { echo "❌ apt install failed: $name"; exit 1; }; \
      else \
        echo "❓ Unknown package type in requirements.txt: $pkg" && exit 1; \
      fi; \
    done < requirements.txt && \
    rm -rf /var/lib/apt/lists/*

# Install only what the user explicitly requests in requirements.txt

# Install required Python packages
RUN pip install --no-cache-dir fastapi uvicorn jinja2 apscheduler

# Copy your application files
#COPY main.py .
#COPY dashboard.html .

# Create a volume-ready directory for scripts and logs
#RUN mkdir -p /app/scripts /app/logs

# Expose the app port
EXPOSE 8000

# Start the application
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]