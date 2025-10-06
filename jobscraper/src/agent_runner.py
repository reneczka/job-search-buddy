"""
Agent Execution Module

Handles agent creation, configuration, and execution:
- Creating agents with MCP servers
- Managing streamed execution
- Handling agent output and events
"""

import asyncio
import os
import random
from collections import deque
from typing import Optional, List, Any, Deque
from rich.console import Console
from rich.panel import Panel
from openai.types.shared import Reasoning, reasoning_effort

from config import (
    DEFAULT_OPENAI_MODEL,
    MCP_MAX_TURNS,
    RATE_LIMIT_MAX_RETRIES,
    RATE_LIMIT_BASE_DELAY_SECONDS,
    RATE_LIMIT_JITTER_SECONDS,
    RATE_LIMIT_REQUESTS_PER_MINUTE,
    RATE_LIMIT_WINDOW_SECONDS,
)
from prompts import NARRATIVE_INSTRUCTIONS

console = Console()


class AsyncRateLimiter:
    """Simple sliding-window async rate limiter."""

    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._timestamps: Deque[float] = deque()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        while True:
            async with self._lock:
                loop = asyncio.get_running_loop()
                now = loop.time()
                window_start = now - self.window_seconds

                while self._timestamps and self._timestamps[0] <= window_start:
                    self._timestamps.popleft()

                if len(self._timestamps) < self.max_requests:
                    self._timestamps.append(now)
                    return

                wait_time = self._timestamps[0] + self.window_seconds - now

            await asyncio.sleep(max(wait_time, 0.0))


_rate_limiter = AsyncRateLimiter(
    max_requests=RATE_LIMIT_REQUESTS_PER_MINUTE,
    window_seconds=RATE_LIMIT_WINDOW_SECONDS,
)


try:
    from openai import RateLimitError
except ImportError:  # pragma: no cover - fallback when openai missing during linting
    RateLimitError = Exception


class AgentRunner:
    """Manages agent creation and execution"""
    
    def __init__(self, model: str = DEFAULT_OPENAI_MODEL):
        self.model = model
        self.console = console
        self._last_displayed_message: Optional[str] = None
    
    def create_agent(self, name: str, instructions: str, mcp_servers: List[Any]) -> Any:
        """Create an agent with MCP servers"""
        try:
            from agents.agent import Agent
            from agents import ModelSettings
            from agents.extensions.models.litellm_model import LitellmModel
        except ImportError as e:
            raise RuntimeError(f"OpenAI Agents SDK not available: {e}")

        active_servers = [server for server in mcp_servers if server is not None]

        model_lower = self.model.lower()
        if "mistral" in model_lower or "google" in model_lower or "/" in self.model:
            model = LitellmModel(
                model=self.model,
                api_key=os.getenv("OPENAI_API_KEY"),
            )

            return Agent(
                name=name,
                instructions=instructions,
                model=model,
                model_settings=ModelSettings(
                    include_usage=True,
                    reasoning_effort="medium",
                    reasoning_verbosity="low",
                ),
                mcp_servers=active_servers,
            )

        return Agent(
            name=name,
            instructions=instructions,
            model=self.model,
            mcp_servers=active_servers,
        )
    
    async def run_agent_streamed(self, agent: Any, input_text: str, max_turns: int = MCP_MAX_TURNS) -> Any:
        """Execute agent with streaming output"""
        try:
            from agents import Runner, ItemHelpers
        except ImportError as e:
            raise RuntimeError(f"OpenAI Agents SDK not available: {e}")

        streamed = Runner.run_streamed(agent, input=input_text, max_turns=max_turns)
        self._last_displayed_message = None
        self.console.print(Panel(input_text, title="User", style="magenta"))
        async for event in streamed.stream_events():
            await self._handle_stream_event(event)

        return streamed
    
    async def run_agent_with_retry(self, agent: Any, input_text: str, max_turns: int = MCP_MAX_TURNS) -> Any:
        """Execute an agent with exponential backoff on rate limits"""
        last_error: Optional[Exception] = None
        for attempt in range(RATE_LIMIT_MAX_RETRIES + 1):
            await _rate_limiter.acquire()
            try:
                return await self.run_agent_streamed(agent, input_text, max_turns)
            except RateLimitError as exc:
                last_error = exc
                if attempt == RATE_LIMIT_MAX_RETRIES:
                    break

                delay = RATE_LIMIT_BASE_DELAY_SECONDS * (2 ** attempt) + random.uniform(0, RATE_LIMIT_JITTER_SECONDS)
                self.console.print(Panel(
                    f"OpenAI rate limit encountered. Retrying in {delay:.1f}s (attempt {attempt + 1}/{RATE_LIMIT_MAX_RETRIES + 1}).",
                    title="Rate Limit",
                    style="yellow",
                ))
                await asyncio.sleep(delay)
            except Exception:
                raise

        raise RuntimeError("OpenAI rate limit retries exhausted") from last_error

    async def _handle_stream_event(self, event: Any) -> None:
        """Handle stream events"""
        if event.type == "run_item_stream_event" and event.item.type == "message_output_item":
            try:
                from agents import ItemHelpers
                text = ItemHelpers.text_message_output(event.item)
                cleaned_text = self._sanitize_agent_output(text)
                if cleaned_text and cleaned_text != self._last_displayed_message:
                    self.console.print(Panel(cleaned_text, title="Agent", style="green"))
                    self._last_displayed_message = cleaned_text
            except Exception:
                pass

    @staticmethod
    def _sanitize_agent_output(text: str) -> str:
        """Remove verbose code snippets while keeping conversational content."""
        if not text:
            return ""

        def is_code_like(line: str) -> bool:
            stripped = line.strip()
            if not stripped:
                return False
            if stripped.startswith("│"):
                return True

            lower = stripped.lower()
            code_tokens = (
                "const ",
                "let ",
                "var ",
                "function",
                "class ",
                "return ",
                " document.",
                "queryselector",
                "if (",
                "else",
                "while (",
                "for (",
                "=>",
            )
            if any(token in lower for token in code_tokens):
                return True

            symbol_density = sum(ch in "{}();<>[]#=+-*/\\|" for ch in stripped) / max(len(stripped), 1)
            return symbol_density > 0.25

        sanitized_lines = []
        code_buffer = []
        in_code_fence = False

        def flush_code_buffer() -> None:
            if not code_buffer:
                return
            if len(code_buffer) > 1:
                sanitized_lines.append("[code snippet omitted]")
            else:
                sanitized_lines.extend(code_buffer)
            code_buffer.clear()

        for line in text.splitlines():
            stripped = line.strip()

            if stripped.startswith("```"):
                if in_code_fence:
                    flush_code_buffer()
                in_code_fence = not in_code_fence
                continue

            if in_code_fence:
                code_buffer.append(line)
                continue

            if is_code_like(line):
                code_buffer.append(line)
                continue

            flush_code_buffer()
            sanitized_lines.append(line)

        flush_code_buffer()

        return "\n".join(line for line in sanitized_lines if line.strip()).strip()

    
    
    def display_final_result(self, result: Any) -> None:
        """Display final result"""
        final_output: Optional[str] = getattr(result, "final_output", None)
        if final_output:
            cleaned_text = self._sanitize_agent_output(final_output)
            text_to_display = cleaned_text if cleaned_text else final_output
            if text_to_display and text_to_display != self._last_displayed_message:
                self.console.print(Panel(text_to_display, title="Final Output"))
                self._last_displayed_message = text_to_display


def create_playwright_agent(playwright_server: Any) -> Any:
    """Create agent configured with the Playwright MCP server."""
    runner = AgentRunner()
    servers = [playwright_server]

    return runner.create_agent(
        name="MCP Agent",
        instructions=NARRATIVE_INSTRUCTIONS,
        mcp_servers=servers,
    )


async def run_agent_with_task(agent: Any, task_prompt: str, max_turns: int = MCP_MAX_TURNS) -> Any:
    """Run agent with task"""
    runner = AgentRunner()
    result = await runner.run_agent_with_retry(agent, task_prompt, max_turns)
    runner.display_final_result(result)
    return result
