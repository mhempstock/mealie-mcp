FROM python:3.12-slim

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY pyproject.toml .
COPY src/ src/

RUN uv pip install --system .

ENV MEALIE_URL=""
ENV MEALIE_API_TOKEN=""

EXPOSE 8000

CMD ["mealie-mcp"]
