FROM repo.asax.ir/python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# ENV PIP_INDEX_URL=https://repo.asax.ir/repository/pypi-group1/simple \
#     PIP_TRUSTED_HOST=repo.asax.ir

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY discover_prefixes.py compare_es_mappings.py app.py prefixes.json ./
COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh \
    && mkdir -p /app/results

EXPOSE 8080

# Defaults; overridden by compose command / env
ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["compare"]
