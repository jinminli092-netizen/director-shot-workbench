FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app
RUN mkdir -p /app/data

ENV SEEDANCE_HOST=0.0.0.0 \
    SEEDANCE_PORT=8765 \
    SEEDANCE_DATA_DIR=/app/data

EXPOSE 8765
CMD ["python", "app.py"]
