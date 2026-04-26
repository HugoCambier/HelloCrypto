FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir \
    anthropic \
    flask \
    gunicorn \
    requests \
    python-dotenv \
    google-genai \
    google-auth-oauthlib \
    google-cloud-firestore

COPY . .

EXPOSE 8080
CMD ["gunicorn", "-b", "0.0.0.0:8080", "--threads", "4", "--timeout", "0", "dashboard.app:app"]
