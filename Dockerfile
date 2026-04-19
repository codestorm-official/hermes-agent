FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates git gettext-base && \
    rm -rf /var/lib/apt/lists/*

# Install hermes-agent as a package (gives us the `hermes` CLI entry point)
RUN git clone --depth 1 https://github.com/NousResearch/hermes-agent.git /tmp/hermes-agent && \
    cd /tmp/hermes-agent && \
    uv pip install --system --no-cache -e ".[all]" && \
    rm -rf /tmp/hermes-agent/.git

COPY requirements.txt /app/requirements.txt
RUN uv pip install --system --no-cache -r /app/requirements.txt

RUN mkdir -p /data/.hermes

# Git-tracked config seed + merge script. See hermes-config/config.seed.yaml
# and merge_config.py for the merge semantics. Seed wins for terminal/agent
# defaults; model.default and model.provider are preserved from the volume.
COPY hermes-config/config.seed.yaml /opt/hermes-config/config.seed.yaml
COPY merge_config.py /app/merge_config.py

# PA bootstrap: agent persona, user facts, memory scaffold. start.sh copies
# these into $HERMES_HOME only if the target file is missing, so runtime
# edits (via memory tools or railway ssh) always win over the seed.
COPY hermes-config/SOUL.md /opt/hermes-config/SOUL.md
COPY hermes-config/USER.md /opt/hermes-config/USER.md
COPY hermes-config/MEMORY.md /opt/hermes-config/MEMORY.md

# Helper script that invokes the Codex OAuth flow without the curses picker.
# Useful from `railway ssh` on Windows where arrow-key navigation is broken.
COPY codex_login.py /app/codex_login.py

COPY server.py /app/server.py
COPY templates/ /app/templates/
COPY graph-ingester/ /app/graph-ingester/
COPY graph-mcp/ /app/graph-mcp/
COPY start.sh /app/start.sh
RUN chmod +x /app/start.sh

ENV HOME=/data
ENV HERMES_HOME=/data/.hermes

CMD ["/app/start.sh"]
