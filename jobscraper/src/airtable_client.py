from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, urlunparse

from rich.console import Console
from rich.panel import Panel


console = Console()


def normalize_url(url: str) -> str:
    """Normalize URL by removing query parameters and fragments.
    
    Examples:
        https://example.com/job?id=123#section -> https://example.com/job
        https://example.com/job -> https://example.com/job
    """
    if not url:
        return url
    
    parsed = urlparse(url)
    # Reconstruct URL without query params and fragments
    normalized = urlunparse((
        parsed.scheme,
        parsed.netloc,
        parsed.path,
        '',  # params (usually empty)
        '',  # query
        ''   # fragment
    ))
    return normalized


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

    def create_records(self, records: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Create records in Airtable, skipping duplicates based on Link field.
        
        Returns:
            Dict with 'created' (count), 'skipped' (count), and 'records' (list of created records)
        """
        if not records:
            console.log("[yellow]No records to create in Airtable.")
            return {"created": 0, "skipped": 0, "records": []}

        table = self._connect()

        # Fetch existing job links from Airtable
        console.print("[dim]Fetching existing job links to check for duplicates...[/]")
        existing_records = table.all(fields=["Link"])
        # Normalize existing links for comparison (strip query params)
        existing_links_normalized = {
            normalize_url(r["fields"].get("Link")) 
            for r in existing_records 
            if r["fields"].get("Link")
        }
        
        console.print(f"[dim]Found {len(existing_links_normalized)} existing jobs in Airtable[/]")

        # Filter out duplicates
        new_records = []
        skipped_count = 0
        
        for record in records:
            normalized_record = self._normalize_record(record)
            link = normalized_record.get("Link")
            
            # Compare normalized URLs (without query params)
            if link and normalize_url(link) in existing_links_normalized:
                skipped_count += 1
                console.print(f"[dim yellow]Skipping duplicate: {link}[/]")
            else:
                new_records.append(normalized_record)
        
        # Create only new records
        created = []
        if new_records:
            created = table.batch_create(new_records)
            console.print(Panel(
                f"Created {len(created)} new records, skipped {skipped_count} duplicates.",
                title="Airtable",
                style="green",
            ))
        else:
            console.print(Panel(
                f"No new records to create. Skipped {skipped_count} duplicates.",
                title="Airtable",
                style="yellow",
            ))
        
        return {
            "created": len(created),
            "skipped": skipped_count,
            "records": created
        }

    def get_all_records(self, table_id: str, sort_by: Optional[str] = None) -> List[Dict[str, Any]]:
        """Fetch all records from a specified table.
        
        Args:
            table_id: The Airtable table ID
            sort_by: Optional field name to sort by (prefix with '-' for descending order)
        """
        try:
            from pyairtable import Table
        except ImportError as exc:
            raise RuntimeError(
                "pyairtable is not installed. Add it to your environment with\n"
                "  conda run -n <env> pip install pyairtable"
            ) from exc

        table = Table(self.config.api_key, self.config.base_id, table_id)
        
        if sort_by:
            return table.all(sort=[sort_by])
        else:
            return table.all()

    def get_record(self, record_id: str, fields: Optional[List[str]] = None) -> Optional[Dict[str, Any]]:
        """Fetch a single record from the offers table by Airtable record ID."""
        table = self._connect()
        try:
            return table.get(record_id, fields=fields)
        except Exception as exc:  # noqa: BLE001 - pyairtable raises generic exceptions
            console.print(Panel(
                f"Failed to fetch Airtable record {record_id}: {exc}",
                title="Airtable",
                style="red",
            ))
            return None

    def batch_update_records(self, updates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Batch update multiple records in the offers table."""
        if not updates:
            return []

        table = self._connect()
        results: List[Dict[str, Any]] = []
        chunk_size = 10  # Airtable API limit per request

        for i in range(0, len(updates), chunk_size):
            chunk = updates[i : i + chunk_size]
            try:
                results.extend(table.batch_update(chunk))
            except Exception as exc:  # noqa: BLE001
                console.print(Panel(
                    f"Failed to update Airtable records chunk starting at index {i}: {exc}",
                    title="Airtable",
                    style="red",
                ))
        return results

    @staticmethod
    def _normalize_record(record: Dict[str, Any]) -> Dict[str, Any]:
        """Accept either raw field dicts or objects containing a `fields` key."""
        fields = record["fields"] if "fields" in record and isinstance(record["fields"], dict) else record

        if isinstance(fields, dict):
            notes = fields.get("Notes")
            if "Notes" in fields and (notes is None or not str(notes).strip()):
                fields["Notes"] = AirtableClient._synthesize_notes(fields)

        if isinstance(fields, dict) and "Requirements" in fields:
            requirements = fields.get("Requirements")

            if requirements is None:
                fields.pop("Requirements", None)
            elif isinstance(requirements, list):
                normalized = [str(item).strip() for item in requirements if item is not None and str(item).strip()]
                value = ", ".join(normalized).replace("\n", " ").strip()
                if value:
                    fields["Requirements"] = value
                else:
                    fields.pop("Requirements", None)
            elif isinstance(requirements, dict):
                try:
                    value = json.dumps(requirements, ensure_ascii=False)
                except TypeError:
                    value = str(requirements)
                value = value.replace("\n", " ").strip()
                if value:
                    fields["Requirements"] = value
                else:
                    fields.pop("Requirements", None)
            else:
                value = str(requirements).replace("\n", " ").strip()
                if value:
                    fields["Requirements"] = value
                else:
                    fields.pop("Requirements", None)

        return fields

    @staticmethod
    def _synthesize_notes(fields: Dict[str, Any]) -> str:
        parts: List[str] = []

        work_mode = str(fields.get("Local/Remote/Hybrid") or "").strip()
        if work_mode and work_mode != "N/A":
            parts.append(f"Work mode: {work_mode}")

        location = str(fields.get("Location") or "").strip()
        if location and location not in {"N/A", work_mode}:
            parts.append(f"Location: {location}")

        salary = str(fields.get("Salary") or "").strip()
        if salary and salary != "N/A":
            parts.append(f"Salary: {salary}")

        requirements = str(fields.get("Requirements") or "").strip()
        if requirements and requirements != "N/A":
            requirement_hint = AirtableClient._pick_note_requirement(requirements.splitlines())
            if requirement_hint:
                parts.append(f"Key requirement: {requirement_hint}")

        position = str(fields.get("Position") or "").strip()
        if not parts and position and position != "N/A":
            parts.append(f"Role: {position}")

        company = str(fields.get("Company") or "").strip()
        if not parts and company and company != "N/A":
            parts.append(f"Company: {company}")

        return "; ".join(parts[:4]).strip(" ;")

    @staticmethod
    def _pick_note_requirement(requirements: List[str]) -> str:
        candidates: List[str] = []
        for item in requirements:
            text = str(item).strip()
            if not text:
                continue
            chunks = re.split(r"(?:\n|(?<=\S)\s+-\s+)", text)
            for chunk in chunks:
                cleaned = chunk.removeprefix("- ").strip()
                if cleaned:
                    candidates.append(cleaned)
        if not candidates:
            return ""

        def score(value: str) -> tuple[int, int]:
            lowered = value.lower()
            technical = bool(re.search(
                r"\b(?:python|java(?:script)?|typescript|sql|html|css|aws|azure|gcp|docker|kubernetes|git|react|angular|django|flask|fastapi|node(?:\\.js)?|api|llm|rag|genai|tensorflow|pytorch|terraform|databricks|pyspark|go|golang|c\\+\\+|c#|php|bash|linux|excel|power bi)\b",
                lowered,
            ))
            concise = 20 <= len(value) <= 120
            return (2 if technical else 0) + (1 if concise else 0), -len(value)

        return max(candidates, key=score)
