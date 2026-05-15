# Adaptive Trading System — Docker image
# Target: Windows containers (MT5 requires Windows) or Linux for headless services.
#
# Build:
#   docker build -t adaptive-trading .
#
# Run live loop (needs MT5 on host — use docker-compose):
#   docker run -v D:\future\data:/app/data adaptive-trading main.py live
#
# Run daily ops:
#   docker run -v D:\future\data:/app/data adaptive-trading main.py daily-ops

FROM python:3.11-slim

LABEL org.adaptive-trading.version="0.1.0"
LABEL org.adaptive-trading.description="Institution-grade adaptive quantitative trading system"

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY pyproject.toml .
RUN pip install --no-cache-dir \
    pydantic>=2.7,<3.0 \
    pyyaml>=6.0.1,<7.0.0 \
    pandas>=2.2,<3.0 \
    numpy>=1.26,<3.0 \
    onnxruntime>=1.18,<2.0 \
    orjson>=3.10,<4.0 \
    tenacity>=8.2,<9.0 \
    structlog>=24.1,<25.0 \
    typer>=0.12,<1.0 \
    rich>=13.7,<14.0 \
    scikit-learn>=1.5,<2.0 \
    joblib>=1.4,<2.0 \
    lightgbm>=4.3,<5.0 \
    xgboost>=2.1,<3.0

# Copy application code
COPY core/ ./core/
COPY apps/ ./apps/
COPY configs/ ./configs/
COPY scripts/ ./scripts/
COPY main.py .

# Data directory is mounted at runtime (contains models, training data, P&L ledger, etc.)
RUN mkdir -p /app/data

# Default command
CMD ["python", "main.py", "status"]
