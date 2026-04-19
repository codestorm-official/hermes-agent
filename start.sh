#!/bin/bash
set -e

mkdir -p /data/.hermes/sessions /data/.hermes/skills /data/.hermes/workspace /data/.hermes/pairing

# Merge the git-tracked seed config into the persisted config.yaml. The seed
# wins for everything except model.default and model.provider, which are
# owned at runtime by the admin dashboard and `hermes model` / `codex_login`
# respectively. envsubst fills any ${VAR} placeholders before the merge.
if [ -f /opt/hermes-config/config.seed.yaml ]; then
    envsubst < /opt/hermes-config/config.seed.yaml > /tmp/config.seed.rendered.yaml
    python /app/merge_config.py /tmp/config.seed.rendered.yaml /data/.hermes/config.yaml
fi

# PA bootstrap: seed SOUL.md / USER.md / MEMORY.md only if the target file
# is missing. Runtime edits (via the memory tool or `railway ssh`) always
# win over the seed, matching the config.seed.yaml philosophy.
for f in SOUL.md USER.md MEMORY.md; do
    if [ ! -f "/data/.hermes/$f" ] && [ -f "/opt/hermes-config/$f" ]; then
        cp "/opt/hermes-config/$f" "/data/.hermes/$f"
    fi
done

# Obsidian vault mount (2-way). When OBSIDIAN_VAULT_REPO_URL +
# OBSIDIAN_VAULT_GITHUB_TOKEN are set, clone the vault to /data/vault on
# first boot and keep it in sync with GitHub via a background loop:
#   - auto-commit any uncommitted local changes (safety net if the agent
#     forgot the commit ritual from SOUL.md). These fallback commits have
#     timestamp names, not semantic ones; agent-crafted commits remain the
#     primary path.
#   - pull --rebase --autostash keeps local work safe when Ari's Obsidian
#     Git plugin pushes new content.
#   - push origin HEAD ensures Hermes' commits land on GitHub.
# OBSIDIAN_VAULT_PATH is exported so the bundled note-taking/obsidian
# Hermes skill finds the vault.
if [ -n "${OBSIDIAN_VAULT_REPO_URL:-}" ] && [ -n "${OBSIDIAN_VAULT_GITHUB_TOKEN:-}" ]; then
    AUTHED_URL="${OBSIDIAN_VAULT_REPO_URL/https:\/\//https:\/\/x-access-token:${OBSIDIAN_VAULT_GITHUB_TOKEN}@}"
    if [ -d /data/vault/.git ]; then
        git -C /data/vault pull --rebase --autostash 2>&1 | sed 's/^/[vault-pull] /' \
            || git -C /data/vault rebase --abort 2>&1 | sed 's/^/[vault-pull] /' \
            || true
    else
        rm -rf /data/vault 2>/dev/null || true
        git clone --depth 50 "$AUTHED_URL" /data/vault 2>&1 | sed 's/^/[vault-clone] /' || true
    fi

    # Configure Hermes' git identity so his own commits are distinguishable
    # from Ari's in the vault's GitHub history.
    if [ -d /data/vault/.git ]; then
        git -C /data/vault config user.name  "Hermes PA"
        git -C /data/vault config user.email "hermes@ari-birnbaum"

        # Background sync loop - 60-second interval. Short interval is
        # chosen because the agent's SOUL Exit-Bedingung (commit before
        # answering Ari) is not reliably honoured by the model; the loop
        # is effectively the real commit mechanism. 60s keeps the
        # observable data-loss window tiny while still leaving semantic
        # room for agent-crafted commits in the cases where it does
        # commit. Errors swallowed so a transient network blip or rebase
        # conflict never crashes the gateway.
        (
            while true; do
                sleep 60
                if [ -n "$(git -C /data/vault status --porcelain 2>/dev/null)" ]; then
                    git -C /data/vault add -A 2>/dev/null || true
                    git -C /data/vault commit -m "Hermes: auto-commit pending changes $(date -u +%Y-%m-%dT%H:%M:%SZ)" >/dev/null 2>&1 || true
                fi
                git -C /data/vault pull --rebase --autostash >/dev/null 2>&1 \
                    || git -C /data/vault rebase --abort >/dev/null 2>&1 || true
                git -C /data/vault push origin HEAD >/dev/null 2>&1 || true
            done
        ) &
        echo "[vault] background sync loop started (PID $!)"
    fi
fi
export OBSIDIAN_VAULT_PATH="${OBSIDIAN_VAULT_PATH:-/data/vault}"

exec python /app/server.py
