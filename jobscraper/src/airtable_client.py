from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from rich.console import Console
from rich.panel import Panel


console = Console()


@dataclass
class AirtableConfig:
    """Holds Airtable credentials sourced from the environment."""

    api_key: Optional[str]
    base_id: Optional[str]
    offers_table_id: Optional[str]
    sources_table_id: Optional[str]

    @classmethod
    def from_env(cls) -> "AirtableConfig":
        return cls(
            api_key=os.getenv("AIRTABLE_API_KEY"),
            base_id=os.getenv("AIRTABLE_BASE_ID"),
            offers_table_id=os.getenv("AIRTABLE_OFFERS_TABLE_ID"),
            sources_table_id=os.getenv("AIRTABLE_SOURCES_TABLE_ID"),
        )

    def is_configured(self) -> bool:
        return all([self.api_key, self.base_id, self.offers_table_id])


class AirtableClient:
    """Thin wrapper around the official Airtable SDK (pyairtable)."""

    def __init__(self, config: AirtableConfig):
        if not config.is_configured():
            raise ValueError("Airtable configuration is incomplete.")
        self.config = config
        self._table = None

    def _connect(self):
        if self._table is not None:
            return self._table

        try:
            from pyairtable import Table
        except ImportError as exc:
            raise RuntimeError(
                "pyairtable is not installed. Add it to your environment with\n"
                "  conda run -n <env> pip install pyairtable"
            ) from exc

        self._table = Table(
            self.config.api_key,
            self.config.base_id,
            self.config.offers_table_id,
        )
        return self._table

    def create_records(self, records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if not records:
            console.log("[yellow]No records to create in Airtable.")
            return []

        table = self._connect()

        payload = [self._normalize_record(record) for record in records]
        created = table.batch_create(payload)

        console.print(Panel(
            f"Created {len(created)} Airtable records.",
            title="Airtable",
            style="green",
        ))
        return created

    def get_all_records(self, table_id: str) -> List[Dict[str, Any]]:
        """Fetch all records from a specified table."""
        try:
            from pyairtable import Table
        except ImportError as exc:
            raise RuntimeError(
                "pyairtable is not installed. Add it to your environment with\n"
                "  conda run -n <env> pip install pyairtable"
            ) from exc

        table = Table(self.config.api_key, self.config.base_id, table_id)
        return table.all(sort=[{"field": "Job Boards", "direction": "desc"}])

    @staticmethod
    def _normalize_record(record: Dict[str, Any]) -> Dict[str, Any]:
        """Accept either raw field dicts or objects containing a `fields` key."""
        if "fields" in record and isinstance(record["fields"], dict):
            return record["fields"]
        return record
