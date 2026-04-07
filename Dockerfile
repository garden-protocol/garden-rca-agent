FROM python:3.12-slim

# git is needed by study agent (git pull on executor repos)
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# knowledge/ is baked in from git at build time.
# At runtime, Coolify mounts a persistent volume over /app/knowledge
# so any /study/{chain} calls survive restarts and redeployments.
RUN mkdir -p /app/knowledge

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
