from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

import httpx
from dotenv import load_dotenv
from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright
from rich.console import Console


os.environ.setdefault("AI_SDK_LOG_WARNINGS", "false")
os.environ.setdefault("LOG_LEVEL", "error")
os.environ.setdefault("PINO_LOG_LEVEL", "error")

TRUTHY = {"1", "true", "yes"}
console = Console()


def env_str(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def env_flag(name: str, default: str = "false") -> bool:
    return env_str(name, default).lower() in TRUTHY


def debug_enabled() -> bool:
    return env_flag("STAGEHAND_DEBUG", "false")


def model_options(
    model_name: str,
    model_api_key: Optional[str],
    model_base_url: Optional[str],
) -> dict[str, Any]:
    cfg: dict[str, Any] = {"modelName": model_name}
    if model_api_key:
        cfg["apiKey"] = model_api_key
    if model_base_url:
        cfg["baseURL"] = model_base_url
    return {"model": cfg}


def scoped_model_options(model_opts: dict[str, Any], selector: Optional[str]) -> dict[str, Any]:
    if not selector:
        return model_opts
    scoped = dict(model_opts)
    scoped["selector"] = selector
    return scoped


async def sleep_ms(ms: int) -> None:
    await asyncio.sleep(ms / 1000)


def load_storage_state_payload(session_state_path: Optional[Path]) -> Optional[dict[str, Any]]:
    if not session_state_path or not session_state_path.exists():
        return None
    try:
        payload = json.loads(session_state_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return payload if isinstance(payload, dict) else None


async def apply_storage_state_to_context(context: BrowserContext, payload: Optional[dict[str, Any]]) -> None:
    if not payload:
        return

    cookies = payload.get("cookies")
    if isinstance(cookies, list) and cookies:
        try:
            await context.add_cookies(cookies)
        except Exception:
            pass

    origins = payload.get("origins")
    if isinstance(origins, list) and origins:
        try:
            await context.add_init_script(
                """(origins) => {
                    const current = window.location.origin;
                    const match = origins.find((item) => item && item.origin === current);
                    if (!match || !Array.isArray(match.localStorage)) return;
                    for (const entry of match.localStorage) {
                      if (!entry || typeof entry.name !== "string") continue;
                      try {
                        window.localStorage.setItem(entry.name, String(entry.value ?? ""));
                      } catch (error) {
                      }
                    }
                }""",
                origins,
            )
        except Exception:
            pass


def cleanup_browser_processes() -> None:
    patterns = [
        "stagehand",
        "playwright.*chrom",
        "chromium",
        "chrome.*remote-debugging-port",
    ]
    for pattern in patterns:
        try:
            subprocess.run(["pkill", "-f", pattern], check=False, capture_output=True)
        except Exception:
            pass


def configure_local_stagehand_server_logging() -> None:
    if debug_enabled():
        return
    try:
        from stagehand.lib import sea_server as stagehand_sea_server  # type: ignore
    except Exception:
        return
    if getattr(stagehand_sea_server, "_jsb_quiet_patch_applied", False):
        return
    original_popen = stagehand_sea_server.subprocess.Popen

    def _quiet_popen(*args: Any, **kwargs: Any) -> Any:
        cmd0 = None
        if args and isinstance(args[0], (list, tuple)) and args[0]:
            cmd0 = str(args[0][0])
        elif args and isinstance(args[0], str):
            cmd0 = args[0]
        elif isinstance(kwargs.get("args"), (list, tuple)) and kwargs.get("args"):
            cmd0 = str(kwargs["args"][0])
        elif isinstance(kwargs.get("args"), str):
            cmd0 = kwargs["args"]

        is_stagehand_binary = bool(cmd0 and "stagehand" in cmd0 and ("_sea" in cmd0 or "darwin" in cmd0))
        if not is_stagehand_binary:
            return original_popen(*args, **kwargs)

        env = kwargs.get("env")
        if isinstance(env, dict):
            env.setdefault("LOG_LEVEL", "error")
            env.setdefault("PINO_LOG_LEVEL", "error")
            env.setdefault("AI_SDK_LOG_WARNINGS", "false")
        kwargs["stdout"] = subprocess.DEVNULL
        kwargs["stderr"] = subprocess.DEVNULL
        return original_popen(*args, **kwargs)

    stagehand_sea_server.subprocess.Popen = _quiet_popen
    stagehand_sea_server._jsb_quiet_patch_applied = True


def normalize_page_url(url: str) -> str:
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


class LoggedStagehandSession:
    def __init__(self, session: Any, enable_logs: bool = True) -> None:
        self._session = session
        self._enable_logs = enable_logs
        self._counts: dict[str, int] = {}
        self._dur_ms: dict[str, int] = {}
        self._fingerprints: dict[str, int] = {}
        self._failures: dict[str, int] = {}

    def _page_url(self, kwargs: dict[str, Any]) -> str:
        page = kwargs.get("page")
        return normalize_page_url(getattr(page, "url", "")) if page is not None else "-"

    def _instruction(self, kwargs: dict[str, Any]) -> str:
        for key in ("instruction", "input"):
            value = kwargs.get(key)
            if isinstance(value, str):
                return value.strip().replace("\n", " ")[:180]
        execute_opts = kwargs.get("execute_options")
        if isinstance(execute_opts, dict):
            value = execute_opts.get("instruction")
            if isinstance(value, str):
                return value.strip().replace("\n", " ")[:180]
        return "-"

    def _log(self, action: str, ok: bool, duration_ms: int, kwargs: dict[str, Any]) -> None:
        self._counts[action] = self._counts.get(action, 0) + 1
        self._dur_ms[action] = self._dur_ms.get(action, 0) + duration_ms
        page_url = self._page_url(kwargs)
        instruction = self._instruction(kwargs)
        fingerprint = f"{action}|{page_url}|{instruction}"
        self._fingerprints[fingerprint] = self._fingerprints.get(fingerprint, 0) + 1
        redundant = self._fingerprints[fingerprint] > 1

        if ok:
            self._failures[action] = 0
        else:
            self._failures[action] = self._failures.get(action, 0) + 1

        if self._enable_logs:
            console.print(
                f"ACTION_{action.upper()} ok={str(ok).lower()} ms={duration_ms} "
                f"page={page_url} instruction={instruction}"
            )
            if redundant:
                console.print(f"ACTION_REDUNDANT={action} count={self._fingerprints[fingerprint]} page={page_url}")

    def should_skip(self, action: str, max_failures: int = 3) -> bool:
        return self._failures.get(action, 0) >= max_failures

    async def _call(self, action: str, fn: Any, kwargs: dict[str, Any]) -> Any:
        started = time.perf_counter()
        try:
            result = await fn(**kwargs)
            duration_ms = int((time.perf_counter() - started) * 1000)
            self._log(action, True, duration_ms, kwargs)
            return result
        except Exception:
            duration_ms = int((time.perf_counter() - started) * 1000)
            self._log(action, False, duration_ms, kwargs)
            raise

    async def observe(self, **kwargs: Any) -> Any:
        return await self._call("observe", self._session.observe, kwargs)

    async def extract(self, **kwargs: Any) -> Any:
        return await self._call("extract", self._session.extract, kwargs)

    async def act(self, **kwargs: Any) -> Any:
        return await self._call("act", self._session.act, kwargs)

    async def execute(self, **kwargs: Any) -> Any:
        return await self._call("execute", self._session.execute, kwargs)

    async def navigate(self, **kwargs: Any) -> Any:
        return await self._call("navigate", self._session.navigate, kwargs)

    async def end(self, **kwargs: Any) -> Any:
        return await self._call("end", self._session.end, kwargs)

    def print_summary(self) -> None:
        for action in sorted(self._counts):
            console.print(
                f"ACTION_SUMMARY_{action.upper()} count={self._counts[action]} total_ms={self._dur_ms.get(action, 0)}"
            )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._session, name)


async def fetch_stagehand_metrics(
    base_url: str,
    model_api_key: Optional[str],
    browserbase_api_key: Optional[str],
    browserbase_project_id: Optional[str],
) -> Optional[dict[str, Any]]:
    headers: dict[str, str] = {}
    if model_api_key:
        headers["x-model-api-key"] = model_api_key
    if browserbase_api_key:
        headers["x-bb-api-key"] = browserbase_api_key
    if browserbase_project_id:
        headers["x-bb-project-id"] = browserbase_project_id
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(f"{base_url.rstrip('/')}/metrics", headers=headers)
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def print_metrics_summary(metrics: Optional[dict[str, Any]]) -> None:
    if not metrics:
        console.print("STAGEHAND_METRICS=unavailable")
        return
    prompt = int(metrics.get("totalPromptTokens") or 0)
    completion = int(metrics.get("totalCompletionTokens") or 0)
    cached = int(metrics.get("totalCachedInputTokens") or 0)
    reasoning = int(metrics.get("totalReasoningTokens") or 0)
    inference_ms = int(metrics.get("totalInferenceTimeMs") or 0)
    llm_input_total = prompt + cached
    cache_ratio = (100.0 * cached / llm_input_total) if llm_input_total > 0 else 0.0

    console.print(f"METRICS_PROMPT_TOKENS={prompt}")
    console.print(f"METRICS_COMPLETION_TOKENS={completion}")
    console.print(f"METRICS_REASONING_TOKENS={reasoning}")
    console.print(f"METRICS_CACHED_INPUT_TOKENS={cached}")
    console.print(f"METRICS_CACHE_RATIO_INPUT_PCT={cache_ratio:.1f}")
    console.print(f"METRICS_INFERENCE_MS={inference_ms}")


def print_replay_summary(replay: Any) -> None:
    try:
        pages = getattr(getattr(replay, "data", None), "pages", None) or []
        cached = 0
        input_tokens = 0
        output_tokens = 0
        actions = 0
        for page in pages:
            for action in getattr(page, "actions", None) or []:
                usage = getattr(action, "token_usage", None)
                if usage is None:
                    continue
                actions += 1
                cached += int(getattr(usage, "cached_input_tokens", 0) or 0)
                input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
                output_tokens += int(getattr(usage, "output_tokens", 0) or 0)
        total_input = cached + input_tokens
        ratio = (100.0 * cached / total_input) if total_input > 0 else 0.0
        console.print(f"STAGEHAND_REPLAY_ACTIONS={actions}")
        console.print(f"STAGEHAND_REPLAY_CACHED_INPUT_TOKENS={cached}")
        console.print(f"STAGEHAND_REPLAY_INPUT_TOKENS={input_tokens}")
        console.print(f"STAGEHAND_REPLAY_OUTPUT_TOKENS={output_tokens}")
        console.print(f"STAGEHAND_REPLAY_CACHE_RATIO_INPUT_PCT={ratio:.1f}")
    except Exception:
        console.print("STAGEHAND_REPLAY_METRICS=unavailable")


class StagehandRuntime:
    def __init__(
        self,
        *,
        client: Any,
        raw_session: Any,
        session: LoggedStagehandSession,
        playwright: Playwright,
        browser: Browser,
        context: BrowserContext,
        page: Page,
        session_state_path: Optional[Path],
        model_name: str,
        model_api_key: Optional[str],
        browserbase_api_key: Optional[str],
        browserbase_project_id: Optional[str],
        model_base_url: Optional[str],
        stagehand_server: str,
        local_headless: bool,
        model_opts: dict[str, Any],
        cache_enabled: bool,
    ) -> None:
        self.client = client
        self.raw_session = raw_session
        self.session = session
        self.playwright = playwright
        self.browser = browser
        self.context = context
        self.page = page
        self.session_state_path = session_state_path
        self.model_name = model_name
        self.model_api_key = model_api_key
        self.browserbase_api_key = browserbase_api_key
        self.browserbase_project_id = browserbase_project_id
        self.model_base_url = model_base_url
        self.stagehand_server = stagehand_server
        self.local_headless = local_headless
        self.model_opts = model_opts
        self.cache_enabled = cache_enabled

    async def ensure_page(self, force_new: bool = False) -> Page:
        if await self._browser_stack_closed():
            await self.restart_browser_session(reason="browser_stack_closed")

        if not force_new:
            try:
                if not self.page.is_closed():
                    return self.page
            except Exception:
                pass
            for candidate in await self._context_pages():
                try:
                    if not candidate.is_closed():
                        self.page = candidate
                        return candidate
                except Exception:
                    continue

        try:
            self.page = await self.context.new_page()
        except Exception:
            await self.restart_browser_session(reason="context_new_page_failed")
            self.page = await self.context.new_page()
        return self.page

    async def restart_browser_session(self, reason: str) -> None:
        console.print(f"RUNTIME_RECOVERY=restart_browser_session reason={reason}")
        self.session.print_summary()
        await self._close_current_stack(end_session=True)
        cleanup_browser_processes()
        stack = await self._start_runtime_stack(
            model_name=self.model_name,
            model_api_key=self.model_api_key,
            model_base_url=self.model_base_url,
            cache_enabled=self.cache_enabled,
            stagehand_server=self.stagehand_server,
            local_headless=self.local_headless,
            browserbase_api_key=self.browserbase_api_key,
            browserbase_project_id=self.browserbase_project_id,
            session_state_path=self.session_state_path,
        )
        self.client = stack["client"]
        self.raw_session = stack["raw_session"]
        self.session = stack["session"]
        self.playwright = stack["playwright"]
        self.browser = stack["browser"]
        self.context = stack["context"]
        self.page = stack["page"]

    async def save_storage_state(self) -> None:
        if not self.session_state_path:
            return
        try:
            self.session_state_path.parent.mkdir(parents=True, exist_ok=True)
            await self.context.storage_state(path=str(self.session_state_path))
        except Exception:
            pass

    async def _context_pages(self) -> list[Page]:
        try:
            return list(self.context.pages)
        except Exception:
            return []

    async def _browser_stack_closed(self) -> bool:
        try:
            if self.page is not None and not self.page.is_closed():
                return False
        except Exception:
            pass
        try:
            pages = self.context.pages
        except Exception:
            return True
        for candidate in pages:
            try:
                if not candidate.is_closed():
                    self.page = candidate
                    return False
            except Exception:
                continue
        return True

    @staticmethod
    async def _close_stack_resources(
        *,
        session: LoggedStagehandSession,
        browser: Browser,
        playwright: Playwright,
        client: Any,
        end_session: bool,
    ) -> None:
        if end_session:
            try:
                await session.end()
            except Exception:
                pass
        try:
            await browser.close()
        except Exception:
            pass
        try:
            await playwright.stop()
        except Exception:
            pass
        try:
            await client.close()
        except Exception:
            pass

    async def _close_current_stack(self, end_session: bool) -> None:
        await self.save_storage_state()
        await self.__class__._close_stack_resources(
            session=self.session,
            browser=self.browser,
            playwright=self.playwright,
            client=self.client,
            end_session=end_session,
        )

    @classmethod
    async def create(cls, *, session_state_path: Optional[str] = None) -> "StagehandRuntime":
        load_dotenv()

        model_name = env_str("MODEL")
        if not model_name:
            raise RuntimeError("MODEL env var is required (example: openai/gpt-5-mini).")

        model_api_key = env_str("LLM_API_KEY") or None
        model_base_url = env_str("LLM_API_BASE") or None
        cache_enabled = env_flag("STAGEHAND_EXECUTE_CACHE", "true")
        stagehand_env = env_str("STAGEHAND_ENV", "LOCAL").lower()
        stagehand_server = "remote" if stagehand_env == "remote" else "local"
        local_headless = env_flag("STAGEHAND_HEADLESS", "false")

        browserbase_api_key = env_str("BROWSERBASE_API_KEY") or None
        browserbase_project_id = env_str("BROWSERBASE_PROJECT_ID") or None

        resolved_session_state_path = Path(session_state_path).expanduser().resolve() if session_state_path else None

        stack = await cls._start_runtime_stack(
            model_name=model_name,
            model_api_key=model_api_key,
            model_base_url=model_base_url,
            cache_enabled=cache_enabled,
            stagehand_server=stagehand_server,
            local_headless=local_headless,
            browserbase_api_key=browserbase_api_key,
            browserbase_project_id=browserbase_project_id,
            session_state_path=resolved_session_state_path,
        )

        return cls(
            client=stack["client"],
            raw_session=stack["raw_session"],
            session=stack["session"],
            playwright=stack["playwright"],
            browser=stack["browser"],
            context=stack["context"],
            page=stack["page"],
            session_state_path=resolved_session_state_path,
            model_name=model_name,
            model_api_key=model_api_key,
            browserbase_api_key=browserbase_api_key,
            browserbase_project_id=browserbase_project_id,
            model_base_url=model_base_url,
            stagehand_server=stagehand_server,
            local_headless=local_headless,
            model_opts=model_options(model_name, model_api_key, model_base_url),
            cache_enabled=cache_enabled,
        )

    @classmethod
    async def _start_runtime_stack(
        cls,
        *,
        model_name: str,
        model_api_key: Optional[str],
        model_base_url: Optional[str],
        cache_enabled: bool,
        stagehand_server: str,
        local_headless: bool,
        browserbase_api_key: Optional[str],
        browserbase_project_id: Optional[str],
        session_state_path: Optional[Path],
    ) -> dict[str, Any]:
        del cache_enabled

        try:
            from stagehand import AsyncStagehand  # type: ignore
        except Exception as exc:
            raise RuntimeError("Stagehand is not installed. Run poetry install.") from exc

        configure_local_stagehand_server_logging()

        client_kwargs: dict[str, Any] = {"server": stagehand_server, "model_api_key": model_api_key}
        if stagehand_server == "local":
            client_kwargs["local_headless"] = local_headless
            if model_api_key:
                client_kwargs["local_openai_api_key"] = model_api_key
        if browserbase_api_key:
            client_kwargs["browserbase_api_key"] = browserbase_api_key
        if browserbase_project_id:
            client_kwargs["browserbase_project_id"] = browserbase_project_id

        client = AsyncStagehand(**client_kwargs)
        raw_session = await client.sessions.start(
            model_name=model_name,
            self_heal=True,
            verbose=1 if debug_enabled() else 0,
            browser={
                "type": "local" if stagehand_server == "local" else "browserbase",
                "launchOptions": {"headless": local_headless},
            },
        )
        if not raw_session.data.cdp_url:
            await client.close()
            raise RuntimeError("Missing cdpUrl from Stagehand session.")

        session = LoggedStagehandSession(raw_session, enable_logs=True)
        playwright = await async_playwright().start()
        browser = await playwright.chromium.connect_over_cdp(raw_session.data.cdp_url)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()
        await apply_storage_state_to_context(context, load_storage_state_payload(session_state_path))
        page: Page = context.pages[0] if context.pages else await context.new_page()
        return {
            "client": client,
            "raw_session": raw_session,
            "session": session,
            "playwright": playwright,
            "browser": browser,
            "context": context,
            "page": page,
        }

    async def close(self) -> None:
        self.session.print_summary()
        try:
            await self._close_current_stack(end_session=True)
        finally:
            cleanup_browser_processes()


async def run_cache_probe(runtime: StagehandRuntime) -> None:
    instruction = "Read current page title and URL in one short sentence."

    async def _probe_once() -> int:
        probe_page = await runtime.context.new_page()
        try:
            await probe_page.goto("data:text/html,<title>Cache Probe</title><p>Cache Probe</p>")
            result = await runtime.session.execute(
                execute_options={"instruction": instruction, "max_steps": 1},
                agent_config={"model": runtime.model_name},
                should_cache=runtime.cache_enabled,
                page=probe_page,
            )
            usage = getattr(getattr(result.data, "result", None), "usage", None)
            return int((getattr(usage, "cached_input_tokens", 0) or 0))
        finally:
            try:
                if not probe_page.is_closed():
                    await probe_page.close()
            except Exception:
                pass

    try:
        cached1 = await _probe_once()
        cached2 = await _probe_once()
        mode = "enabled" if runtime.cache_enabled else "disabled"
        console.print(f"STAGEHAND_CACHE_MODE={mode}")
        console.print(f"CACHE_PROBE_CACHED_INPUT_RUN1={cached1}")
        console.print(f"CACHE_PROBE_CACHED_INPUT_RUN2={cached2}")
    except Exception:
        console.print("CACHE_PROBE=unavailable")


async def print_replay_metrics(runtime: StagehandRuntime) -> None:
    try:
        replay = await runtime.client.sessions.replay(runtime.raw_session.id)
    except Exception:
        console.print("STAGEHAND_REPLAY_METRICS=unavailable")
        return
    print_replay_summary(replay)
