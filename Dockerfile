FROM ghcr.io/astral-sh/uv:debian-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV LANG=en_US.UTF-8
ENV PATH="/app/.venv/bin:$PATH"

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        build-essential \
        bash \
        git \
        curl \
        ca-certificates \
        locales \
        unzip \
        ffmpeg && \
    locale-gen en_US.UTF-8 && \
    # rclone kur
    curl -fsSL https://rclone.org/install.sh | bash && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .
RUN uv lock
RUN uv sync --locked
RUN chmod +x start.sh
CMD ["bash", "start.sh"]
