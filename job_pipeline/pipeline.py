from __future__ import annotations

import json

from dotenv import load_dotenv
from rich.console import Console

from .airtable_mapper import dedupe_airtable_records, to_airtable_record
from .airtable_sync import write_airtable_records
from .boards import selected_boards
from .detail_extraction import extract_job_detail
from .models import PipelineRunResult
from .stagehand_session import (
    StagehandRuntime,
    env_flag,
    fetch_stagehand_metrics,
    print_metrics_summary,
    print_replay_metrics,
    run_cache_probe,
)
from .url_discovery import discover_job_urls


console = Console()


async def run_pipeline(site: str, dry_run: bool, write_airtable: bool) -> PipelineRunResult:
    load_dotenv()
    boards = selected_boards(site)
    if not boards:
        raise RuntimeError(f"No boards matched --site {site!r}.")

    runtime = await StagehandRuntime.create()
    discovery_results = []
    mapped_records: list[dict[str, str]] = []
    board_counts: list[tuple[str, int, int]] = []

    try:
        console.print(f"PIPELINE_MODE={'dry-run' if dry_run or not write_airtable else 'write-airtable'}")
        console.print(f"STAGEHAND_CACHE_ENABLED={str(runtime.cache_enabled).lower()}")
        if env_flag("STAGEHAND_CACHE_PROBE", "false"):
            await run_cache_probe(runtime)
        else:
            console.print("CACHE_PROBE=disabled")

        for board in boards:
            discovery = await discover_job_urls(runtime, board)
            discovery_results.append(discovery)
            board_record_start = len(mapped_records)

            console.print(
                f"DISCOVERY_RESULT site={board.name} urls={len(discovery.urls)} "
                f"selector={discovery.metadata.selector or '-'} "
                f"first_offer={discovery.metadata.first_offer_url or '-'}"
            )

            for index, url in enumerate(discovery.urls, start=1):
                console.print(f"DETAIL_PROGRESS site={board.name} index={index}/{len(discovery.urls)} url={url}")
                detail = await extract_job_detail(runtime, board.name, url)
                mapped_records.append(to_airtable_record(detail))

            extracted_count = len(mapped_records) - board_record_start
            board_counts.append((board.name, len(discovery.urls), extracted_count))
            console.print(
                f"BOARD_RESULT site={board.name} discovered={len(discovery.urls)} extracted={extracted_count}"
            )

        deduped_records = dedupe_airtable_records(mapped_records)
        console.print(f"RECORDS_TOTAL={len(mapped_records)}")
        console.print(f"RECORDS_DEDUPED={len(deduped_records)}")
        for site_name, discovered_count, extracted_count in board_counts:
            console.print(
                f"BOARD_SUMMARY site={site_name} discovered={discovered_count} extracted={extracted_count}"
            )

        for record in deduped_records:
            console.print(json.dumps(record, ensure_ascii=False, indent=2))

        if write_airtable:
            write_airtable_records(deduped_records)

        metrics = await fetch_stagehand_metrics(
            base_url=str(runtime.client.base_url),
            model_api_key=runtime.model_api_key,
            browserbase_api_key=runtime.browserbase_api_key,
            browserbase_project_id=runtime.browserbase_project_id,
        )
        print_metrics_summary(metrics)
        await print_replay_metrics(runtime)

        return PipelineRunResult(discovery_results=discovery_results, records=deduped_records)
    finally:
        await runtime.close()
