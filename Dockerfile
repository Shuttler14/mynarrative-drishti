FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev curl && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bake the rembg segmentation model into the image — local background removal
# is the primary path now (Replicate's hosted rembg gets stuck queueing for
# minutes, which silently disabled garment extraction and let raw on-model
# photos — including visible pants — go straight to VTON).
RUN python -c "from rembg import new_session; new_session('u2net')"

# Install drishti-observability from local package
COPY libs/py-observability /tmp/py-observability
RUN pip install --no-cache-dir /tmp/py-observability && rm -rf /tmp/py-observability

COPY . .

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && uvicorn api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
