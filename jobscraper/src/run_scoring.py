import argparse
import asyncio
import os
from typing import List, Optional

from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel

from airtable_client import AirtableClient, AirtableConfig
from config import DEFAULT_OPENAI_MODEL
from scoring import score_records


console = Console()


def _sanitize_env_var(name: str) -> None:
    value = os.getenv(name)
    if value is None:
        return
    os.environ[name] = value.strip()


def _load_env() -> None:
    load_dotenv(override=True)
    for name in [
        "OPENAI_API_KEY",
        "OPENAI_API_BASE",
        "OPENAI_API_TYPE",
        "OPENAI_HTTP_REFERER",
        "AIRTABLE_API_KEY",
        "AIRTABLE_BASE_ID",
        "AIRTABLE_OFFERS_TABLE_ID",
        "AIRTABLE_SOURCES_TABLE_ID",
    ]:
        _sanitize_env_var(name)


def _read_ids_file(path: str) -> List[str]:
    ids: List[str] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            value = line.strip()
            if value:
                ids.append(value)
    return ids


def _pick_unscored_ids(airtable_client: AirtableClient, limit: int) -> List[str]:
    table = airtable_client._connect()
    records = table.all(fields=["Score"])
    unscored = [record["id"] for record in records if "Score" not in record.get("fields", {})]
    return unscored[:limit]


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "record_ids",
        nargs="*",
        help="Airtable record IDs to score (e.g. recXXXXXXXX)",
    )
    parser.add_argument(
        "--ids-file",
        dest="ids_file",
        help="Path to a text file with Airtable record IDs (one per line)",
    )
    parser.add_argument(
        "--unscored",
        action="store_true",
        help="Score offers that currently have empty Score field",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Max number of records to score when using --unscored (default: 20)",
    )
    parser.add_argument(
        "--cv",
        default="cv.md",
        help="Path to candidate CV markdown (default: cv.md)",
    )
    parser.add_argument(
        "--preferences",
        default="preferences.json",
        help="Path to preferences JSON (default: preferences.json)",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_OPENAI_MODEL,
        help=f"LLM model to use (default: {DEFAULT_OPENAI_MODEL})",
    )

    args = parser.parse_args(argv)

    if not args.record_ids and not args.ids_file and not args.unscored:
        parser.error("Provide record IDs, --ids-file, or --unscored.")

    return args


async def _run() -> int:
    args = _parse_args()
    _load_env()

    api_key = os.getenv("OPENAI_API_KEY")
    api_base = os.getenv("OPENAI_API_BASE")
    if api_base:
        console.print(Panel(f"OPENAI_API_BASE={api_base}", title="Scoring", style="blue"))
    if api_key:
        key_hint = "sk-or-..." if api_key.startswith("sk-or-") else "sk-..."
        console.print(Panel(f"OPENAI_API_KEY detected ({key_hint})", title="Scoring", style="blue"))

    airtable_config = AirtableConfig.from_env()
    if not airtable_config.is_configured():
        console.print(Panel("Airtable is not configured (missing env vars).", title="Scoring", style="red"))
        return 2

    airtable_client = AirtableClient(airtable_config)

    record_ids: List[str] = []
    record_ids.extend(args.record_ids)

    if args.ids_file:
        record_ids.extend(_read_ids_file(args.ids_file))

    if args.unscored:
        record_ids.extend(_pick_unscored_ids(airtable_client, args.limit))

    record_ids = [rid for rid in record_ids if rid]
    seen = set()
    record_ids = [rid for rid in record_ids if not (rid in seen or seen.add(rid))]

    console.print(Panel(f"Scoring {len(record_ids)} record(s)...", title="Scoring", style="green"))

    await score_records(
        airtable_client=airtable_client,
        record_ids=record_ids,
        cv_path=args.cv,
        preferences_path=args.preferences,
        model=args.model,
    )

    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
