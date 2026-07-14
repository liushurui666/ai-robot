FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf

ARG NANOBOT_VERSION=0.2.2

RUN groupadd --gid 1000 nanobot \
    && useradd --uid 1000 --gid 1000 --create-home nanobot \
    && pip install --no-cache-dir "nanobot-ai==${NANOBOT_VERSION}"

WORKDIR /opt/feishu-reminder
COPY pyproject.toml ./
COPY src ./src
RUN PYTHONPATH=src python -m reminder_mcp.patch_nanobot \
    && pip install --no-cache-dir .

USER nanobot
ENV HOME=/home/nanobot \
    REMINDER_DB_PATH=/home/nanobot/.nanobot/reminder/reminder.db

ENTRYPOINT ["nanobot"]
CMD ["gateway", "--foreground"]
