# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS build
WORKDIR /src
RUN pip install --no-cache-dir build
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m build --wheel --outdir /dist

FROM python:3.12-slim
RUN useradd -r -u 1001 -m thruk
WORKDIR /app
COPY --from=build /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm -f /tmp/*.whl
USER thruk
ENV PYTHONUNBUFFERED=1
# Default = stdio transport (Docker MCP Gateway / Claude Desktop / LibreChat).
# For HTTP/Streamable-HTTP, override CMD: ["--listen", "8001"]
EXPOSE 8001
ENTRYPOINT ["thruk-mcp"]
CMD []
