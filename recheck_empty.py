#!/usr/bin/env python3
"""
Recheck all key files every 30 min (cron):
  - Per-type funded files (gemini_keys.txt, openai_keys.txt, ...):
      if a key lost funds → move to empty_keys.txt
  - empty_keys.txt:
      if a key got funds  → move to the correct per-type file
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import re
import time
from collections import defaultdict
from typing import Any

import aiohttp
from fake_useragent import UserAgent

# ── logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── dotenv ───────────────────────────────────────────────────────────────────
def _load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip() and not line.startswith("#") and "=" in line:
                key, val = line.strip().split("=", 1)
                os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

EMPTY_TXT = os.getenv("EMPTY_TXT", "empty_keys.txt")
EMPTY_PATH = os.path.join(SCRIPT_DIR, EMPTY_TXT)

# Per-type funded key files (only OpenAI + Anthropic)
FUNDED_FILES: dict[str, str] = {
    "OpenAI": "openai_keys.txt",
    "Anthropic": "anthropic_keys.txt",
}

CONCURRENCY = 10

# ── key type detection ───────────────────────────────────────────────────────
KEY_PATTERNS: dict[str, re.Pattern[str]] = {
    "Anthropic": re.compile(r"^sk-ant-api\d{2}-[a-zA-Z0-9\-_]{80,150}$"),
    "OpenAI": re.compile(r"^sk-(?:proj-|svcacct-)?[a-zA-Z0-9\-_]{40,250}$"),
}


def detect_key_type(key: str) -> str | None:
    for name, pat in KEY_PATTERNS.items():
        if pat.match(key):
            return name
    return None


# ── http helpers ─────────────────────────────────────────────────────────────
@dataclasses.dataclass(frozen=True, slots=True)
class HttpResult:
    status: int
    body: str
    json_data: dict[str, Any] | None = None


class HttpClient:
    def __init__(self, semaphore: asyncio.Semaphore) -> None:
        self._semaphore = semaphore
        self._ua = UserAgent()
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(limit=50, limit_per_host=10, ttl_dns_cache=300)
            self._session = aiohttp.ClientSession(
                connector=connector,
                timeout=aiohttp.ClientTimeout(total=20),
            )
        return self._session

    async def request(
        self, method: str, url: str, *,
        headers: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> HttpResult | None:
        merged: dict[str, str] = {"User-Agent": self._ua.random}
        if headers:
            merged.update(headers)
        session = await self._get_session()
        req_timeout = aiohttp.ClientTimeout(total=timeout) if timeout else None
        try:
            async with self._semaphore:
                async with session.request(method, url, headers=merged, json=json_body, timeout=req_timeout) as resp:
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
                    return HttpResult(status=resp.status, body=text, json_data=json_data)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("HTTP %s %s failed: %s", method, url[:80], exc)
            return None

    async def get(self, url: str, **kw: Any) -> HttpResult | None:
        return await self.request("GET", url, **kw)

    async def post(self, url: str, **kw: Any) -> HttpResult | None:
        return await self.request("POST", url, **kw)

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


# ── verifiers ────────────────────────────────────────────────────────────────
class KeyVerifier:
    def __init__(self, http: HttpClient) -> None:
        self._http = http

    async def verify(self, key_type: str, key: str) -> tuple[str, str]:
        if key_type == "OpenAI":
            return await self._verify_openai(key)
        if key_type == "Anthropic":
            return await self._verify_anthropic(key)
        return "UNKNOWN", "No verifier"

    async def _verify_openai(self, key: str) -> tuple[str, str]:
        """2-step: models (alive?) → completions (credit + billing active?)."""
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

        # Step 1: alive check
        result = await self._http.get("https://api.openai.com/v1/models", headers=headers, timeout=10.0)
        if result is None:
            return "ERROR", "Network"
        if result.status == 401:
            return "DEAD", "Invalid"
        if result.status == 403:
            body = result.json_data or {}
            msg = body.get("error", {}).get("message", "").lower()
            err_type = body.get("error", {}).get("type", "")
            if "not active" in msg or "billing" in msg or err_type == "billing_not_active":
                return "EMPTY", "Account Not Active"
            return "DEAD", "Forbidden"
        if result.status == 429:
            body = result.json_data or {}
            err_type = body.get("error", {}).get("type", "")
            if err_type == "insufficient_quota":
                return "EMPTY", "No Credit ($0)"
            if err_type == "billing_not_active":
                return "EMPTY", "Billing Not Active"
        if result.status not in (200, 429):
            return "UNKNOWN", f"HTTP {result.status}"

        # Step 2: credit + billing check
        result = await self._http.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers,
            json_body={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "1"}], "max_tokens": 2},
            timeout=15.0,
        )
        if result is None:
            return "FUNDED", "Alive (timeout)"
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
        headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "Content-Type": "application/json"}
        result = await self._http.get("https://api.anthropic.com/v1/models", headers=headers, timeout=10.0)
        if result is None:
            return "ERROR", "Network"
        if result.status == 401:
            return "DEAD", "Invalid"
        if result.status == 403:
            return "DEAD", "Forbidden"
        result = await self._http.post(
            "https://api.anthropic.com/v1/messages",
            headers=headers,
            json_body={"model": "claude-3-5-haiku-20241022", "max_tokens": 1, "messages": [{"role": "user", "content": "1"}]},
            timeout=15.0,
        )
        if result is None:
            return "FUNDED", "Alive (timeout)"
        if result.status == 200:
            return "FUNDED", "Has Credit ($$)"
        if result.status == 429:
            body = result.json_data or {}
            if body.get("error", {}).get("type") == "rate_limit_error":
                return "FUNDED", "Rate Limited (has credit)"
            return "EMPTY", "Quota Exceeded"
        if result.status == 400:
            body = result.json_data or {}
            msg = body.get("error", {}).get("message", "").lower()
            if "credit" in msg or "billing" in msg:
                return "EMPTY", "No Credit ($0)"
            return "FUNDED", "Alive (request rejected)"
        if result.status == 401:
            return "DEAD", "Invalid"
        return "UNKNOWN", f"HTTP {result.status}"


# ── file helpers ─────────────────────────────────────────────────────────────
def read_keys(path: str) -> list[str]:
    """Read keys from file, stripping line numbers/whitespace."""
    if not os.path.exists(path):
        return []
    keys: list[str] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) == 2 and parts[0].isdigit():
                keys.append(parts[1])
            else:
                keys.append(line)
    return keys


def write_keys(path: str, keys: list[str]) -> None:
    """Overwrite file with keys, one per line."""
    with open(path, "w", encoding="utf-8") as f:
        for key in keys:
            f.write(f"{key}\n")
        f.flush()
        os.fsync(f.fileno())


def append_keys(path: str, new_keys: list[str]) -> None:
    """Append keys to file."""
    with open(path, "a", encoding="utf-8") as f:
        for key in new_keys:
            f.write(f"{key}\n")
        f.flush()
        os.fsync(f.fileno())


def funded_path(key_type: str) -> str:
    """Get absolute path for a per-type funded file."""
    filename = FUNDED_FILES.get(key_type, "openai_keys.txt")
    return os.path.join(SCRIPT_DIR, filename)


# ── main logic ───────────────────────────────────────────────────────────────
async def recheck() -> None:
    sem = asyncio.Semaphore(CONCURRENCY)
    http = HttpClient(sem)
    verifier = KeyVerifier(http)

    total_moved_to_empty = 0
    total_moved_to_funded = 0
    total_checked = 0

    # ── Phase 1: recheck all per-type funded files ───────────────────────
    logger.info("=== Phase 1: Rechecking per-type funded files ===")

    for key_type, filename in FUNDED_FILES.items():
        fpath = os.path.join(SCRIPT_DIR, filename)
        keys = read_keys(fpath)
        if not keys:
            continue

        logger.info("[%s] Checking %d keys from %s ...", key_type, len(keys), filename)

        still_funded: list[str] = []
        became_empty: list[str] = []

        for i, key in enumerate(keys, 1):
            total_checked += 1
            detected = detect_key_type(key)
            actual_type = detected or key_type
            short = f"{key[:12]}...{key[-4:]}"

            status, detail = await verifier.verify(actual_type, key)

            if status == "FUNDED":
                still_funded.append(key)
                logger.info("  [%d/%d] FUNDED  %s — %s", i, len(keys), short, detail)
            else:
                became_empty.append(key)
                logger.info("  [%d/%d] %s  %s — %s", i, len(keys), status, short, detail)

            await asyncio.sleep(0.5)

        # Rewrite funded file without dead/empty keys
        if became_empty:
            write_keys(fpath, still_funded)
            append_keys(EMPTY_PATH, became_empty)
            total_moved_to_empty += len(became_empty)
            logger.info("[%s] Moved %d key(s) to %s, %d remain in %s",
                        key_type, len(became_empty), EMPTY_TXT, len(still_funded), filename)

    # ── Phase 2: recheck empty_keys.txt ──────────────────────────────────
    empty_keys = read_keys(EMPTY_PATH)
    if empty_keys:
        logger.info("=== Phase 2: Rechecking %d keys from %s ===", len(empty_keys), EMPTY_TXT)

        still_empty: list[str] = []
        # Group newly funded keys by type
        newly_funded: dict[str, list[str]] = defaultdict(list)

        # Load all existing funded keys to avoid duplicates
        all_funded: set[str] = set()
        for _, fname in FUNDED_FILES.items():
            all_funded.update(read_keys(os.path.join(SCRIPT_DIR, fname)))

        for i, key in enumerate(empty_keys, 1):
            total_checked += 1
            key_type = detect_key_type(key)
            short = f"{key[:12]}...{key[-4:]}"

            if key_type is None:
                logger.warning("  [%d/%d] Unknown type, keeping in empty: %s", i, len(empty_keys), short)
                still_empty.append(key)
                continue

            status, detail = await verifier.verify(key_type, key)

            if status == "FUNDED":
                if key not in all_funded:
                    newly_funded[key_type].append(key)
                    logger.info("  [%d/%d] FUNDED  %s  %s — %s", i, len(empty_keys), key_type, short, detail)
                else:
                    still_empty.append(key)
                    logger.info("  [%d/%d] FUNDED (already exists) %s %s", i, len(empty_keys), key_type, short)
            else:
                still_empty.append(key)
                logger.info("  [%d/%d] %s  %s  %s — %s", i, len(empty_keys), status, key_type, short, detail)

            await asyncio.sleep(0.5)

        # Append newly funded to per-type files
        for key_type, keys_list in newly_funded.items():
            fpath = funded_path(key_type)
            append_keys(fpath, keys_list)
            total_moved_to_funded += len(keys_list)
            logger.info("Moved %d %s key(s) to %s", len(keys_list), key_type, FUNDED_FILES[key_type])

        # Rewrite empty_keys without the funded ones
        write_keys(EMPTY_PATH, still_empty)
    else:
        logger.info("No keys in %s, skipping phase 2.", EMPTY_TXT)

    await http.close()

    # ── Summary ──────────────────────────────────────────────────────────
    logger.info("=" * 60)
    logger.info("SUMMARY:")
    logger.info("  Total checked     : %d", total_checked)
    logger.info("  Moved to empty    : %d", total_moved_to_empty)
    logger.info("  Moved to funded   : %d", total_moved_to_funded)
    for key_type, filename in FUNDED_FILES.items():
        fpath = os.path.join(SCRIPT_DIR, filename)
        count = len(read_keys(fpath))
        if count > 0:
            logger.info("  %-18s: %d keys", filename, count)
    logger.info("  %-18s: %d keys", EMPTY_TXT, len(read_keys(EMPTY_PATH)))
    logger.info("=" * 60)


def main() -> None:
    start = time.time()
    asyncio.run(recheck())
    elapsed = time.time() - start
    logger.info("Finished in %.1f seconds", elapsed)


if __name__ == "__main__":
    main()
