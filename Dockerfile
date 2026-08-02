FROM repo.asax.ir/python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ENABLE_BETA=false

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY discover_prefixes.py compare_es_mappings.py app.py prefixes.json ./
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh \
    && mkdir -p /app/results

EXPOSE 8501

# Defaults; overridden by compose command / env
ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["compare"]
