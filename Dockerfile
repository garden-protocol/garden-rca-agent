FROM python:3.12-slim

# git is needed by entrypoint (clone/pull repos) and study agent (git pull)
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# knowledge/ is baked in from git at build time.
# At runtime, Coolify mounts a persistent volume over /app/knowledge
# so any /study/{chain} calls survive restarts and redeployments.
RUN mkdir -p /app/knowledge

# /opt/repos is mounted as a persistent volume by Coolify at runtime.
# The entrypoint clones/pulls all chain repos into it on every start.
RUN mkdir -p /opt/repos

EXPOSE 8000

ENTRYPOINT ["./docker-entrypoint.sh"]
