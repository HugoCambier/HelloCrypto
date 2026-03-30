FROM python:3.11-slim

WORKDIR /app

# Install Poetry
RUN pip install --no-cache-dir poetry==1.8.2

# Install dependencies (layer-cached)
COPY pyproject.toml poetry.lock* ./
RUN poetry config virtualenvs.create false \
 && poetry lock --no-update \
 && poetry install --extras gemini --no-root --no-interaction

# Copy source
COPY . .

ENTRYPOINT ["python", "runner/main.py"]
CMD ["--mode", "real"]
