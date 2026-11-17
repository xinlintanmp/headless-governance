FROM python:3.11-slim

# The Agent SDK ships a ~330MB `claude` binary that it spawns per session.
# It needs a libc-based image; alpine will not run it.
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1 \
    PORT=8000

EXPOSE 8000

# Render and similar hosts inject $PORT.
CMD ["sh", "-c", "uvicorn server:app --host 0.0.0.0 --port ${PORT:-8000}"]
