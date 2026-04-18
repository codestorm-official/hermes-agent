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

# Obsidian vault mount. When OBSIDIAN_VAULT_REPO_URL + OBSIDIAN_VAULT_GITHUB_TOKEN
# are set, clone the private vault repo to /data/vault on first boot and
# pull on subsequent boots. A background loop refreshes the vault every
# 15 min so changes made in Obsidian (pushed by the Obsidian Git plugin)
# propagate to the PA without a redeploy. OBSIDIAN_VAULT_PATH is exported
# so the bundled note-taking/obsidian Hermes skill finds the vault.
if [ -n "${OBSIDIAN_VAULT_REPO_URL:-}" ] && [ -n "${OBSIDIAN_VAULT_GITHUB_TOKEN:-}" ]; then
    AUTHED_URL="${OBSIDIAN_VAULT_REPO_URL/https:\/\//https:\/\/x-access-token:${OBSIDIAN_VAULT_GITHUB_TOKEN}@}"
    if [ -d /data/vault/.git ]; then
        git -C /data/vault pull --ff-only 2>&1 | sed 's/^/[vault-pull] /' || true
    else
        rm -rf /data/vault 2>/dev/null || true
        git clone --depth 50 "$AUTHED_URL" /data/vault 2>&1 | sed 's/^/[vault-clone] /' || true
    fi

    # Background pull loop - 15-minute interval. Errors swallowed so a transient
    # network blip never crashes the gateway.
    if [ -d /data/vault/.git ]; then
        (
            while true; do
                sleep 900
                git -C /data/vault pull --ff-only >/dev/null 2>&1 || true
            done
        ) &
        echo "[vault] background pull loop started (PID $!)"
    fi
fi
export OBSIDIAN_VAULT_PATH="${OBSIDIAN_VAULT_PATH:-/data/vault}"

exec python /app/server.py
