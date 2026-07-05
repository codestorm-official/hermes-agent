"""
Hermes Agent — Railway admin server.

Serves an admin UI on $PORT, manages the Hermes gateway as a subprocess.
The gateway is started automatically on boot if a provider API key is present.
"""

import asyncio
import base64
import hashlib
import hmac
import imaplib
import json
import os
import re
import secrets
import shutil
import ssl
import signal
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from starlette.applications import Starlette
from starlette.authentication import (
    AuthCredentials,
    AuthenticationBackend,
    SimpleUser,
)
from starlette.middleware import Middleware
from starlette.middleware.authentication import AuthenticationMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Route
from starlette.templating import Jinja2Templates

ANSI_ESCAPE = re.compile(r"\x1b\[[0-9;]*m")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

HERMES_HOME = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
ENV_FILE = Path(HERMES_HOME) / ".env"
CONFIG_FILE = Path(HERMES_HOME) / "config.yaml"
PAIRING_DIR = Path(HERMES_HOME) / "pairing"
PAIRING_TTL = 3600
MAX_PROVIDER_RESPONSE = 8 * 1024 * 1024

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
if not ADMIN_PASSWORD:
    ADMIN_PASSWORD = secrets.token_urlsafe(16)
    print(f"[server] Admin credentials — username: {ADMIN_USERNAME}  password: {ADMIN_PASSWORD}", flush=True)
else:
    print(f"[server] Admin username: {ADMIN_USERNAME}", flush=True)

SESSION_COOKIE = "hermes_admin_session"
SESSION_TTL = int(os.environ.get("ADMIN_SESSION_TTL", str(12 * 3600)))
SESSION_SECRET = os.environ.get("ADMIN_SESSION_SECRET", "")
if not SESSION_SECRET:
    SESSION_SECRET = hashlib.sha256(
        f"hermes-admin-session:{ADMIN_USERNAME}:{ADMIN_PASSWORD}".encode()
    ).hexdigest()
login_attempts: dict[str, deque[float]] = {}

# ── Env var registry ──────────────────────────────────────────────────────────
# (key, label, category, is_secret)
ENV_VARS = [
    ("LLM_MODEL",               "Model",                    "model",     False),
    ("LLM_PROVIDER",            "Active Provider",          "model",     False),
    ("OPENROUTER_API_KEY",       "OpenRouter",               "provider",  True),
    ("OPENAI_API_KEY",           "OpenAI",                   "provider",  True),
    ("ANTHROPIC_API_KEY",        "Claude / Anthropic",       "provider",  True),
    ("GEMINI_API_KEY",           "Google Gemini",            "provider",  True),
    ("XAI_API_KEY",              "xAI / Grok",               "provider",  True),
    ("NVIDIA_API_KEY",           "NVIDIA NIM",               "provider",  True),
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
PROVIDER_CONFIG = {
    "OpenRouter": ("OPENROUTER_API_KEY", "openrouter"),
    "OpenAI": ("OPENAI_API_KEY", "openai"),
    "Claude / Anthropic": ("ANTHROPIC_API_KEY", "anthropic"),
    "Google Gemini": ("GEMINI_API_KEY", "gemini"),
    "xAI / Grok": ("XAI_API_KEY", "xai"),
    "NVIDIA NIM": ("NVIDIA_API_KEY", "nvidia"),
    "DeepSeek": ("DEEPSEEK_API_KEY", "deepseek"),
    "DashScope": ("DASHSCOPE_API_KEY", "alibaba"),
    "GLM / Z.AI": ("GLM_API_KEY", "zai"),
    "Kimi": ("KIMI_API_KEY", "kimi-coding"),
    "MiniMax": ("MINIMAX_API_KEY", "minimax"),
    "HuggingFace": ("HF_TOKEN", "huggingface"),
}
CHANNEL_MAP  = {
    "Telegram":    "TELEGRAM_BOT_TOKEN",
    "Discord":     "DISCORD_BOT_TOKEN",
    "Slack":       "SLACK_BOT_TOKEN",
    "WhatsApp":    "WHATSAPP_ENABLED",
    "Email":       "EMAIL_ADDRESS",
    "Mattermost":  "MATTERMOST_TOKEN",
    "Matrix":      "MATRIX_ACCESS_TOKEN",
}
CHANNEL_KEYS = {
    "Telegram": ["TELEGRAM_BOT_TOKEN", "TELEGRAM_ALLOWED_USERS"],
    "Discord": ["DISCORD_BOT_TOKEN", "DISCORD_ALLOWED_USERS"],
    "Slack": ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN"],
    "WhatsApp": ["WHATSAPP_ENABLED"],
    "Email": ["EMAIL_ADDRESS", "EMAIL_PASSWORD", "EMAIL_IMAP_HOST", "EMAIL_SMTP_HOST"],
    "Mattermost": ["MATTERMOST_URL", "MATTERMOST_TOKEN"],
    "Matrix": ["MATRIX_HOMESERVER", "MATRIX_ACCESS_TOKEN", "MATRIX_USER_ID"],
}

PROVIDER_TESTS = {
    "OpenRouter": ("OPENROUTER_API_KEY", "https://openrouter.ai/api/v1/chat/completions", "openai"),
    "OpenAI": ("OPENAI_API_KEY", "https://api.openai.com/v1/chat/completions", "openai"),
    "Claude / Anthropic": ("ANTHROPIC_API_KEY", "https://api.anthropic.com/v1/messages", "anthropic"),
    "Google Gemini": ("GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent", "gemini"),
    "xAI / Grok": ("XAI_API_KEY", "https://api.x.ai/v1/chat/completions", "openai"),
    "NVIDIA NIM": ("NVIDIA_API_KEY", "https://integrate.api.nvidia.com/v1/chat/completions", "openai"),
    "DeepSeek": ("DEEPSEEK_API_KEY", "https://api.deepseek.com/chat/completions", "openai"),
    "DashScope": ("DASHSCOPE_API_KEY", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/chat/completions", "openai"),
    "GLM / Z.AI": ("GLM_API_KEY", "https://open.bigmodel.cn/api/paas/v4/chat/completions", "openai"),
    "Kimi": ("KIMI_API_KEY", "https://api.moonshot.cn/v1/chat/completions", "openai"),
    "MiniMax": ("MINIMAX_API_KEY", "https://api.minimax.io/v1/chat/completions", "openai"),
    "HuggingFace": ("HF_TOKEN", "https://router.huggingface.co/v1/chat/completions", "openai"),
}

PROVIDER_MODEL_URLS = {
    "OpenRouter": "https://openrouter.ai/api/v1/models",
    "OpenAI": "https://api.openai.com/v1/models",
    "Claude / Anthropic": "https://api.anthropic.com/v1/models?limit=1000",
    "Google Gemini": "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000",
    "xAI / Grok": "https://api.x.ai/v1/models",
    "NVIDIA NIM": "https://integrate.api.nvidia.com/v1/models",
    "DeepSeek": "https://api.deepseek.com/models",
    "DashScope": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1/models",
    "GLM / Z.AI": "https://open.bigmodel.cn/api/paas/v4/models",
    "Kimi": "https://api.moonshot.cn/v1/models",
    "MiniMax": "https://api.minimax.io/v1/models",
    "HuggingFace": "https://router.huggingface.co/v1/models",
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
    """Merge dashboard-owned settings without discarding other Hermes config."""
    model = data.get("LLM_MODEL", "")
    provider = data.get("LLM_PROVIDER", "auto") or "auto"
    config: dict = {}
    if CONFIG_FILE.exists():
        loaded = yaml.safe_load(CONFIG_FILE.read_text())
        if loaded is not None and not isinstance(loaded, dict):
            raise ValueError("config.yaml root must be a mapping")
        config = loaded or {}

    model_config = config.get("model")
    if not isinstance(model_config, dict):
        model_config = {}
        config["model"] = model_config
    model_config["default"] = model
    model_config["provider"] = provider

    terminal = config.get("terminal")
    if not isinstance(terminal, dict):
        terminal = {}
        config["terminal"] = terminal
    terminal.setdefault("backend", "local")
    terminal.setdefault("timeout", 60)
    terminal.setdefault("cwd", "/tmp")

    agent = config.get("agent")
    if not isinstance(agent, dict):
        agent = {}
        config["agent"] = agent
    agent.setdefault("max_iterations", 50)
    config.setdefault("data_dir", HERMES_HOME)

    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_path = CONFIG_FILE.with_suffix(".yaml.tmp")
    temp_path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True))
    os.chmod(temp_path, 0o600)
    temp_path.replace(CONFIG_FILE)


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
    os.chmod(path, 0o600)


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


def resolve_secret(value: str, key: str) -> str:
    """Resolve a masked value from disk without ever returning it to the client."""
    if value.endswith("***"):
        return read_env(ENV_FILE).get(key, "")
    return value


def provider_model_key(api_key_name: str) -> str:
    base = api_key_name.removesuffix("_API_KEY").removesuffix("_TOKEN")
    return f"LLM_MODEL_{base}"


def effective_active_provider(data: dict[str, str]) -> str:
    requested = data.get("LLM_PROVIDER", "")
    if requested and requested != "auto":
        registered = any(
            slug == requested and data.get(key) and data.get(provider_model_key(key))
            for key, slug in PROVIDER_CONFIG.values()
        )
        if registered:
            return requested
    registered_slug = next((
        slug for _, (key, slug) in PROVIDER_CONFIG.items()
        if data.get(key) and data.get(provider_model_key(key))
    ), "")
    if registered_slug:
        return registered_slug
    return next((slug for _, (key, slug) in PROVIDER_CONFIG.items() if data.get(key)), "")


def _http_json(url: str, *, headers: dict[str, str] | None = None,
               payload: dict | None = None, timeout: int = 15) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Accept": "application/json", **(headers or {})},
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as res:
            content = res.read(MAX_PROVIDER_RESPONSE + 1)
            if len(content) > MAX_PROVIDER_RESPONSE:
                raise ValueError("Provider response is larger than 8 MB")
            raw = content.decode(errors="replace")
            return res.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        raw = exc.read(8_192).decode(errors="replace")
        try:
            detail = json.loads(raw)
        except json.JSONDecodeError:
            detail = {"error": raw[:500] or exc.reason}
        return exc.code, detail


def _error_message(data: dict, fallback: str) -> str:
    error = data.get("error", data.get("message", fallback))
    if isinstance(error, dict):
        error = error.get("message") or error.get("type") or fallback
    return str(error)[:300]


# ── Auth ──────────────────────────────────────────────────────────────────────
def create_session_token() -> str:
    expires = int(time.time()) + SESSION_TTL
    payload = f"{ADMIN_USERNAME}:{expires}".encode()
    signature = hmac.new(SESSION_SECRET.encode(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(payload + b"." + signature).decode()


def verify_session_token(token: str) -> bool:
    try:
        decoded = base64.urlsafe_b64decode(token.encode())
        payload, signature = decoded.rsplit(b".", 1)
        expected = hmac.new(SESSION_SECRET.encode(), payload, hashlib.sha256).digest()
        user, expires = payload.decode().rsplit(":", 1)
        return (
            hmac.compare_digest(signature, expected)
            and hmac.compare_digest(user, ADMIN_USERNAME)
            and int(expires) > int(time.time())
        )
    except Exception:
        return False


class AdminAuth(AuthenticationBackend):
    async def authenticate(self, conn):
        token = conn.cookies.get(SESSION_COOKIE, "")
        if token and verify_session_token(token):
            return AuthCredentials(["authenticated"]), SimpleUser(ADMIN_USERNAME)
        if "Authorization" not in conn.headers:
            return None
        try:
            scheme, creds = conn.headers["Authorization"].split()
            if scheme.lower() != "basic":
                return None
            user, _, pw = base64.b64decode(creds).decode().partition(":")
        except Exception:
            return None
        if user == ADMIN_USERNAME and pw == ADMIN_PASSWORD:
            return AuthCredentials(["authenticated"]), SimpleUser(user)
        return None


def guard(request: Request):
    if not request.user.is_authenticated:
        if request.url.path == "/":
            return RedirectResponse("/login", status_code=303)
        return JSONResponse({"error": "Authentication required"}, status_code=401)


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


async def page_login(request: Request):
    if request.user.is_authenticated:
        return RedirectResponse("/", status_code=303)
    error = ""
    if request.method == "POST":
        client_ip = request.client.host if request.client else "unknown"
        now = time.time()
        attempts = login_attempts.setdefault(client_ip, deque())
        while attempts and now - attempts[0] > 300:
            attempts.popleft()
        if len(attempts) >= 10:
            error = "Too many login attempts. Try again in a few minutes."
        else:
            form = await request.form()
            username = str(form.get("username", ""))
            password = str(form.get("password", ""))
            valid = (
                hmac.compare_digest(username, ADMIN_USERNAME)
                and hmac.compare_digest(password, ADMIN_PASSWORD)
            )
            if valid:
                login_attempts.pop(client_ip, None)
                response = RedirectResponse("/", status_code=303)
                forwarded_proto = request.headers.get("x-forwarded-proto", "")
                response.set_cookie(
                    SESSION_COOKIE,
                    create_session_token(),
                    max_age=SESSION_TTL,
                    httponly=True,
                    secure=request.url.scheme == "https" or forwarded_proto == "https",
                    samesite="strict",
                    path="/",
                )
                return response
            attempts.append(now)
            error = "Invalid username or password."
    notice = "Your session expired. Please login again." if request.query_params.get("expired") else ""
    return templates.TemplateResponse(
        request, "login.html", {"error": error, "notice": notice},
        status_code=401 if error else 200,
    )


async def route_health(request: Request):
    return JSONResponse({"status": "ok", "gateway": gw.state})


async def route_logout(request: Request):
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.headers["Cache-Control"] = "no-store"
    return response


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
    active_slug = effective_active_provider(data)
    providers = {}
    for label, (key, slug) in PROVIDER_CONFIG.items():
        model_key = provider_model_key(key)
        registered_model = data.get(model_key, "")
        model = registered_model or (data.get("LLM_MODEL", "") if slug == active_slug else "")
        key_set = bool(data.get(key))
        providers[label] = {
            "configured": key_set and bool(registered_model),
            "key_set": key_set,
            "active": slug == active_slug,
            "slug": slug,
            "model": model,
        }
    channels = {
        name: {"configured": bool(v := data.get(key,"")) and v.lower() not in ("false","0","no")}
        for name, key in CHANNEL_MAP.items()
    }
    return JSONResponse({"gateway": gw.status(), "providers": providers, "channels": channels,
                         "active_provider": active_slug})


async def api_provider_activate(request: Request):
    if err := guard(request): return err
    try:
        body = await request.json()
        label = str(body.get("provider", ""))
        spec = PROVIDER_CONFIG.get(label)
        if not spec:
            return JSONResponse({"error": "Unknown provider"}, status_code=400)
        key, slug = spec
        async with cfg_lock:
            data = read_env(ENV_FILE)
            if not data.get(key):
                return JSONResponse({"error": "Configure this provider API key first"}, status_code=422)
            model_key = provider_model_key(key)
            model = data.get(model_key, "")
            if not model:
                return JSONResponse(
                    {"error": "Choose and save a model for this provider first"},
                    status_code=422,
                )
            data["LLM_PROVIDER"] = slug
            data["LLM_MODEL"] = model
            write_env(ENV_FILE, data)
            write_config_yaml(data)
        return JSONResponse({
            "ok": True,
            "provider": label,
            "model": model,
            "restart_required": False,
            "restart_recommended": gw.state == "running",
            "message": "New sessions will use this provider. Existing sessions keep their current model.",
        })
    except Exception as exc:
        return JSONResponse({"error": str(exc)[:300]}, status_code=500)


async def api_provider_remove(request: Request):
    if err := guard(request): return err
    try:
        body = await request.json()
        label = str(body.get("provider", ""))
        spec = PROVIDER_CONFIG.get(label)
        if not spec:
            return JSONResponse({"error": "Unknown provider"}, status_code=400)
        key, slug = spec
        async with cfg_lock:
            data = read_env(ENV_FILE)
            if not data.get(key):
                return JSONResponse({"error": "Provider is not configured"}, status_code=404)
            if slug == effective_active_provider(data) and data.get(provider_model_key(key)):
                return JSONResponse(
                    {"error": "Switch to another provider before removing the active provider"},
                    status_code=409,
                )
            data.pop(key, None)
            data.pop(provider_model_key(key), None)
            if data.get("LLM_PROVIDER") == slug:
                replacement = effective_active_provider(data)
                data["LLM_PROVIDER"] = replacement or "auto"
                if replacement:
                    replacement_key = next(
                        k for k, s in PROVIDER_CONFIG.values() if s == replacement
                    )
                    data["LLM_MODEL"] = data.get(provider_model_key(replacement_key), "")
            write_env(ENV_FILE, data)
            write_config_yaml(data)
        return JSONResponse({"ok": True, "provider": label})
    except Exception as exc:
        return JSONResponse({"error": str(exc)[:300]}, status_code=500)


async def api_channel_remove(request: Request):
    if err := guard(request): return err
    try:
        body = await request.json()
        channel = str(body.get("channel", ""))
        if channel not in CHANNEL_MAP:
            return JSONResponse({"error": "Unknown channel"}, status_code=400)
        async with cfg_lock:
            data = read_env(ENV_FILE)
            configured = [
                name for name, key in CHANNEL_MAP.items()
                if data.get(key, "").lower() not in ("", "false", "0", "no")
            ]
            if channel not in configured:
                return JSONResponse({"error": "Channel is not configured"}, status_code=404)
            if len(configured) <= 1:
                return JSONResponse(
                    {"error": "Add another channel before removing the last configured channel"},
                    status_code=409,
                )
            for key in CHANNEL_KEYS[channel]:
                data.pop(key, None)
            write_env(ENV_FILE, data)
            write_config_yaml(data)
        restarting = gw.state == "running"
        if restarting:
            asyncio.create_task(gw.restart())
        return JSONResponse({"ok": True, "channel": channel, "restarting": restarting})
    except Exception as exc:
        return JSONResponse({"error": str(exc)[:300]}, status_code=500)


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
        if CONFIG_FILE.exists():
            CONFIG_FILE.unlink()
        write_config_yaml({})
    return JSONResponse({"ok": True})


# ── Setup tests & diagnostics ─────────────────────────────────────────────────
async def api_test_provider(request: Request):
    if err := guard(request): return err
    try:
        body = await request.json()
        provider = body.get("provider", "")
        model = str(body.get("model", "")).strip()
        spec = PROVIDER_TESTS.get(provider)
        if not spec or not model:
            return JSONResponse({"error": "Provider and model are required"}, status_code=400)
        key_name, endpoint, protocol = spec
        api_key = resolve_secret(str(body.get("api_key", "")), key_name)
        if not api_key:
            return JSONResponse({"error": "API key is required"}, status_code=400)
        started = time.monotonic()
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        payload = {
                "model": model,
                "messages": [{"role": "user", "content": "Reply with OK"}],
                "max_tokens": 2,
                "temperature": 0,
        }
        if protocol == "anthropic":
            headers = {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            }
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": "Reply with OK"}],
                "max_tokens": 2,
            }
        elif protocol == "gemini":
            endpoint = endpoint.format(model=urllib.parse.quote(model, safe=""))
            endpoint += "?" + urllib.parse.urlencode({"key": api_key})
            headers = {"Content-Type": "application/json"}
            payload = {"contents": [{"parts": [{"text": "Reply with OK"}]}],
                       "generationConfig": {"maxOutputTokens": 2}}
        status, data = await asyncio.to_thread(
            _http_json, endpoint, headers=headers, payload=payload,
        )
        latency = int((time.monotonic() - started) * 1000)
        if 200 <= status < 300:
            return JSONResponse({"ok": True, "message": "Provider connected", "latency_ms": latency})
        return JSONResponse(
            {"ok": False, "error": _error_message(data, f"Provider returned HTTP {status}"),
             "latency_ms": latency}, status_code=422,
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return JSONResponse({"ok": False, "error": f"Connection failed: {exc}"}, status_code=502)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)[:300]}, status_code=500)


async def api_provider_models(request: Request):
    if err := guard(request): return err
    try:
        body = await request.json()
        provider = str(body.get("provider", ""))
        spec = PROVIDER_TESTS.get(provider)
        endpoint = PROVIDER_MODEL_URLS.get(provider)
        if not spec or not endpoint:
            return JSONResponse({"error": "Model catalog is not available for this provider"}, status_code=400)
        key_name, _, protocol = spec
        api_key = resolve_secret(str(body.get("api_key", "")), key_name)
        if not api_key:
            return JSONResponse({"error": "API key is required"}, status_code=400)

        headers = {"Authorization": f"Bearer {api_key}"}
        if protocol == "anthropic":
            headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
        elif protocol == "gemini":
            endpoint += "&" + urllib.parse.urlencode({"key": api_key})
            headers = {}

        status, data = await asyncio.to_thread(_http_json, endpoint, headers=headers)
        if not 200 <= status < 300:
            return JSONResponse(
                {"error": _error_message(data, f"Provider returned HTTP {status}")},
                status_code=422,
            )

        raw_models = data.get("models", []) if protocol == "gemini" else data.get("data", [])
        models = []
        excluded_openai_prefixes = (
            "text-embedding", "whisper", "tts-", "dall-e", "omni-moderation",
        )
        for item in raw_models:
            if not isinstance(item, dict):
                continue
            if protocol == "gemini":
                methods = item.get("supportedGenerationMethods", [])
                if "generateContent" not in methods:
                    continue
                model_id = str(item.get("baseModelId") or item.get("name", "")).removeprefix("models/")
                name = item.get("displayName") or model_id
                context = item.get("inputTokenLimit")
            else:
                model_id = str(item.get("id") or item.get("name", "")).removeprefix("models/")
                if provider == "OpenAI" and model_id.startswith(excluded_openai_prefixes):
                    continue
                name = item.get("name") or item.get("display_name") or model_id
                context = item.get("context_length") or item.get("input_token_limit")
            if model_id:
                models.append({"id": model_id, "name": str(name), "context_length": context})

        models.sort(key=lambda model: model["id"].lower())
        return JSONResponse({"ok": True, "provider": provider, "models": models[:1000],
                             "count": min(len(models), 1000)})
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return JSONResponse({"error": f"Could not load models: {exc}"}, status_code=502)
    except Exception as exc:
        return JSONResponse({"error": str(exc)[:300]}, status_code=500)


def _test_channel_sync(channel: str, values: dict[str, str]) -> tuple[bool, str]:
    if channel == "telegram":
        token = values.get("TELEGRAM_BOT_TOKEN", "")
        status, data = _http_json(f"https://api.telegram.org/bot{token}/getMe")
        name = data.get("result", {}).get("username", "")
        return status == 200 and bool(data.get("ok")), f"Connected as @{name}" if name else _error_message(data, "Invalid bot token")
    if channel == "discord":
        status, data = _http_json(
            "https://discord.com/api/v10/users/@me",
            headers={"Authorization": f"Bot {values.get('DISCORD_BOT_TOKEN', '')}"},
        )
        name = data.get("username", "")
        return status == 200, f"Connected as {name}" if name else _error_message(data, "Invalid bot token")
    if channel == "slack":
        payload = urllib.parse.urlencode({"token": values.get("SLACK_BOT_TOKEN", "")}).encode()
        req = urllib.request.Request("https://slack.com/api/auth.test", data=payload, method="POST")
        with urllib.request.urlopen(req, timeout=15) as res:
            data = json.loads(res.read().decode())
        return bool(data.get("ok")), f"Connected to {data.get('team', 'Slack')}" if data.get("ok") else data.get("error", "Invalid token")
    if channel == "mattermost":
        base = values.get("MATTERMOST_URL", "").rstrip("/")
        status, data = _http_json(
            f"{base}/api/v4/users/me",
            headers={"Authorization": f"Bearer {values.get('MATTERMOST_TOKEN', '')}"},
        )
        return status == 200, f"Connected as {data.get('username', 'bot')}" if status == 200 else _error_message(data, "Connection failed")
    if channel == "matrix":
        base = values.get("MATRIX_HOMESERVER", "").rstrip("/")
        status, data = _http_json(
            f"{base}/_matrix/client/v3/account/whoami",
            headers={"Authorization": f"Bearer {values.get('MATRIX_ACCESS_TOKEN', '')}"},
        )
        return status == 200, f"Connected as {data.get('user_id', 'user')}" if status == 200 else _error_message(data, "Connection failed")
    if channel == "email":
        host = values.get("EMAIL_IMAP_HOST", "")
        client = imaplib.IMAP4_SSL(host, timeout=15)
        try:
            client.login(values.get("EMAIL_ADDRESS", ""), values.get("EMAIL_PASSWORD", ""))
            return True, f"Connected to {host}"
        finally:
            try: client.logout()
            except Exception: pass
    if channel == "whatsapp":
        return True, "WhatsApp pairing is verified after the gateway starts"
    return False, "Unsupported channel"


async def api_test_channel(request: Request):
    if err := guard(request): return err
    try:
        body = await request.json()
        channel = str(body.get("channel", "")).lower()
        submitted = body.get("vars", {})
        allowed = {k for k, _, _, _ in ENV_VARS}
        existing = read_env(ENV_FILE)
        values = {
            k: resolve_secret(str(v), k)
            for k, v in submitted.items() if k in allowed
        }
        values = {**existing, **values}
        started = time.monotonic()
        ok, message = await asyncio.to_thread(_test_channel_sync, channel, values)
        result = {"ok": ok, "message" if ok else "error": message,
                  "latency_ms": int((time.monotonic() - started) * 1000)}
        return JSONResponse(result, status_code=200 if ok else 422)
    except (urllib.error.URLError, TimeoutError, OSError, imaplib.IMAP4.error) as exc:
        return JSONResponse({"ok": False, "error": f"Connection failed: {exc}"}, status_code=502)
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)[:300]}, status_code=500)


async def api_diagnostics(request: Request):
    if err := guard(request): return err
    data = read_env(ENV_FILE)
    if request.method == "POST":
        try:
            submitted = (await request.json()).get("vars", {})
            allowed = {k for k, _, _, _ in ENV_VARS}
            data.update(unmask({k: str(v) for k, v in submitted.items() if k in allowed}, data))
        except Exception:
            return JSONResponse({"error": "Invalid diagnostics payload"}, status_code=400)
    home = Path(HERMES_HOME)
    checks = []

    def add(name: str, ok: bool, detail: str, level: str = "error"):
        checks.append({"name": name, "ok": ok, "level": "ok" if ok else level, "detail": detail})

    add("Hermes home", home.exists() and os.access(home, os.W_OK),
        f"{home} is writable" if home.exists() and os.access(home, os.W_OK) else f"{home} is not writable")
    hermes_bin = shutil.which("hermes")
    add("Hermes executable", bool(hermes_bin), hermes_bin or "hermes was not found in PATH")
    providers = [label for key, label, cat, _ in ENV_VARS if cat == "provider" and data.get(key)]
    add("LLM provider", bool(providers), ", ".join(providers) if providers else "No provider API key configured")
    add("Model", bool(data.get("LLM_MODEL")), data.get("LLM_MODEL") or "No model configured")
    channels = [name for name, key in CHANNEL_MAP.items()
                if data.get(key, "").lower() not in ("", "false", "0", "no")]
    add("Messaging channel", bool(channels), ", ".join(channels) if channels else "No channel configured")
    add("Gateway", gw.state == "running", f"Gateway is {gw.state}",
        "warning" if gw.state in ("stopped", "starting", "stopping") else "error")
    try:
        usage = shutil.disk_usage(home if home.exists() else home.parent)
        free_mb = usage.free // (1024 * 1024)
        add("Storage", free_mb >= 100, f"{free_mb:,} MB free", "warning")
    except OSError as exc:
        add("Storage", False, str(exc), "warning")

    version = "unknown"
    if hermes_bin:
        try:
            proc = await asyncio.create_subprocess_exec(
                hermes_bin, "--version", stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            raw, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
            version = raw.decode(errors="replace").strip().splitlines()[0][:100] or "unknown"
        except Exception:
            pass
    return JSONResponse({
        "ok": not any(c["level"] == "error" for c in checks),
        "checks": checks,
        "summary": {
            "hermes_version": version,
            "gateway_state": gw.state,
            "configured_providers": len(providers),
            "configured_channels": len(channels),
        },
    })


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
    Route("/login",                     page_login, methods=["GET", "POST"]),
    Route("/health",                    route_health),
    Route("/logout",                    route_logout),
    Route("/api/config",                api_config_get,      methods=["GET"]),
    Route("/api/config",                api_config_put,      methods=["PUT"]),
    Route("/api/status",                api_status),
    Route("/api/provider/activate",     api_provider_activate, methods=["POST"]),
    Route("/api/provider/remove",       api_provider_remove, methods=["POST"]),
    Route("/api/channel/remove",        api_channel_remove, methods=["POST"]),
    Route("/api/logs",                  api_logs),
    Route("/api/gateway/start",         api_gw_start,        methods=["POST"]),
    Route("/api/gateway/stop",          api_gw_stop,         methods=["POST"]),
    Route("/api/gateway/restart",       api_gw_restart,      methods=["POST"]),
    Route("/api/config/reset",          api_config_reset,    methods=["POST"]),
    Route("/api/setup/test-provider",   api_test_provider,    methods=["POST"]),
    Route("/api/setup/models",          api_provider_models,  methods=["POST"]),
    Route("/api/setup/test-channel",    api_test_channel,     methods=["POST"]),
    Route("/api/diagnostics",           api_diagnostics,     methods=["GET", "POST"]),
    Route("/api/pairing/pending",       api_pairing_pending),
    Route("/api/pairing/approve",       api_pairing_approve, methods=["POST"]),
    Route("/api/pairing/deny",          api_pairing_deny,    methods=["POST"]),
    Route("/api/pairing/approved",      api_pairing_approved),
    Route("/api/pairing/revoke",        api_pairing_revoke,  methods=["POST"]),
]

app = Starlette(
    routes=routes,
    middleware=[Middleware(AuthenticationMiddleware, backend=AdminAuth())],
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
