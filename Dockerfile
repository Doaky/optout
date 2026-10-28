FROM python:3.13-slim
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE NOTICE ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd -m optout
USER optout
ENV XDG_CONFIG_HOME=/config XDG_DATA_HOME=/data
# Unattended lane: email only. No browser is installed.
CMD ["sh", "-c", "while true; do optout brokers import >/dev/null 2>&1; optout daily; sleep 86400; done"]
