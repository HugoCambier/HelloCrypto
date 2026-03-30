FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir \
    anthropic \
    flask \
    requests \
    python-dotenv \
    google-genai \
    google-auth-oauthlib \
    google-cloud-firestore

COPY . .

EXPOSE 5000
CMD ["python", "dashboard/app.py"]
