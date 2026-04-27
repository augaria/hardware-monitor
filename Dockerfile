FROM python:3.11-slim

WORKDIR /app

COPY central_server/ .

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir flask requests

# Git-based deps — busted by --refresh-deps / --no-cache via docker-build.sh
ARG CACHE_BUST=1
RUN pip install --no-cache-dir git+https://github.com/augaria/notifier.git

EXPOSE 5000

CMD ["python", "main.py"]
