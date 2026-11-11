FROM python:3.13-alpine
WORKDIR /app
COPY app.py /app/app.py
COPY static /app/static
ENV RYADOM_DATA=/app/data \
    RYADOM_DOWNLOADS=/app/downloads \
    RYADOM_KEY_FILE=/rustdesk/id_ed25519.pub \
    PYTHONUNBUFFERED=1
EXPOSE 21114
HEALTHCHECK --interval=30s --timeout=5s CMD wget -qO- http://127.0.0.1:21114/panel/api/public >/dev/null || exit 1
CMD ["python3", "/app/app.py"]
