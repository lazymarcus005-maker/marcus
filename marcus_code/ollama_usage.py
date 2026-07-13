import asyncio
import contextlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

OLLAMA_PROFILE_DIR = Path.home() / ".marcus" / "ollama-browser-profile"
OLLAMA_PROFILE_CACHE_FILE = Path.home() / ".marcus" / "ollama-profile.json"
OLLAMA_STORAGE_STATE_FILE = Path.home() / ".marcus" / "ollama-storage-state.json"


@dataclass(frozen=True)
class UsagePeriod:
    percent: float | None
    resets_in: str | None


@dataclass(frozen=True)
class OllamaCloudUsage:
    session: UsagePeriod | None
    weekly: UsagePeriod | None
    email: str | None = None


class OllamaUsageError(RuntimeError):
    pass


def is_ollama_cloud(base_url: str) -> bool:
    hostname = (urlparse(base_url).hostname or "").lower()
    return hostname == "ollama.com" or hostname.endswith(".ollama.com")


def parse_ollama_usage(text: str) -> OllamaCloudUsage | None:
    def extract(label: str) -> UsagePeriod | None:
        match = re.search(re.escape(label), text, flags=re.IGNORECASE)
        if not match:
            return None
        section = text[match.start() : match.start() + 500]
        percent_match = re.search(r"(\d+(?:\.\d+)?)\s*%\s*used", section, re.IGNORECASE)
        reset_match = re.search(r"Resets?\s+in\s+([^\n.]+)", section, re.IGNORECASE)
        if not percent_match and not reset_match:
            return None
        return UsagePeriod(
            percent=float(percent_match.group(1)) if percent_match else None,
            resets_in=reset_match.group(1).strip() if reset_match else None,
        )

    session = extract("Session usage")
    weekly = extract("Weekly usage")
    if session is None and weekly is None:
        return None
    email_match = re.search(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", text, re.IGNORECASE)
    return OllamaCloudUsage(
        session=session,
        weekly=weekly,
        email=email_match.group(0) if email_match else None,
    )


def load_cached_ollama_email(cache_file: Path = OLLAMA_PROFILE_CACHE_FILE) -> str | None:
    try:
        value = json.loads(cache_file.read_text(encoding="utf-8")).get("email")
    except (OSError, ValueError, AttributeError):
        return None
    return value if isinstance(value, str) and value else None


def save_cached_ollama_email(email: str, cache_file: Path = OLLAMA_PROFILE_CACHE_FILE) -> None:
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps({"email": email}, indent=2), encoding="utf-8")
    with contextlib.suppress(OSError):
        os.chmod(cache_file, 0o600)


def find_installed_browsers() -> list[Path]:
    """Return real browser executables, preferring Google Chrome."""
    candidates: list[Path] = []
    for command in ("chrome", "google-chrome", "chromium", "msedge"):
        if executable := shutil.which(command):
            candidates.append(Path(executable))

    if os.name == "nt":
        locations = (
            ("PROGRAMFILES", "Google/Chrome/Application/chrome.exe"),
            ("PROGRAMFILES(X86)", "Google/Chrome/Application/chrome.exe"),
            ("LOCALAPPDATA", "Google/Chrome/Application/chrome.exe"),
            ("PROGRAMFILES", "Microsoft/Edge/Application/msedge.exe"),
            ("PROGRAMFILES(X86)", "Microsoft/Edge/Application/msedge.exe"),
        )
        candidates.extend(
            Path(root) / relative
            for env_name, relative in locations
            if (root := os.environ.get(env_name))
        )
    elif sys.platform == "darwin":
        candidates.extend(
            (
                Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
            )
        )

    found = []
    for candidate in candidates:
        if candidate.is_file() and candidate not in found:
            found.append(candidate)
    return found


def _unused_local_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def interactive_browser_args(profile_dir: Path, port: int) -> tuple[str, ...]:
    return (
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "https://ollama.com/settings",
    )


class OllamaCloudUsageClient:
    def __init__(
        self,
        profile_dir: Path = OLLAMA_PROFILE_DIR,
        storage_state_file: Path = OLLAMA_STORAGE_STATE_FILE,
    ) -> None:
        self.profile_dir = profile_dir
        self.storage_state_file = storage_state_file

    @property
    def has_profile(self) -> bool:
        return self.storage_state_file.is_file()

    def logout(self, *, cache_file: Path = OLLAMA_PROFILE_CACHE_FILE) -> int:
        """Delete saved browser credentials and cached identity for Ollama Cloud."""
        removed = 0
        for path in (self.storage_state_file, cache_file):
            if path.is_file():
                path.unlink()
                removed += 1

        profile = self.profile_dir.resolve()
        marcus_home = (Path.home() / ".marcus").resolve()
        if profile.is_dir():
            if profile == marcus_home or marcus_home not in profile.parents:
                raise OllamaUsageError("refusing to remove browser profile outside ~/.marcus")
            shutil.rmtree(profile)
            removed += 1
        return removed

    async def fetch(self, *, interactive: bool = False) -> OllamaCloudUsage:
        if not interactive:
            return await self._fetch_via_http()

        try:
            from playwright.async_api import Error as PlaywrightError
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise OllamaUsageError(
                "Playwright is not installed; install Marcus with the ollama-usage extra."
            ) from exc

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        async with async_playwright() as playwright:
            browser, context, browser_process = await self._open_login_browser(
                playwright, PlaywrightError
            )

            try:
                page = context.pages[0] if context.pages else await context.new_page()
                await page.goto(
                    "https://ollama.com/settings",
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
                deadline = asyncio.get_running_loop().time() + 180
                while asyncio.get_running_loop().time() < deadline:
                    usage = parse_ollama_usage(await page.locator("body").inner_text())
                    if usage is not None:
                        self.storage_state_file.parent.mkdir(parents=True, exist_ok=True)
                        await context.storage_state(path=str(self.storage_state_file))
                        with contextlib.suppress(OSError):
                            os.chmod(self.storage_state_file, 0o600)
                        if usage.email:
                            save_cached_ollama_email(usage.email)
                        return usage
                    await asyncio.sleep(1)
                raise OllamaUsageError(
                    "Could not read usage after login; Ollama may have changed the settings page."
                )
            except PlaywrightError as exc:
                raise OllamaUsageError(f"Could not read Ollama settings: {exc}") from exc
            finally:
                await browser.close()
                if browser_process is not None and browser_process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        browser_process.terminate()
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(browser_process.wait(), timeout=5)

    async def _open_login_browser(self, playwright, playwright_error):
        executables = find_installed_browsers()
        if not executables:
            raise OllamaUsageError(
                "Google Chrome or Microsoft Edge was not found for secure interactive login."
            )
        executable = executables[0]
        port = _unused_local_port()
        process_group_args = (
            {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}
        )
        process = await asyncio.create_subprocess_exec(
            str(executable),
            *interactive_browser_args(self.profile_dir, port),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            **process_group_args,
        )
        deadline = asyncio.get_running_loop().time() + 30
        while asyncio.get_running_loop().time() < deadline:
            try:
                browser = await playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                if not browser.contexts:
                    raise OllamaUsageError("Browser opened without a usable profile context.")
                return browser, browser.contexts[0], process
            except playwright_error:
                await asyncio.sleep(0.25)
        if process.returncode is None:
            process.terminate()
            await process.wait()
        raise OllamaUsageError("Chrome opened but its secure login session could not be attached.")

    async def _fetch_via_http(self) -> OllamaCloudUsage:
        if not self.storage_state_file.is_file():
            raise OllamaUsageError("Ollama login is not saved yet; run /usage login once.")
        try:
            state = json.loads(self.storage_state_file.read_text(encoding="utf-8"))
            cookies_raw = state.get("cookies", [])
        except (OSError, ValueError, AttributeError) as exc:
            raise OllamaUsageError(
                "Saved Ollama login is unreadable; run /usage login to replace it."
            ) from exc

        cookies = httpx.Cookies()
        for cookie in cookies_raw:
            if not isinstance(cookie, dict) or "ollama.com" not in cookie.get("domain", ""):
                continue
            cookies.set(
                cookie.get("name", ""),
                cookie.get("value", ""),
                domain=cookie.get("domain"),
                path=cookie.get("path", "/"),
            )
        try:
            async with httpx.AsyncClient(
                cookies=cookies, follow_redirects=True, timeout=15
            ) as client:
                response = await client.get("https://ollama.com/settings")
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise OllamaUsageError(f"Could not fetch Ollama settings: {exc}") from exc

        usage = parse_ollama_usage(response.text)
        if usage is None:
            raise OllamaUsageError(
                "Saved Ollama login is missing or expired; run /usage login to refresh it."
            )
        if usage.email:
            save_cached_ollama_email(usage.email)
        return usage
