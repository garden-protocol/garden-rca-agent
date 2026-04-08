#!/bin/sh
set -e

# Start Discord bot in background
python discord_bot.py &

# Start FastAPI server in foreground (container lifetime tied to this)
exec python -m uvicorn main:app --host 0.0.0.0 --port 8080
