FROM python:3.12-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && useradd --system --uid 10001 monitor \
    && install -d -o monitor -g monitor /data
COPY app.py storage.py sources.py sync.py model.py ./
COPY static/ static/
COPY templates/ templates/
USER monitor
ENV BOOSTER_DB=/data/boosters.sqlite3
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
