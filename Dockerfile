FROM python:3.12-slim

# ffmpeg decodes mp3/m4a/aac uploads; the slim image has no codecs of its own.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py .
COPY engine ./engine
COPY public ./public

ENV PYTHONUNBUFFERED=1
EXPOSE 8765

# --no-warm keeps the container up immediately; the first analysis pays the
# numba compile cost once per container.
CMD ["python", "server.py", "--host", "0.0.0.0", "--no-browser", "--no-warm"]