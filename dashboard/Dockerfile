FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir poetry==1.8.2

COPY pyproject.toml poetry.lock ./
RUN poetry config virtualenvs.create false \
 && poetry install --extras gemini --no-root --no-interaction

COPY . .

EXPOSE 5000
CMD ["python", "dashboard/app.py"]
