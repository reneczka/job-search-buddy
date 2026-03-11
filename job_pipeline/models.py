from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class BoardConfig:
    name: str
    url: str
    discovery_mode: str = "generic"


@dataclass
class DiscoveryMetadata:
    first_offer_url: str = ""
    selector: str = ""
    include_tokens: list[str] = field(default_factory=list)
    exclude_tokens: list[str] = field(default_factory=list)
    expected_count: Optional[int] = None
    total_steps: int = 0


@dataclass
class DiscoveryResult:
    board: BoardConfig
    urls: list[str]
    metadata: DiscoveryMetadata = field(default_factory=DiscoveryMetadata)


@dataclass
class JobDetail:
    source: str
    discovered_url: str
    final_url: str
    company: str = ""
    position: str = ""
    salary: str = ""
    location: str = ""
    notes: str = ""
    requirements: list[str] = field(default_factory=list)
    company_description: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class PipelineRunResult:
    discovery_results: list[DiscoveryResult]
    records: list[dict[str, str]]

