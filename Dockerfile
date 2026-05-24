FROM python:3.11-slim

WORKDIR /app

# Install uv
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates && rm -rf /var/lib/apt/lists/*
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

# Copy project files
COPY pyproject.toml ./
COPY hermes_trading ./hermes_trading

# Install dependencies
RUN uv sync

# State directory — Railway persistent volume mounts here
# Template state created at runtime by the trading loop
VOLUME /app/state
RUN mkdir -p /app/state

ENV HERMES_TRADING_MODE=paper
CMD ["uv", "run", "python", "-m", "hermes_trading"]
