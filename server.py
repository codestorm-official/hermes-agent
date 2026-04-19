"""
Hermes Agent — Railway admin server.

Serves an admin UI on $PORT, manages the Hermes gateway as a subprocess.
The gateway is started automatically on boot if a provider API key is present.
"""

import asyncio
import base64
import json
import os
import re
import secrets
import signal
import subprocess
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path

from starlette.applications import Starlette
from starlette.authentication import (
    AuthCredentials,
    AuthenticationBackend,
    AuthenticationError,
    SimpleUser,
)
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from starlette.templating import Jinja2Templates

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

HERMES_HOME = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
ENV_FILE = Path(HERMES_HOME) / ".env"
PAIRING_DIR = Path(HERMES_HOME) / "pairing"
PAIRING_TTL = 3600

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
if not ADMIN_PASSWORD:
    ADMIN_PASSWORD = secrets.token_urlsafe(16)
    print(f"[server] Admin credentials — username: {ADMIN_USERNAME}  password: {ADMIN_PASSWORD}", flush=True)
else:
    print(f"[server] Admin username: {ADMIN_USERNAME}", flush=True)

# ── Env var registry ──────────────────────────────────────────────────────────
# (key, label, category, is_secret)
ENV_VARS = [
    ("LLM_MODEL",               "Model",                    "model",     False),
    ("OPENROUTER_API_KEY",       "OpenRouter",               "provider",  True),
    ("DEEPSEEK_API_KEY",         "DeepSeek",                 "provider",  True),
    ("DASHSCOPE_API_KEY",        "DashScope",                "provider",  True),
    ("GLM_API_KEY",              "GLM / Z.AI",               "provider",  True),
    ("KIMI_API_KEY",             "Kimi",                     "provider",  True),
    ("MINIMAX_API_KEY",          "MiniMax",                  "provider",  True),
    ("HF_TOKEN",                 "Hugging Face",             "provider",  True),
    ("PARALLEL_API_KEY",         "Parallel (search)",        "tool",      True),
    ("FIRECRAWL_API_KEY",        "Firecrawl (scrape)",       "tool",      True),
    ("TAVILY_API_KEY",           "Tavily (search)",          "tool",      True),
    ("FAL_KEY",                  "FAL (image gen)",          "tool",      True),
    ("BROWSERBASE_API_KEY",      "Browserbase key",          "tool",      True),
    ("BROWSERBASE_PROJECT_ID",   "Browserbase project",      "tool",      False),
    ("GITHUB_TOKEN",             "GitHub token",             "tool",      True),
    ("VOICE_TOOLS_OPENAI_KEY",   "OpenAI (voice/TTS)",       "tool",      True),
    ("HONCHO_API_KEY",           "Honcho (memory)",          "tool",      True),
    ("TELEGRAM_BOT_TOKEN",       "Bot Token",                "telegram",  True),
    ("TELEGRAM_ALLOWED_USERS",   "Allowed User IDs",         "telegram",  False),
    ("DISCORD_BOT_TOKEN",        "Bot Token",                "discord",   True),
    ("DISCORD_ALLOWED_USERS",    "Allowed User IDs",         "discord",   False),
    ("SLACK_BOT_TOKEN",          "Bot Token (xoxb-...)",     "slack",     True),
    ("SLACK_APP_TOKEN",          "App Token (xapp-...)",     "slack",     True),
    ("WHATSAPP_ENABLED",         "Enable WhatsApp",          "whatsapp",  False),
    ("EMAIL_ADDRESS",            "Email Address",            "email",     False),
    ("EMAIL_PASSWORD",           "Email Password",           "email",     True),
    ("EMAIL_IMAP_HOST",          "IMAP Host",                "email",     False),
    ("EMAIL_SMTP_HOST",          "SMTP Host",                "email",     False),
    ("MATTERMOST_URL",           "Server URL",               "mattermost",False),
    ("MATTERMOST_TOKEN",         "Bot Token",                "mattermost",True),
    ("MATRIX_HOMESERVER",        "Homeserver URL",           "matrix",    False),
    ("MATRIX_ACCESS_TOKEN",      "Access Token",             "matrix",    True),
    ("MATRIX_USER_ID",           "User ID",                  "matrix",    False),
    ("GATEWAY_ALLOW_ALL_USERS",  "Allow all users",          "gateway",   False),
    ("ADMIN_USERNAME",           "Admin username",           "admin",     False),
    ("ADMIN_PASSWORD",           "Admin password",           "admin",     True),
]

SECRET_KEYS  = {k for k, _, _, s in ENV_VARS if s}
PROVIDER_KEYS = [k for k, _, c, _ in ENV_VARS if c == "provider"]
CHANNEL_MAP  = {
    "Telegram":    "TELEGRAM_BOT_TOKEN",
    "Discord":     "DISCORD_BOT_TOKEN",
    "Slack":       "SLACK_BOT_TOKEN",
    "WhatsApp":    "WHATSAPP_ENABLED",
    "Email":       "EMAIL_ADDRESS",
    "Mattermost":  "MATTERMOST_TOKEN",
    "Matrix":      "MATRIX_ACCESS_TOKEN",
}


# ── .env helpers ──────────────────────────────────────────────────────────────
def read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
            v = v[1:-1]
        out[k.strip()] = v
    return out


def write_config_yaml(data: dict[str, str]) -> None:
    """Merge the dashboard's model choice into the persisted config.yaml.

    Non-dashboard fields (terminal, agent, mcp_servers, model.provider) are
    owned by hermes-config/config.seed.yaml (merged at container boot by
    start.sh / merge_config.py) and by `hermes model` / codex_login.py at
    runtime. The dashboard only owns model.default — the single dropdown the
    admin UI exposes. This function therefore loads the current config.yaml,
    sets model.default if the user picked one, and writes back — everything
    else is preserved.
    """
    import yaml

    config_path = Path(HERMES_HOME) / "config.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)

    current: dict = {}
    if config_path.exists():
        try:
            current = yaml.safe_load(config_path.read_text()) or {}
        except yaml.YAMLError:
            # Corrupt file: fall back to an empty dict so the merge rewrites it.
            current = {}

    model = data.get("LLM_MODEL", "")
    if model:
        # The model key can be a dict, a bare string (from `hermes config set
        # model <name>`), or missing. Normalise to dict before updating.
        raw = current.get("model")
        if isinstance(raw, dict):
            model_block = dict(raw)
        elif isinstance(raw, str) and raw.strip():
            model_block = {"default": raw.strip()}
        else:
            model_block = {}
        model_block["default"] = model
        current["model"] = model_block

    config_path.write_text(yaml.safe_dump(current, sort_keys=False))


def write_env(path: Path, data: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cat_order = ["model", "provider", "tool",
                 "telegram", "discord", "slack", "whatsapp",
                 "email", "mattermost", "matrix", "gateway"]
    cat_labels = {
        "model": "Model", "provider": "Providers", "tool": "Tools",
        "telegram": "Telegram", "discord": "Discord", "slack": "Slack",
        "whatsapp": "WhatsApp", "email": "Email",
        "mattermost": "Mattermost", "matrix": "Matrix", "gateway": "Gateway",
    }
    key_cat = {k: c for k, _, c, _ in ENV_VARS}
    grouped: dict[str, list[str]] = {c: [] for c in cat_order}
    grouped["other"] = []

    for k, v in data.items():
        if not v:
            continue
        cat = key_cat.get(k, "other")
        grouped.setdefault(cat, []).append(f"{k}={v}")

    lines: list[str] = []
    for cat in cat_order:
        entries = sorted(grouped.get(cat, []))
        if entries:
            lines.append(f"# {cat_labels.get(cat, cat)}")
            lines.extend(entries)
            lines.append("")
    if grouped["other"]:
        lines.append("# Other")
        lines.extend(sorted(grouped["other"]))
        lines.append("")

    path.write_text("\n".join(lines))


def mask(data: dict[str, str]) -> dict[str, str]:
    return {
        k: (v[:8] + "***" if len(v) > 8 else "***") if k in SECRET_KEYS and v else v
        for k, v in data.items()
    }


def unmask(new: dict[str, str], existing: dict[str, str]) -> dict[str, str]:
    return {
        k: (existing.get(k, "") if k in SECRET_KEYS and v.endswith("***") else v)
        for k, v in new.items()
    }


# ── Auth ──────────────────────────────────────────────────────────────────────
class BasicAuth(AuthenticationBackend):
    async def authenticate(self, conn):
        if "Authorization" not in conn.headers:
            return None
        try:
            scheme, creds = conn.headers["Authorization"].split()
            if scheme.lower() != "basic":
                return None
            user, _, pw = base64.b64decode(creds).decode().partition(":")
        except Exception:
            raise AuthenticationError("Invalid credentials")
        if user == ADMIN_USERNAME and pw == ADMIN_PASSWORD:
            return AuthCredentials(["authenticated"]), SimpleUser(user)
        raise AuthenticationError("Invalid credentials")


def guard(request: Request):
    if not request.user.is_authenticated:
        return PlainTextResponse(
            "Unauthorized", status_code=401,
            headers={"WWW-Authenticate": 'Basic realm="hermes-admin"'},
        )


# ── Gateway manager ───────────────────────────────────────────────────────────
class Gateway:
    def __init__(self):
        self.proc: asyncio.subprocess.Process | None = None
        self.state = "stopped"
        self.logs: deque[str] = deque(maxlen=500)
        self.started_at: float | None = None
        self.restarts = 0

    async def start(self):
        if self.proc and self.proc.returncode is None:
            return
        self.state = "starting"
        try:
            # .env values take priority over Railway env vars.
            # We build the env this way so hermes's own dotenv loading
            # (which reads the same file) doesn't shadow our values.
            env = {**os.environ, "HERMES_HOME": HERMES_HOME}
            env.update(read_env(ENV_FILE))
            model = env.get("LLM_MODEL", "")
            provider_key = next((env.get(k, "") for k in PROVIDER_KEYS if env.get(k)), "")
            print(f"[gateway] model={model or '⚠ NOT SET'} | provider_key={'set' if provider_key else '⚠ NOT SET'}", flush=True)
            # Write config.yaml so hermes picks up the model (env vars alone aren't always enough)
            write_config_yaml(read_env(ENV_FILE))
            self.proc = await asyncio.create_subprocess_exec(
                "hermes", "gateway",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=env,
            )
            self.state = "running"
            self.started_at = time.time()
            asyncio.create_task(self._drain())
        except Exception as e:
            self.state = "error"
            self.logs.append(f"[error] Failed to start: {e}")

    async def stop(self):
        if not self.proc or self.proc.returncode is not None:
            self.state = "stopped"
            return
        self.state = "stopping"
        self.proc.terminate()
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            self.proc.kill()
            await self.proc.wait()
        self.state = "stopped"
        self.started_at = None

    async def restart(self):
        await self.stop()
        self.restarts += 1
        await self.start()

    async def _drain(self):
        assert self.proc and self.proc.stdout
        async for raw in self.proc.stdout:
            line = ANSI_ESCAPE.sub("", raw.decode(errors="replace").rstrip())
            self.logs.append(line)
        if self.state == "running":
            self.state = "error"
            self.logs.append(f"[error] Gateway exited (code {self.proc.returncode})")

    def status(self) -> dict:
        uptime = int(time.time() - self.started_at) if self.started_at and self.state == "running" else None
        return {
            "state":    self.state,
            "pid":      self.proc.pid if self.proc and self.proc.returncode is None else None,
            "uptime":   uptime,
            "restarts": self.restarts,
        }


gw = Gateway()
cfg_lock = asyncio.Lock()


# ── Route handlers ────────────────────────────────────────────────────────────
async def page_index(request: Request):
    if err := guard(request): return err
    return templates.TemplateResponse(request, "index.html")


async def route_health(request: Request):
    return JSONResponse({"status": "ok", "gateway": gw.state})


async def api_config_get(request: Request):
    if err := guard(request): return err
    async with cfg_lock:
        data = read_env(ENV_FILE)
    defs = [{"key": k, "label": l, "category": c, "secret": s} for k, l, c, s in ENV_VARS]
    return JSONResponse({"vars": mask(data), "defs": defs})


async def api_config_put(request: Request):
    if err := guard(request): return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    try:
        restart = body.pop("_restart", False)
        new_vars = body.get("vars", {})
        async with cfg_lock:
            existing = read_env(ENV_FILE)
            merged = unmask(new_vars, existing)
            for k, v in existing.items():
                if k not in merged:
                    merged[k] = v
            write_env(ENV_FILE, merged)
            write_config_yaml(merged)
        if restart:
            asyncio.create_task(gw.restart())
        return JSONResponse({"ok": True, "restarting": restart})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_status(request: Request):
    if err := guard(request): return err
    data = read_env(ENV_FILE)
    providers = {
        k.replace("_API_KEY","").replace("_TOKEN","").replace("HF_","HuggingFace ").replace("_"," ").title():
        {"configured": bool(data.get(k))}
        for k in PROVIDER_KEYS
    }
    channels = {
        name: {"configured": bool(v := data.get(key,"")) and v.lower() not in ("false","0","no")}
        for name, key in CHANNEL_MAP.items()
    }
    return JSONResponse({"gateway": gw.status(), "providers": providers, "channels": channels})


async def api_logs(request: Request):
    if err := guard(request): return err
    return JSONResponse({"lines": list(gw.logs)})


async def api_gw_start(request: Request):
    if err := guard(request): return err
    asyncio.create_task(gw.start())
    return JSONResponse({"ok": True})


async def api_gw_stop(request: Request):
    if err := guard(request): return err
    asyncio.create_task(gw.stop())
    return JSONResponse({"ok": True})


async def api_gw_restart(request: Request):
    if err := guard(request): return err
    asyncio.create_task(gw.restart())
    return JSONResponse({"ok": True})


async def api_config_reset(request: Request):
    if err := guard(request): return err
    asyncio.create_task(gw.stop())
    async with cfg_lock:
        if ENV_FILE.exists():
            ENV_FILE.unlink()
        write_config_yaml({})
    return JSONResponse({"ok": True})


# ── Pairing ───────────────────────────────────────────────────────────────────
def _pjson(path: Path) -> dict:
    try:
        return json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        return {}


def _wjson(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    try: os.chmod(path, 0o600)
    except OSError: pass


def _platforms(suffix: str) -> list[str]:
    if not PAIRING_DIR.exists(): return []
    return [f.stem.rsplit(f"-{suffix}", 1)[0] for f in PAIRING_DIR.glob(f"*-{suffix}.json")]


async def api_pairing_pending(request: Request):
    if err := guard(request): return err
    now = time.time()
    out = []
    for p in _platforms("pending"):
        for code, info in _pjson(PAIRING_DIR / f"{p}-pending.json").items():
            if now - info.get("created_at", now) <= PAIRING_TTL:
                out.append({"platform": p, "code": code,
                            "user_id": info.get("user_id",""), "user_name": info.get("user_name",""),
                            "age_minutes": int((now - info.get("created_at", now)) / 60)})
    return JSONResponse({"pending": out})


async def api_pairing_approve(request: Request):
    if err := guard(request): return err
    try: body = await request.json()
    except Exception: return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    platform, code = body.get("platform",""), body.get("code","").upper().strip()
    if not platform or not code:
        return JSONResponse({"error": "platform and code required"}, status_code=400)
    pending_path = PAIRING_DIR / f"{platform}-pending.json"
    pending = _pjson(pending_path)
    if code not in pending:
        return JSONResponse({"error": "Code not found"}, status_code=404)
    entry = pending.pop(code)
    _wjson(pending_path, pending)
    approved = _pjson(PAIRING_DIR / f"{platform}-approved.json")
    approved[entry["user_id"]] = {"user_name": entry.get("user_name",""), "approved_at": time.time()}
    _wjson(PAIRING_DIR / f"{platform}-approved.json", approved)
    return JSONResponse({"ok": True})


async def api_pairing_deny(request: Request):
    if err := guard(request): return err
    try: body = await request.json()
    except Exception: return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    platform, code = body.get("platform",""), body.get("code","").upper().strip()
    p = PAIRING_DIR / f"{platform}-pending.json"
    pending = _pjson(p)
    if code in pending:
        del pending[code]
        _wjson(p, pending)
    return JSONResponse({"ok": True})


async def api_pairing_approved(request: Request):
    if err := guard(request): return err
    out = []
    for p in _platforms("approved"):
        for uid, info in _pjson(PAIRING_DIR / f"{p}-approved.json").items():
            out.append({"platform": p, "user_id": uid,
                        "user_name": info.get("user_name",""), "approved_at": info.get("approved_at",0)})
    return JSONResponse({"approved": out})


async def api_pairing_revoke(request: Request):
    if err := guard(request): return err
    try: body = await request.json()
    except Exception: return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    platform, uid = body.get("platform",""), body.get("user_id","")
    if not platform or not uid:
        return JSONResponse({"error": "platform and user_id required"}, status_code=400)
    p = PAIRING_DIR / f"{platform}-approved.json"
    approved = _pjson(p)
    if uid in approved:
        del approved[uid]
        _wjson(p, approved)
    return JSONResponse({"ok": True})


# ── Skills registry (read-only) ───────────────────────────────────────────────
SKILLS_DIR = Path("/opt/hermes-skills")
VAULT_DIR = Path(os.environ.get("OBSIDIAN_VAULT_PATH", "/data/vault"))


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Minimal YAML frontmatter parser. Returns (metadata, body)."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    raw = text[4:end]
    body = text[end + 5:]
    meta: dict = {}
    for line in raw.splitlines():
        line = line.rstrip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
            val = val[1:-1]
        meta[key.strip()] = val
    return meta, body


async def api_skills_list(request: Request):
    if err := guard(request): return err
    out = []
    if SKILLS_DIR.exists():
        for skill_dir in sorted(SKILLS_DIR.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill_md = skill_dir / "SKILL.md"
            if not skill_md.exists():
                continue
            try:
                text = skill_md.read_text(encoding="utf-8")
            except OSError:
                continue
            meta, _ = _parse_frontmatter(text)
            scripts = []
            scripts_dir = skill_dir / "scripts"
            if scripts_dir.exists():
                scripts = sorted(p.name for p in scripts_dir.iterdir() if p.is_file())
            out.append({
                "name": meta.get("name", skill_dir.name),
                "description": meta.get("description", ""),
                "path": str(skill_dir),
                "scripts": scripts,
            })
    return JSONResponse({"skills": out})


# ── Vault task status write-back ──────────────────────────────────────────────
VALID_TASK_STATUSES = {"todo", "in_progress", "doing", "done", "cancelled"}


def _vault_safe_path(relative: str) -> Path | None:
    """Resolve `relative` under VAULT_DIR. Rejects traversal, absolute paths,
    and anything outside Tasks/. Returns None on any safety violation."""
    if not relative:
        return None
    p = Path(relative.replace("\\", "/"))
    if p.is_absolute():
        return None
    if ".." in p.parts:
        return None
    if p.parts[:1] != ("Tasks",):
        return None
    if p.suffix.lower() != ".md":
        return None
    full = (VAULT_DIR / p).resolve()
    try:
        full.relative_to(VAULT_DIR.resolve())
    except ValueError:
        return None
    if not full.exists():
        return None
    return full


def _patch_task_status(full_path: Path, new_status: str) -> tuple[str, str]:
    """Rewrite the task's YAML frontmatter with status=<new_status>. Also
    sets completed_date when status is 'done'. Returns (old_status, filename)."""
    text = full_path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError("Task file has no YAML frontmatter")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise ValueError("Unterminated YAML frontmatter")
    fm = text[4:end]
    body = text[end + 5:]

    old_status = "unknown"
    lines = fm.splitlines()
    status_line_idx = None
    completed_line_idx = None
    for i, line in enumerate(lines):
        if line.startswith("status:"):
            status_line_idx = i
            old_status = line.partition(":")[2].strip().strip('"').strip("'")
        elif line.startswith("completed_date:"):
            completed_line_idx = i

    today = date.today().isoformat()
    if status_line_idx is None:
        lines.append(f"status: {new_status}")
    else:
        lines[status_line_idx] = f"status: {new_status}"

    if new_status == "done":
        cd_line = f"completed_date: {today}"
        if completed_line_idx is None:
            lines.append(cd_line)
        else:
            lines[completed_line_idx] = cd_line

    new_text = "---\n" + "\n".join(lines) + "\n---\n" + body
    full_path.write_text(new_text, encoding="utf-8")
    return old_status, full_path.name


def _git_commit_vault(path: Path, message: str) -> tuple[bool, str]:
    """Stage + commit + push a single changed path in the vault. Best effort."""
    try:
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        subprocess.run(["git", "-C", str(VAULT_DIR), "add", str(path)], check=True, env=env, timeout=30)
        r = subprocess.run(
            ["git", "-C", str(VAULT_DIR), "commit", "-m", message],
            capture_output=True, text=True, env=env, timeout=30,
        )
        if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr):
            return False, f"commit failed: {(r.stdout + r.stderr).strip()[:200]}"
        subprocess.run(["git", "-C", str(VAULT_DIR), "push", "origin", "HEAD"],
                       check=False, env=env, timeout=60)
        return True, "ok"
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        return False, f"git error: {e}"


async def api_vault_task_status(request: Request):
    """POST {path: "Tasks/XYZ.md", status: "done"} → patches YAML + commits vault."""
    if err := guard(request): return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    relative = (body.get("path") or "").strip()
    new_status = (body.get("status") or "").strip().lower().replace(" ", "_")
    if new_status not in VALID_TASK_STATUSES:
        return JSONResponse({"error": f"status must be one of {sorted(VALID_TASK_STATUSES)}"}, status_code=400)

    full = _vault_safe_path(relative)
    if full is None:
        return JSONResponse({"error": "invalid or unknown path"}, status_code=400)

    try:
        old_status, fname = _patch_task_status(full, new_status)
    except Exception as e:
        return JSONResponse({"error": f"patch failed: {e}"}, status_code=500)

    msg = f"Hermes: Task '{fname[:-3]}' status {old_status} -> {new_status}"
    ok, info = _git_commit_vault(full, msg)
    return JSONResponse({
        "ok": ok, "path": relative, "old_status": old_status,
        "new_status": new_status, "git": info,
    })


# ── App lifecycle ─────────────────────────────────────────────────────────────
async def auto_start():
    data = read_env(ENV_FILE)
    if any(data.get(k) for k in PROVIDER_KEYS):
        asyncio.create_task(gw.start())
    else:
        print("[server] No provider key found — gateway not started. Configure one in the admin UI.", flush=True)


@asynccontextmanager
async def lifespan(app):
    await auto_start()
    yield
    await gw.stop()


routes = [
    Route("/",                          page_index),
    Route("/health",                    route_health),
    Route("/api/config",                api_config_get,      methods=["GET"]),
    Route("/api/config",                api_config_put,      methods=["PUT"]),
    Route("/api/status",                api_status),
    Route("/api/logs",                  api_logs),
    Route("/api/gateway/start",         api_gw_start,        methods=["POST"]),
    Route("/api/gateway/stop",          api_gw_stop,         methods=["POST"]),
    Route("/api/gateway/restart",       api_gw_restart,      methods=["POST"]),
    Route("/api/config/reset",          api_config_reset,    methods=["POST"]),
    Route("/api/pairing/pending",       api_pairing_pending),
    Route("/api/pairing/approve",       api_pairing_approve, methods=["POST"]),
    Route("/api/pairing/deny",          api_pairing_deny,    methods=["POST"]),
    Route("/api/pairing/approved",      api_pairing_approved),
    Route("/api/pairing/revoke",        api_pairing_revoke,  methods=["POST"]),
    Route("/api/skills",                api_skills_list),
    Route("/api/vault/task/status",     api_vault_task_status, methods=["POST"]),
]

app = Starlette(
    routes=routes,
    middleware=[Middleware(AuthenticationMiddleware, backend=BasicAuth())],
    lifespan=lifespan,
)

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8080"))
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info", loop="asyncio")
    server = uvicorn.Server(config)

    def _shutdown():
        loop.create_task(gw.stop())
        server.should_exit = True

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _shutdown)

    loop.run_until_complete(server.serve())
