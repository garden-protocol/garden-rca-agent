#!/bin/sh
set -e

# Start Discord bot in background (only if token is set)
if [ -n "$DISCORD_BOT_TOKEN" ]; then
  python discord_bot.py &
fi

# Start Slack bot in background (only if both tokens are set)
if [ -n "$SLACK_BOT_TOKEN" ] && [ -n "$SLACK_APP_TOKEN" ]; then
  python slack_bot.py &
fi

# Start FastAPI server in foreground (container lifetime tied to this)
exec python -m uvicorn main:app --host 0.0.0.0 --port 8080
