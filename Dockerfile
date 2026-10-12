FROM node:22-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends python3 ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci && npx playwright install --with-deps chromium
COPY guardian ./guardian
COPY static ./static
COPY scripts ./scripts
COPY demo ./demo
RUN useradd --create-home --uid 10001 guardian && mkdir -p /app/data && chown -R guardian:guardian /app/data /root/.cache/ms-playwright && mv /root/.cache/ms-playwright /opt/playwright
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright GUARDIAN_CONTAINER=1 PYTHONUNBUFFERED=1
USER guardian
EXPOSE 8787
CMD ["python3", "-m", "guardian", "serve", "--config", "/app/config.json"]
