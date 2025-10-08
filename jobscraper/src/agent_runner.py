"""
Agent Execution Module

Handles agent creation, configuration, and execution:
- Creating agents with MCP servers
- Managing streamed execution
- Handling agent output and events
"""

import asyncio
import json
import os
import random
from collections import deque
from typing import Optional, List, Any, Deque
from rich.console import Console
from rich.panel import Panel
from rich.text import Text
from openai.types.shared import Reasoning, reasoning_effort

from config import (
    DEFAULT_OPENAI_MODEL,
    MCP_MAX_TURNS,
    RATE_LIMIT_MAX_RETRIES,
    RATE_LIMIT_BASE_DELAY_SECONDS,
    RATE_LIMIT_JITTER_SECONDS,
    RATE_LIMIT_REQUESTS_PER_MINUTE,
    RATE_LIMIT_WINDOW_SECONDS,
    VERBOSE_MCP_LOGGING,
    VERBOSE_AGENT_DECISIONS,
)
from prompts import NARRATIVE_INSTRUCTIONS

console = Console()


def _format_debug_data(data: Any) -> Optional[str]:
    """Best-effort pretty-print for tool arguments/results."""
    if data is None:
        return None
    try:
        return json.dumps(data, indent=2, ensure_ascii=False)
    except TypeError:
        if hasattr(data, "model_dump") and callable(getattr(data, "model_dump")):
            try:
                return json.dumps(data.model_dump(), indent=2, ensure_ascii=False)
            except Exception:
                pass
        if hasattr(data, "__dict__"):
            try:
                serializable = {
                    key: value
                    for key, value in data.__dict__.items()
                    if not callable(value) and not key.startswith("_")
                }
                return json.dumps(serializable, indent=2, ensure_ascii=False, default=str)
            except Exception:
                pass
        return repr(data)


def _extract_name(*candidates: Any, default: str = "unknown") -> str:
    for candidate in candidates:
        if not candidate:
            continue
        if isinstance(candidate, str):
            stripped = candidate.strip()
            if stripped:
                return stripped
        if hasattr(candidate, "name"):
            name = getattr(candidate, "name")
            if isinstance(name, str) and name.strip():
                return name.strip()
    return default


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
                    reasoning_effort="minimal",
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
        self.console.print(Panel(Text(input_text), title="User", style="magenta"))
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
        """Handle stream events with verbose logging for MCP activity."""
        try:
            event_type = getattr(event, "type", "unknown")

            if event_type == "run_item_stream_event":
                item = getattr(event, "item", None)
                item_type = getattr(item, "type", "unknown")

                if item_type == "message_output_item":
                    from agents import ItemHelpers

                    text = ItemHelpers.text_message_output(item)
                    if text and text != self._last_displayed_message:
                        self.console.print(Panel(Text(text), title="Agent", style="green"))
                        self._last_displayed_message = text

                    if not VERBOSE_AGENT_DECISIONS:
                        return

                if not VERBOSE_MCP_LOGGING:
                    return

                elif item_type == "tool_call_item":
                    tool_name = _extract_name(
                        getattr(item, "tool_name", None),
                        getattr(item, "tool", None),
                        getattr(item, "name", None),
                        default="unknown tool",
                    )
                    server_name = _extract_name(
                        getattr(item, "server_name", None),
                        getattr(item, "server", None),
                        getattr(getattr(item, "tool", None), "server_name", None),
                        default="unknown server",
                    )
                    arguments = getattr(item, "arguments", None)
                    formatted_args = _format_debug_data(arguments)

                    body_lines = [
                        f"🔧 Tool call: {tool_name}",
                        f"MCP Server: {server_name}",
                        "Arguments:",
                        formatted_args or "(none)",
                    ]
                    if tool_name == "unknown tool" or server_name == "unknown server":
                        raw_item = _format_debug_data(item)
                        if raw_item:
                            body_lines.extend(["Raw item:", raw_item])
                    self.console.print(Panel(Text("\n".join(body_lines)), title="MCP Tool Call", style="cyan"))

                elif item_type == "tool_result_item":
                    tool_name = _extract_name(
                        getattr(item, "tool_name", None),
                        getattr(item, "tool", None),
                        getattr(item, "name", None),
                        default="unknown tool",
                    )
                    server_name = _extract_name(
                        getattr(item, "server_name", None),
                        getattr(item, "server", None),
                        getattr(getattr(item, "tool", None), "server_name", None),
                        default="unknown server",
                    )
                    output = getattr(item, "output", None)
                    formatted_output = _format_debug_data(output)

                    body_lines = [
                        f"✅ Tool result: {tool_name}",
                        f"MCP Server: {server_name}",
                        "Output:",
                        formatted_output or "(empty)",
                    ]
                    if tool_name == "unknown tool" or server_name == "unknown server":
                        raw_item = _format_debug_data(item)
                        if raw_item:
                            body_lines.extend(["Raw item:", raw_item])
                    self.console.print(Panel(Text("\n".join(body_lines)), title="MCP Tool Result", style="green"))

                elif item_type == "tool_call_output_item":
                    output = getattr(item, "output", None)
                    formatted_output = _format_debug_data(output)
                    raw_item = _format_debug_data(item)
                    body_lines = [
                        "📤 Tool call output chunk",
                        "Output:",
                        formatted_output or "(empty)",
                    ]
                    if raw_item:
                        body_lines.extend(["Raw item:", raw_item])
                    self.console.print(Panel(Text("\n".join(body_lines)), title="MCP Tool Output", style="magenta"))

                elif item_type == "task_error_item":
                    error_message = getattr(item, "error_message", "Unknown error")
                    detail = _format_debug_data(getattr(item, "error", None))

                    body_lines = [f"Error: {error_message}"]
                    if detail:
                        body_lines.extend(["Details:", detail])

                    self.console.print(Panel(Text("\n".join(body_lines)), title="MCP Error", style="red"))

                elif item_type == "reasoning_item":
                    if not VERBOSE_AGENT_DECISIONS:
                        return
                    reasoning = _format_debug_data(getattr(item, "content", None))
                    raw_item = _format_debug_data(item)
                    body_lines = ["🧠 Reasoning", reasoning or "(empty)"]
                    if raw_item:
                        body_lines.extend(["Raw item:", raw_item])
                    self.console.print(Panel(Text("\n".join(body_lines)), title="Agent Reasoning", style="yellow"))

                else:
                    self.console.print(Panel(Text(f"Unhandled run item type: {item_type}"), title="MCP Event", style="magenta"))

            elif event_type == "agent_updated_stream_event":
                if not VERBOSE_MCP_LOGGING:
                    return
                new_agent = getattr(event, "new_agent", None)
                agent_name = _extract_name(new_agent, default="unknown agent")
                self.console.print(Panel(Text(f"Agent updated: {agent_name}"), title="Agent Update", style="yellow"))

            elif event_type == "run_step_created_event":
                if not VERBOSE_MCP_LOGGING:
                    return
                step = getattr(event, "step", None)
                step_type = getattr(step, "type", "unknown")
                self.console.print(Panel(Text(f"Step created: {step_type}"), title="Run Step", style="blue"))

        except Exception as exc:
            self.console.print(Panel(Text(f"Stream event handling error: {exc}"), title="Stream Error", style="red"))

    def display_final_result(self, result: Any) -> None:
        """Display final result"""
        final_output: Optional[str] = getattr(result, "final_output", None)
        if final_output and final_output != self._last_displayed_message:
            self.console.print(Panel(Text(final_output), title="Final Output", style="green"))
            self._last_displayed_message = final_output

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
