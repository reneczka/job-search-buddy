"""Environment Setup Module.

Handles environment validation and configuration:
- Loading environment variables
- Validating required configuration
- Setting up logging
- Checking dependencies
"""

import os
from typing import Optional

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel

from .config import (
    DEFAULT_OPENAI_TIMEOUT,
    DEFAULT_OPENAI_MAX_RETRIES,
)


console = Console()


class EnvironmentValidator:
    """Validates and sets up the environment for the application"""
    
    def __init__(self):
        self.openai_api_key: Optional[str] = None
        self.openai_api_base: Optional[str] = None
        self.openai_api_type: str = "chat_completions"
        self.openai_disable_tracing: bool = True
        self.openai_referer: Optional[str] = None
        self.airtable_api_key: Optional[str] = None
        self.airtable_base_id: Optional[str] = None
        self.airtable_offers_table_id: Optional[str] = None
        self.airtable_sources_table_id: Optional[str] = None
    
    def load_environment(self) -> None:
        """Load environment variables from .env file"""
        load_dotenv()
        
        # Load required variables
        self.openai_api_key = os.getenv("OPENAI_API_KEY")
        self.openai_api_base = os.getenv("OPENAI_API_BASE")
        self.openai_api_type = os.getenv("OPENAI_API_TYPE", "chat_completions")
        self.openai_disable_tracing = os.getenv("OPENAI_DISABLE_TRACING", "true").lower() in {"1", "true", "yes"}
        self.openai_referer = os.getenv("OPENAI_HTTP_REFERER")
        self.airtable_api_key = os.getenv("AIRTABLE_API_KEY")
        self.airtable_base_id = os.getenv("AIRTABLE_BASE_ID")
        self.airtable_offers_table_id = os.getenv("AIRTABLE_OFFERS_TABLE_ID")
        self.airtable_sources_table_id = os.getenv("AIRTABLE_SOURCES_TABLE_ID")
    
    def validate_openai_config(self) -> None:
        """Validate OpenAI configuration"""
        if not self.openai_api_key:
            raise RuntimeError(
                "Missing OPENAI_API_KEY. Set it in .env or environment."
            )
    
    def validate_airtable_config(self) -> bool:
        """
        Validate Airtable configuration
        
        Returns:
            True if Airtable is fully configured, False otherwise
        """
        missing_vars = []
        
        if not self.airtable_api_key:
            missing_vars.append("AIRTABLE_API_KEY")
        if not self.airtable_base_id:
            missing_vars.append("AIRTABLE_BASE_ID")
        if not self.airtable_offers_table_id:
            missing_vars.append("AIRTABLE_OFFERS_TABLE_ID")
        if not self.airtable_sources_table_id:
            missing_vars.append("AIRTABLE_SOURCES_TABLE_ID")
        
        if missing_vars:
            console.print(Panel(
                f"Airtable configuration incomplete. Missing: {', '.join(missing_vars)}\n"
                "Set these in your .env file to enable Airtable functionality.",
                title="Airtable Configuration",
                style="yellow",
            ))
            return False
        
        return True
    
    def setup_openai_client(self) -> None:
        """Configure OpenAI client with rate limiting and timeout settings"""
        try:
            import httpx
            from openai import AsyncOpenAI
            from agents import set_default_openai_client, set_default_openai_api, set_tracing_disabled
        except ImportError as e:
            console.print(Panel(
                f"Required packages not installed: {e}\n"
                "Install: pip install httpx openai",
                title="Import Error",
                style="red",
            ))
            return
        
        client_kwargs = {
            "api_key": self.openai_api_key,
            "timeout": DEFAULT_OPENAI_TIMEOUT,
            "max_retries": DEFAULT_OPENAI_MAX_RETRIES,
        }

        if self.openai_api_base:
            client_kwargs["base_url"] = self.openai_api_base

        headers = {}
        if self.openai_referer:
            headers["HTTP-Referer"] = self.openai_referer

        if headers:
            client_kwargs["default_headers"] = headers

        # Create custom client with timeout and retry settings
        custom_client = AsyncOpenAI(
            **client_kwargs,
            http_client=httpx.AsyncClient(
                timeout=httpx.Timeout(
                    connect=10.0,  # Connection timeout
                    read=DEFAULT_OPENAI_TIMEOUT,  # Match total timeout
                    write=10.0,    # Write timeout
                    pool=10.0      # Pool timeout
                )
            )
        )
        
        set_default_openai_client(custom_client)
        
        if self.openai_disable_tracing:
            try:
                set_tracing_disabled(True)
            except AttributeError:
                pass
        
        try:
            set_default_openai_api(self.openai_api_type)
        except AttributeError:
            try:
                from agents.models._openai_shared import set_use_responses_by_default
            except ImportError:
                set_use_responses_by_default = None
            if set_use_responses_by_default:
                set_use_responses_by_default(False)

        details = f"API type={self.openai_api_type}"
        if self.openai_api_base:
            details += f", base={self.openai_api_base}"
        console.print(Panel(
            f"OpenAI client configured ({details})",
            title="OpenAI Client",
            style="green",
        ))

    def check_agents_sdk(self) -> None:
        """Check if OpenAI Agents SDK is available"""
        try:
            from agents import Runner, ItemHelpers
            from agents.agent import Agent
        except ImportError as e:
            console.print(Panel(
                "OpenAI Agents SDK is not installed.\n\n"
                "Install from GitHub and retry:\n"
                "  pip install git+https://github.com/openai/openai-agents-python\n\n"
                f"Import error: {e}",
                title="Agents SDK Missing",
                style="red",
            ))
            raise RuntimeError("OpenAI Agents SDK not available")




def validate_and_setup_environment() -> EnvironmentValidator:
    """Setup and validate environment"""
    validator = EnvironmentValidator()
    validator.load_environment()
    validator.check_agents_sdk()
    validator.validate_openai_config()
    validator.validate_airtable_config()
    validator.setup_openai_client()
    return validator
