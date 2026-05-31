from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from dotenv import load_dotenv
from requests import HTTPError
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from jobscraper.src.airtable_client import AirtableClient, AirtableConfig

from .boards import supported_site_names
from .stagehand_session import StagehandRuntime, sleep_ms
from .url_discovery import accept_cookies, dismiss_pracuj_popups


console = Console()

APPLICATION_STATUS_NEW = "New"
APPLICATION_STATUS_READY = "Ready"
APPLICATION_STATUS_APPROVED = "Approved"
APPLICATION_STATUS_IN_PROGRESS = "In Progress"
APPLICATION_STATUS_NEEDS_REVIEW = "Needs Review"
APPLICATION_STATUS_APPLIED = "Applied"
APPLICATION_STATUS_FAILED = "Failed"
APPLICATION_STATUS_SKIPPED = "Skipped"

ALL_APPLICATION_STATUSES = {
    APPLICATION_STATUS_NEW,
    APPLICATION_STATUS_READY,
    APPLICATION_STATUS_APPROVED,
    APPLICATION_STATUS_IN_PROGRESS,
    APPLICATION_STATUS_NEEDS_REVIEW,
    APPLICATION_STATUS_APPLIED,
    APPLICATION_STATUS_FAILED,
    APPLICATION_STATUS_SKIPPED,
}
TOUCHED_APPLICATION_STATUSES = {
    APPLICATION_STATUS_IN_PROGRESS,
    APPLICATION_STATUS_NEEDS_REVIEW,
    APPLICATION_STATUS_APPLIED,
    APPLICATION_STATUS_FAILED,
    APPLICATION_STATUS_SKIPPED,
}
ELIGIBLE_RESET_STATUSES = {"", APPLICATION_STATUS_NEW, APPLICATION_STATUS_READY, APPLICATION_STATUS_APPROVED}
APPROVABLE_APPLICATION_STATUSES = {
    "",
    APPLICATION_STATUS_NEW,
    APPLICATION_STATUS_READY,
    APPLICATION_STATUS_APPROVED,
    APPLICATION_STATUS_IN_PROGRESS,
    APPLICATION_STATUS_NEEDS_REVIEW,
    APPLICATION_STATUS_FAILED,
    APPLICATION_STATUS_SKIPPED,
}
APPLY_SUPPORTED_SOURCES = set(supported_site_names())
DEFAULT_APPLY_THRESHOLD = 80
DEFAULT_APPLY_BATCH_SIZE = 3
DEFAULT_APPLY_SESSION_STATE_PATH = ".session_states/apply-session.json"
DEFAULT_APPLY_RUN_LOCK_PATH = ".session_states/apply-run.lock"
MAX_APPLY_FLOW_RECOVERY_ATTEMPTS = 3
AUTOMATION_PROCESS_MARKERS = (
    ("apply_run", "job_pipeline.apply_jobs"),
    ("stagehand_browser", "stagehand-v3"),
    ("playwright_browser", "ms-playwright"),
)
APPLICATION_FIELDS = [
    "Link",
    "Source",
    "Company",
    "Position",
    "Score",
    "ScoreReason",
    "MatchedSkills",
    "MissingSkills",
    "ApplicationStatus",
    "ApplicationNotes",
    "ApplicationUpdatedAt",
    "ApplicationAttemptedAt",
    "ApplicationApprovedAt",
    "ApplicationBatchTag",
    "Notes",
]
APPLICATION_FIELD_SPECS = {
    "ApplicationStatus": {"type": "singleLineText"},
    "ApplicationNotes": {"type": "multilineText"},
    "ApplicationUpdatedAt": {"type": "singleLineText"},
    "ApplicationAttemptedAt": {"type": "singleLineText"},
    "ApplicationApprovedAt": {"type": "singleLineText"},
    "ApplicationBatchTag": {"type": "singleLineText"},
}
PUBLISH_DATE_PATTERNS = (
    re.compile(r"(?i)\bpublished\s*:\s*(\d{2}\.\d{2}\.\d{4})"),
    re.compile(r"(?i)\bopublikowana?\s*:\s*(\d{2}\.\d{2}\.\d{4})"),
    re.compile(r"(?i)\bvalid for.*?(\d{2}\.\d{2}\.\d{4})"),
)
APPLY_TEXT_MARKERS = (
    "apply",
    "apply now",
    "apply here",
    "aplikuj",
    "zaaplikuj",
    "wyślij cv",
    "send cv",
    "easy apply",
    "apply on company site",
    "aplikuj teraz",
)
APPLY_EXCLUDE_MARKERS = (
    "save",
    "share",
    "udostępnij",
    "zapisz",
    "copy",
    "skopiuj",
    "report",
    "zgłoś",
)
COOKIE_REDIRECT_URL_MARKERS = (
    "cookie",
    "cookies",
    "consent",
    "onetrust",
    "privacy",
    "legal",
    "trust",
)
LOGIN_TEXT_MARKERS = (
    "log in",
    "login",
    "sign in",
    "zaloguj",
    "utwórz konto",
    "create account",
)
SUBMIT_TEXT_MARKERS = (
    "submit",
    "send application",
    "wyślij aplikację",
    "apply now",
    "finish application",
    "złóż aplikację",
)
APPLICATION_SUCCESS_TEXT_MARKERS = (
    "dziękujemy",
    "dziekujemy",
    "thank you",
    "podsumowanie aplikacji",
    "application sent",
    "aplikacja została wysłana",
)
APPLY_CLOSED_TEXT_MARKERS = (
    "zakończył zbieranie zgłoszeń",
    "zakonczył zbieranie zgłoszeń",
    "aktualne oferty pracodawcy",
    "no longer accepting applications",
    "applications are closed",
    "application period has ended",
)
CV_FIELD_MARKERS = ("cv", "resume", "résumé", "życiorys")
COVER_LETTER_MARKERS = (
    "cover letter",
    "motivation",
    "motywac",
    "why do you want",
    "why are you interested",
    "additional information",
)
FIELD_PATTERNS = {
    "first_name": ("first name", "given name", "imię", "imie", "firstname", "first_name"),
    "last_name": ("last name", "family name", "surname", "nazwisko", "lastname", "last_name"),
    "full_name": (
        "full name",
        "first and last name",
        "your name",
        "imię i nazwisko",
        "imie i nazwisko",
        "fullname",
        "full_name",
    ),
    "email": ("email", "e-mail", "mail"),
    "phone": ("phone", "telefon", "mobile"),
    "location_city": ("city", "location", "miasto", "miejscowość", "adres", "zlokalizuj", "dokładny adres"),
    "linkedin_url": ("linkedin",),
    "github_url": ("github",),
    "portfolio_url": ("portfolio", "website", "strona"),
}
COMMON_ANSWER_PATTERNS = {
    "work_authorization": ("authorized to work", "work authorization", "prawo do pracy", "permit to work"),
    "requires_sponsorship": ("sponsorship", "visa", "permit sponsorship"),
    "relocation": ("relocation", "relocate", "przeprowadzk"),
    "salary_expectation": ("salary expectation", "expected salary", "salary"),
    "notice_period": ("notice period", "availability"),
    "required_consent": ("terms of service", "privacy policy", "accept the terms", "i accept", "regulamin", "polityk"),
}


class CandidateApplicationProfileError(RuntimeError):
    """Raised when the structured candidate application profile is invalid."""


@dataclass
class CandidateApplicationProfile:
    full_name: str
    email: str
    phone: str
    location_city: str
    country: str
    linkedin_url: str
    github_url: str
    canonical_cv_path: Path
    portfolio_url: str = ""
    common_answers: dict[str, Any] = field(default_factory=dict)

    @property
    def first_name(self) -> str:
        return self.full_name.split()[0] if self.full_name.split() else self.full_name

    @property
    def last_name(self) -> str:
        parts = self.full_name.split()
        return parts[-1] if len(parts) > 1 else ""


@dataclass
class ApplicationAttemptResult:
    record_id: str
    status: str
    note: str
    selected_apply_url: str = ""
    login_handoff: bool = False
    cv_uploaded: bool = False
    draft_generated: bool = False
    final_submit_withheld: bool = True


def build_airtable_client() -> AirtableClient:
    load_dotenv()
    return AirtableClient(AirtableConfig.from_env())


def _airtable_api(client: AirtableClient) -> Any:
    from pyairtable import Api

    return Api(client.config.api_key)


def get_existing_offer_field_names(client: AirtableClient) -> set[str]:
    api = _airtable_api(client)
    base_id = client.config.base_id
    table_id = client.config.offers_table_id
    response = api.request("GET", f"https://api.airtable.com/v0/meta/bases/{base_id}/tables")
    table = next((item for item in response.get("tables", []) if item.get("id") == table_id), None)
    if not table:
        raise RuntimeError(f"Could not find Airtable offers table {table_id} in base {base_id}.")
    return {str(field.get("name") or "").strip() for field in table.get("fields", []) if field.get("name")}


def ensure_application_fields(client: AirtableClient, *, dry_run: bool = False) -> dict[str, Any]:
    existing = get_existing_offer_field_names(client)
    missing = [name for name in APPLICATION_FIELD_SPECS if name not in existing]
    if not missing:
        return {"created": [], "existing": sorted(existing)}

    if dry_run:
        return {"created": [], "missing": missing, "dry_run": True}

    api = _airtable_api(client)
    table_id = client.config.offers_table_id
    base_id = client.config.base_id
    created: list[str] = []
    for name in missing:
        payload = {"name": name, **APPLICATION_FIELD_SPECS[name]}
        try:
            api.request(
                "POST",
                f"https://api.airtable.com/v0/meta/bases/{base_id}/tables/{table_id}/fields",
                json=payload,
            )
            created.append(name)
        except HTTPError as exc:
            raise RuntimeError(
                f"Failed to create Airtable field '{name}'. "
                "Make sure your token has schema write access for this base."
            ) from exc

    if created:
        console.print(
            Panel(
                "Created Airtable application fields: " + ", ".join(created),
                title="Apply Schema",
                style="green",
            )
        )
    return {"created": created, "existing": sorted(existing | set(created))}


def load_candidate_application_profile(path: str, cv_override: str | None = None) -> CandidateApplicationProfile:
    profile_path = Path(path)
    if not profile_path.exists():
        raise CandidateApplicationProfileError(
            f"Candidate profile file not found: {profile_path}. "
            "Create a local candidate_profile.json based on candidate_profile.example.json."
        )

    try:
        import json

        raw = json.loads(profile_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise CandidateApplicationProfileError(f"Invalid candidate profile JSON: {exc}") from exc

    required_fields = [
        "full_name",
        "email",
        "phone",
        "location_city",
        "country",
        "linkedin_url",
        "github_url",
        "canonical_cv_path",
    ]
    missing = [field for field in required_fields if not str(raw.get(field) or "").strip()]
    if missing:
        raise CandidateApplicationProfileError(
            f"Candidate profile is missing required fields: {', '.join(missing)}"
        )

    cv_value = cv_override or str(raw.get("canonical_cv_path") or "").strip()
    cv_path = (profile_path.parent / cv_value).resolve() if not os.path.isabs(cv_value) else Path(cv_value)
    if not cv_path.exists():
        raise CandidateApplicationProfileError(f"Canonical CV file not found: {cv_path}")

    common_answers = raw.get("common_answers")
    if common_answers is None:
        common_answers = {}
    if not isinstance(common_answers, dict):
        raise CandidateApplicationProfileError("common_answers must be a JSON object.")

    return CandidateApplicationProfile(
        full_name=str(raw["full_name"]).strip(),
        email=str(raw["email"]).strip(),
        phone=str(raw["phone"]).strip(),
        location_city=str(raw["location_city"]).strip(),
        country=str(raw["country"]).strip(),
        linkedin_url=str(raw["linkedin_url"]).strip(),
        github_url=str(raw["github_url"]).strip(),
        portfolio_url=str(raw.get("portfolio_url") or "").strip(),
        canonical_cv_path=cv_path,
        common_answers=common_answers,
    )


def generate_batch_tag() -> str:
    return datetime.now(UTC).strftime("apply-%Y%m%d-%H%M%S")


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _acquire_apply_run_lock(lock_path: str = DEFAULT_APPLY_RUN_LOCK_PATH) -> Path:
    path = Path(lock_path).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    current_pid = os.getpid()

    if path.exists():
        raw = path.read_text(encoding="utf-8").strip()
        stale = True
        active_pid = 0
        if raw:
            try:
                active_pid = int(raw.splitlines()[0].strip())
                stale = not _pid_is_alive(active_pid)
            except ValueError:
                stale = True
        if stale:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        else:
            raise RuntimeError(
                f"Another apply run is already active (pid={active_pid}). "
                "Close or stop the existing apply session before starting a new one."
            )

    path.write_text(f"{current_pid}\n", encoding="utf-8")
    return path


def _release_apply_run_lock(lock_path: Path | None) -> None:
    if lock_path is None:
        return
    try:
        raw = lock_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return
    current_pid = str(os.getpid())
    if raw.splitlines()[:1] == [current_pid]:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def _list_automation_processes() -> list[dict[str, Any]]:
    try:
        result = subprocess.run(
            ["ps", "-Ao", "pid,command"],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return []

    current_pid = os.getpid()
    processes: list[dict[str, Any]] = []
    for raw_line in result.stdout.splitlines():
        line = raw_line.strip()
        if not line or line.lower().startswith("pid "):
            continue
        pid_text, _, command = line.partition(" ")
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        if pid == current_pid:
            continue
        for kind, marker in AUTOMATION_PROCESS_MARKERS:
            if marker in command:
                processes.append({"pid": pid, "kind": kind, "command": command.strip()})
                break
    return sorted(processes, key=lambda item: int(item["pid"]))


def _read_apply_run_lock_status(lock_path: str = DEFAULT_APPLY_RUN_LOCK_PATH) -> dict[str, Any]:
    path = Path(lock_path).expanduser().resolve()
    if not path.exists():
        return {"path": str(path), "exists": False, "pid": None, "alive": False, "stale": False}

    raw = path.read_text(encoding="utf-8").strip()
    pid: int | None = None
    if raw:
        try:
            pid = int(raw.splitlines()[0].strip())
        except ValueError:
            pid = None
    alive = _pid_is_alive(pid or 0) if pid is not None else False
    stale = bool(path.exists() and not alive)
    return {"path": str(path), "exists": True, "pid": pid, "alive": alive, "stale": stale}


def inspect_apply_environment(lock_path: str = DEFAULT_APPLY_RUN_LOCK_PATH) -> dict[str, Any]:
    processes = _list_automation_processes()
    lock = _read_apply_run_lock_status(lock_path)
    safe_to_start = not processes and not lock["exists"]
    return {"safe_to_start": safe_to_start, "processes": processes, "lock": lock}


def assert_apply_environment_ready(lock_path: str = DEFAULT_APPLY_RUN_LOCK_PATH) -> None:
    summary = inspect_apply_environment(lock_path)
    problems: list[str] = []
    if summary["processes"]:
        problems.append(
            "Found active automation processes: "
            + ", ".join(f"{item['kind']} pid={item['pid']}" for item in summary["processes"])
        )
    lock = summary["lock"]
    if lock["exists"]:
        if lock["stale"]:
            problems.append(f"Found stale apply lock at {lock['path']}. Run cleanup before starting a new apply run.")
        else:
            problems.append(f"Apply lock is still active at {lock['path']} (pid={lock['pid']}).")
    if problems:
        raise RuntimeError("Apply preflight failed. " + " ".join(problems))


def print_apply_preflight_summary(summary: dict[str, Any]) -> None:
    lock = summary["lock"]
    status = "SAFE_TO_START=yes" if summary["safe_to_start"] else "SAFE_TO_START=no"
    lines = [status]
    if lock["exists"]:
        lines.append(
            "lock="
            + (
                f"active pid={lock['pid']} path={lock['path']}"
                if lock["alive"]
                else f"stale path={lock['path']}"
            )
        )
    else:
        lines.append("lock=none")
    if summary["processes"]:
        lines.append(f"automation_processes={len(summary['processes'])}")
    else:
        lines.append("automation_processes=0")
    console.print(
        Panel(
            "\n".join(lines),
            title="Apply Preflight",
            style="green" if summary["safe_to_start"] else "yellow",
        )
    )
    if not summary["processes"]:
        return
    table = Table(title="Automation Processes")
    table.add_column("PID", style="cyan")
    table.add_column("Kind", style="magenta")
    table.add_column("Command", overflow="fold")
    for process in summary["processes"]:
        table.add_row(str(process["pid"]), str(process["kind"]), str(process["command"]))
    console.print(table)


def cleanup_apply_environment(lock_path: str = DEFAULT_APPLY_RUN_LOCK_PATH) -> dict[str, Any]:
    before = inspect_apply_environment(lock_path)
    terminated: list[int] = []
    killed: list[int] = []

    for process in before["processes"]:
        pid = int(process["pid"])
        try:
            os.kill(pid, signal.SIGTERM)
            terminated.append(pid)
        except ProcessLookupError:
            continue
        except PermissionError:
            continue

    if terminated:
        time.sleep(0.4)

    remaining = _list_automation_processes()
    for process in remaining:
        pid = int(process["pid"])
        try:
            os.kill(pid, signal.SIGKILL)
            killed.append(pid)
        except ProcessLookupError:
            continue
        except PermissionError:
            continue

    lock = _read_apply_run_lock_status(lock_path)
    removed_lock = False
    if lock["exists"] and lock["stale"]:
        try:
            Path(lock["path"]).unlink()
            removed_lock = True
        except FileNotFoundError:
            pass

    after = inspect_apply_environment(lock_path)
    return {
        "before": before,
        "after": after,
        "terminated_pids": terminated,
        "killed_pids": killed,
        "removed_lock": removed_lock,
    }


def print_apply_cleanup_summary(summary: dict[str, Any]) -> None:
    after = summary["after"]
    lines = [
        f"terminated_pids={summary['terminated_pids'] or '-'}",
        f"killed_pids={summary['killed_pids'] or '-'}",
        f"removed_stale_lock={summary['removed_lock']}",
        f"safe_to_start={after['safe_to_start']}",
    ]
    console.print(
        Panel(
            "\n".join(lines),
            title="Apply Cleanup",
            style="green" if after["safe_to_start"] else "yellow",
        )
    )
    if not after["safe_to_start"]:
        print_apply_preflight_summary(after)


def shortlist_jobs(
    client: AirtableClient,
    *,
    threshold: int,
    record_ids: list[str] | None = None,
    source: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    ensure_application_fields(client, dry_run=dry_run)
    records = _fetch_offer_records(client)
    updates = _prepare_shortlist_updates(records, threshold=threshold, record_ids=record_ids or [], source=source)
    if dry_run:
        _print_shortlist_preview(updates, threshold, source=source)
        return {"updated": 0, "candidates": len(updates), "dry_run": True}
    result = client.batch_update_records(updates)
    label = source or "all supported sources"
    console.print(Panel(f"Shortlisted {len(result)} job(s) as Ready for {label}.", title="Apply Shortlist", style="green"))
    return {"updated": len(result), "candidates": len(updates), "dry_run": False}


async def apply_to_jobs(
    client: AirtableClient,
    *,
    batch_size: int,
    record_ids: list[str] | None,
    candidate_profile: CandidateApplicationProfile | None,
    dry_run: bool = False,
    batch_tag: str | None = None,
    session_state_path: str = DEFAULT_APPLY_SESSION_STATE_PATH,
    source: str | None = None,
) -> dict[str, Any]:
    ensure_application_fields(client, dry_run=dry_run)
    records = _fetch_offer_records(client)
    selected = _select_apply_candidates(records, batch_size=batch_size, record_ids=record_ids or [], source=source)
    if dry_run:
        _print_apply_preview(selected, batch_size, source=source)
        return {"processed": 0, "candidates": len(selected), "dry_run": True}

    if not selected:
        console.print(Panel("No Approved jobs matched this apply run.", title="Apply", style="yellow"))
        return {"processed": 0, "candidates": 0, "dry_run": False}
    if candidate_profile is None:
        raise CandidateApplicationProfileError(
            "A candidate profile is required for apply mode. Create candidate_profile.json from "
            "candidate_profile.example.json or pass --candidate-profile."
        )

    assert_apply_environment_ready()
    run_lock_path = _acquire_apply_run_lock()
    runtime = await StagehandRuntime.create(session_state_path=session_state_path)
    processed = 0
    active_batch_tag = batch_tag or generate_batch_tag()
    try:
        for record in selected:
            prepared = _in_progress_update(record_id=record["id"], batch_tag=active_batch_tag)
            client.batch_update_records([prepared])
            result = await _assist_application_for_record(runtime, record, candidate_profile)
            client.batch_update_records([_result_to_update(result, batch_tag=active_batch_tag)])
            processed += 1
    finally:
        try:
            await runtime.close()
        finally:
            _release_apply_run_lock(run_lock_path)

    console.print(
        Panel(
            f"Processed {processed} approved job(s) in batch {active_batch_tag}.",
            title="Apply",
            style="green",
        )
    )
    return {"processed": processed, "candidates": len(selected), "dry_run": False, "batch_tag": active_batch_tag}


def _fetch_offer_records(client: AirtableClient) -> list[dict[str, Any]]:
    table = client._connect()
    existing_fields = get_existing_offer_field_names(client)
    requested_fields = [name for name in APPLICATION_FIELDS if name in existing_fields]
    return table.all(fields=requested_fields or None)


def _prepare_shortlist_updates(
    records: list[dict[str, Any]],
    *,
    threshold: int,
    record_ids: list[str],
    source: str | None,
) -> list[dict[str, Any]]:
    selected = _select_shortlist_candidates(records, threshold=threshold, record_ids=record_ids, source=source)
    updates: list[dict[str, Any]] = []
    now = _now_iso()
    for record in selected:
        score = _record_score(record)
        fields = {
            "ApplicationStatus": APPLICATION_STATUS_READY,
            "ApplicationNotes": f"Ready for review at score {score}.",
            "ApplicationUpdatedAt": now,
        }
        updates.append({"id": record["id"], "fields": fields})
    return updates


def _select_shortlist_candidates(
    records: list[dict[str, Any]],
    *,
    threshold: int,
    record_ids: list[str],
    source: str | None,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    record_id_filter = set(record_ids)
    normalized_source = (source or "").strip().lower()
    for record in records:
        if record_id_filter and record["id"] not in record_id_filter:
            continue
        fields = record.get("fields", {})
        record_source = str(fields.get("Source") or "").strip()
        if record_source not in APPLY_SUPPORTED_SOURCES:
            continue
        if normalized_source and record_source.lower() != normalized_source:
            continue
        if not str(fields.get("Link") or "").strip():
            continue
        score = _record_score(record)
        if score < threshold:
            continue
        status = _application_status(fields)
        if status not in {"", APPLICATION_STATUS_NEW, APPLICATION_STATUS_READY}:
            continue
        selected.append(record)
    return sorted(selected, key=_queue_sort_key, reverse=True)


def _select_apply_candidates(
    records: list[dict[str, Any]],
    *,
    batch_size: int,
    record_ids: list[str],
    source: str | None,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    record_id_filter = set(record_ids)
    normalized_source = (source or "").strip().lower()
    for record in records:
        if record_id_filter and record["id"] not in record_id_filter:
            continue
        fields = record.get("fields", {})
        record_source = str(fields.get("Source") or "").strip()
        if record_source not in APPLY_SUPPORTED_SOURCES:
            continue
        if normalized_source and record_source.lower() != normalized_source:
            continue
        if _application_status(fields) != APPLICATION_STATUS_APPROVED:
            continue
        if not str(fields.get("Link") or "").strip():
            continue
        selected.append(record)
    selected.sort(key=_queue_sort_key, reverse=True)
    return selected[:batch_size]


def inspect_jobs(
    client: AirtableClient,
    *,
    source: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    records = _fetch_offer_records(client)
    normalized_source = (source or "").strip().lower()
    filtered: list[dict[str, Any]] = []
    for record in records:
        fields = record.get("fields", {})
        record_source = str(fields.get("Source") or "").strip()
        if record_source not in APPLY_SUPPORTED_SOURCES:
            continue
        if normalized_source and record_source.lower() != normalized_source:
            continue
        if not str(fields.get("Link") or "").strip():
            continue
        filtered.append(record)

    if not filtered:
        console.print(
            Panel(
                f"No supported apply candidates found for source={source or 'all'}.",
                title="Apply Inspect",
                style="yellow",
            )
        )
        return {"count": 0, "source": source, "records": []}

    filtered.sort(key=_queue_sort_key, reverse=True)
    status_counts = Counter(_application_status(record.get("fields", {})) or "(empty)" for record in filtered)
    scored_count = sum(1 for record in filtered if str(record.get("fields", {}).get("Score") or "").strip())
    label = source or "all supported sources"
    summary_lines = [
        f"source={label}",
        f"records={len(filtered)}",
        f"scored={scored_count}",
        f"unscored={len(filtered) - scored_count}",
        "statuses=" + ", ".join(f"{name}:{count}" for name, count in sorted(status_counts.items())),
    ]
    console.print(Panel("\n".join(summary_lines), title="Apply Inspect", style="blue"))

    table = Table(title=f"Top {min(limit, len(filtered))} candidate(s)")
    table.add_column("ID", style="cyan")
    table.add_column("Score", justify="right")
    table.add_column("Status", style="magenta")
    table.add_column("Company")
    table.add_column("Position")
    for record in filtered[:limit]:
        fields = record.get("fields", {})
        table.add_row(
            str(record["id"]),
            str(fields.get("Score") or "-"),
            _application_status(fields) or "-",
            str(fields.get("Company") or "-"),
            str(fields.get("Position") or "-"),
        )
    console.print(table)
    return {"count": len(filtered), "source": source, "records": filtered[:limit]}


def approve_jobs(
    client: AirtableClient,
    *,
    record_ids: list[str],
    dry_run: bool = False,
) -> dict[str, Any]:
    if not record_ids:
        raise RuntimeError("approve mode requires at least one --record-ids value.")

    ensure_application_fields(client, dry_run=dry_run)
    records = _fetch_offer_records(client)
    record_id_filter = set(record_ids)
    updates: list[dict[str, Any]] = []
    for record in records:
        if record["id"] not in record_id_filter:
            continue
        fields = record.get("fields", {})
        source = str(fields.get("Source") or "").strip()
        if source not in APPLY_SUPPORTED_SOURCES:
            continue
        if not str(fields.get("Link") or "").strip():
            continue
        status = _application_status(fields)
        if status not in APPROVABLE_APPLICATION_STATUSES:
            continue
        now = _now_iso()
        updates.append(
            {
                "id": record["id"],
                "fields": {
                    "ApplicationStatus": APPLICATION_STATUS_APPROVED,
                    "ApplicationApprovedAt": now,
                    "ApplicationUpdatedAt": now,
                    "ApplicationNotes": "Approved for board-by-board apply test.",
                },
            }
        )

    if dry_run:
        console.print(
            Panel(
                f"Dry run: {len(updates)} job(s) would be marked Approved.",
                title="Apply Approve",
                style="blue",
            )
        )
        for update in updates[:20]:
            console.print(update)
        return {"updated": 0, "candidates": len(updates), "dry_run": True}

    result = client.batch_update_records(updates)
    console.print(Panel(f"Approved {len(result)} job(s) for apply testing.", title="Apply Approve", style="green"))
    return {"updated": len(result), "candidates": len(updates), "dry_run": False}


def _queue_sort_key(record: dict[str, Any]) -> tuple[float, float]:
    return (_record_score(record), _record_posting_timestamp(record))


def _record_score(record: dict[str, Any]) -> float:
    fields = record.get("fields", {})
    raw = str(fields.get("Score") or "").strip()
    try:
        return float(raw)
    except Exception:
        return 0.0


def _record_posting_timestamp(record: dict[str, Any]) -> float:
    fields = record.get("fields", {})
    notes = str(fields.get("Notes") or "")
    for pattern in PUBLISH_DATE_PATTERNS:
        match = pattern.search(notes)
        if not match:
            continue
        try:
            return datetime.strptime(match.group(1), "%d.%m.%Y").replace(tzinfo=UTC).timestamp()
        except ValueError:
            continue
    created = str(record.get("createdTime") or "").strip()
    if created:
        try:
            return datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return 0.0


def _application_status(fields: dict[str, Any]) -> str:
    status = str(fields.get("ApplicationStatus") or "").strip()
    return status if status in ALL_APPLICATION_STATUSES else status


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _in_progress_update(record_id: str, batch_tag: str) -> dict[str, Any]:
    now = _now_iso()
    return {
        "id": record_id,
        "fields": {
            "ApplicationStatus": APPLICATION_STATUS_IN_PROGRESS,
            "ApplicationUpdatedAt": now,
            "ApplicationAttemptedAt": now,
            "ApplicationApprovedAt": now,
            "ApplicationBatchTag": batch_tag,
            "ApplicationNotes": "Application assist started.",
        },
    }


def _result_to_update(result: ApplicationAttemptResult, *, batch_tag: str) -> dict[str, Any]:
    now = _now_iso()
    return {
        "id": result.record_id,
        "fields": {
            "ApplicationStatus": result.status,
            "ApplicationNotes": _truncate_text(result.note, 420),
            "ApplicationUpdatedAt": now,
            "ApplicationAttemptedAt": now,
            "ApplicationBatchTag": batch_tag,
        },
    }


async def _assist_application_for_record(
    runtime: StagehandRuntime,
    record: dict[str, Any],
    profile: CandidateApplicationProfile,
) -> ApplicationAttemptResult:
    fields = record.get("fields", {})
    record_id = record["id"]
    source = str(fields.get("Source") or "").strip()
    url = str(fields.get("Link") or "").strip()
    if source not in APPLY_SUPPORTED_SOURCES or not url:
        return ApplicationAttemptResult(
            record_id=record_id,
            status=APPLICATION_STATUS_FAILED,
            note="Unsupported source or missing job link.",
        )

    page = await runtime.ensure_page(force_new=False)
    await runtime.session.navigate(url=url, page=page)
    await accept_cookies(runtime, page)
    page = await _restore_offer_page_after_cookie_redirect(runtime, page, expected_url=url)
    if source == "pracuj":
        await dismiss_pracuj_popups(page)

    try:
        justjoin_login_handoff = False
        if source == "justjoin":
            page, justjoin_login_handoff, justjoin_login_still_required, justjoin_login_note = await _handle_justjoin_pre_login(
                runtime,
                page=page,
                job_url=url,
            )
            if justjoin_login_still_required:
                return ApplicationAttemptResult(
                    record_id=record_id,
                    status=APPLICATION_STATUS_NEEDS_REVIEW,
                    note=justjoin_login_note or "JustJoin login is still required before apply can continue safely.",
                    selected_apply_url=str(getattr(page, "url", "") or url),
                    login_handoff=justjoin_login_handoff,
                )
        apply_page, apply_url, apply_note = await _open_apply_flow(runtime, page)
        if apply_page is None:
            return ApplicationAttemptResult(
                record_id=record_id,
                status=APPLICATION_STATUS_FAILED,
                note=apply_note or "Could not find the main apply action on the job page.",
            )

        login_handoff, login_still_required = await _handle_login_handoff(apply_page)
        if login_still_required:
            return ApplicationAttemptResult(
                record_id=record_id,
                status=APPLICATION_STATUS_NEEDS_REVIEW,
                note="Login is still required before the application form can be filled safely.",
                selected_apply_url=apply_url,
                login_handoff=login_handoff,
            )
        recovery_notes: list[str] = []
        if source == "pracuj" and login_handoff:
            recovered_page, recovered_url, recovery_note = await _recover_apply_page_after_successful_login(
                runtime,
                page=apply_page,
                source=source,
                job_url=url,
            )
            if recovered_page is None:
                return ApplicationAttemptResult(
                    record_id=record_id,
                    status=APPLICATION_STATUS_NEEDS_REVIEW,
                    note=recovery_note or "Login completed, but I could not reopen the apply flow safely.",
                    selected_apply_url=apply_url,
                    login_handoff=login_handoff,
                )
            apply_page = recovered_page
            apply_url = recovered_url or apply_url
            apply_note = "; ".join(part for part in (apply_note, recovery_note) if part).strip(" ;")
        closed_apply_note = await _detect_closed_apply_state(apply_page)
        if closed_apply_note:
            return ApplicationAttemptResult(
                record_id=record_id,
                status=APPLICATION_STATUS_SKIPPED,
                note=closed_apply_note,
                selected_apply_url=apply_url,
                login_handoff=login_handoff,
            )
        field_snapshot = await _collect_form_fields(apply_page)
        if not field_snapshot:
            prelude_advanced, prelude_note = await _advance_apply_prelude(apply_page)
            if prelude_advanced:
                if prelude_note:
                    recovery_notes.append(prelude_note)
                field_snapshot = await _collect_form_fields(apply_page)
        completion_note = await _detect_application_completion_state(apply_page)
        if completion_note:
            return ApplicationAttemptResult(
                record_id=record_id,
                status=APPLICATION_STATUS_NEEDS_REVIEW,
                note="; ".join(part for part in (apply_note, *recovery_notes, completion_note) if part).strip(" ;"),
                selected_apply_url=str(getattr(apply_page, "url", "") or apply_url),
                login_handoff=login_handoff or justjoin_login_handoff,
                final_submit_withheld=True,
            )
        if source == "justjoin":
            (
                apply_page,
                apply_url,
                field_snapshot,
                justjoin_login_handoff,
                justjoin_login_still_required,
                justjoin_login_note,
            ) = await _handle_justjoin_login_handoff(
                runtime,
                page=apply_page,
                apply_url=apply_url,
                job_url=url,
                field_snapshot=field_snapshot,
            )
            if justjoin_login_still_required:
                return ApplicationAttemptResult(
                    record_id=record_id,
                    status=APPLICATION_STATUS_NEEDS_REVIEW,
                    note=justjoin_login_note or "JustJoin login is still required before apply can continue safely.",
                    selected_apply_url=apply_url,
                    login_handoff=True,
                )
        for _ in range(MAX_APPLY_FLOW_RECOVERY_ATTEMPTS):
            if not _looks_like_search_page(field_snapshot) and not _looks_like_company_page(
                field_snapshot,
                str(getattr(apply_page, "url", "") or ""),
            ):
                break
            recovered_page, recovered_url, recovery_note = await _recover_apply_page_after_login_redirect(
                runtime,
                page=apply_page,
                source=source,
                job_url=url,
            )
            if recovered_page is None:
                break
            apply_page = recovered_page
            apply_url = recovered_url or apply_url
            if recovery_note:
                recovery_notes.append(recovery_note)
            field_snapshot = await _collect_form_fields(apply_page)
        _print_field_debug(field_snapshot)
        if not field_snapshot:
            return ApplicationAttemptResult(
                record_id=record_id,
                status=APPLICATION_STATUS_NEEDS_REVIEW,
                note="Apply page opened, but no fillable fields were detected safely.",
                selected_apply_url=apply_url,
                login_handoff=login_handoff,
            )

        cover_letter_draft = _generate_cover_letter_draft(fields, profile)
        fill_result = await _fill_detected_fields(apply_page, field_snapshot, profile, cover_letter_draft)
        review_status, review_note = _post_fill_review(fields, fill_result)
        final_note = "; ".join(
            part for part in (apply_note, *recovery_notes, fill_result.note, review_note) if part
        ).strip(" ;")
        return ApplicationAttemptResult(
            record_id=record_id,
            status=review_status,
            note=final_note or "Application assist finished.",
            selected_apply_url=apply_url,
            login_handoff=login_handoff or justjoin_login_handoff,
            cv_uploaded=fill_result.cv_uploaded,
            draft_generated=fill_result.cover_letter_inserted,
            final_submit_withheld=True,
        )
    except Exception as exc:  # noqa: BLE001
        return ApplicationAttemptResult(
            record_id=record_id,
            status=APPLICATION_STATUS_FAILED,
            note=f"Application assist failed: {exc}",
        )


async def _restore_offer_page_after_cookie_redirect(runtime: StagehandRuntime, page: Any, *, expected_url: str) -> Any:
    current_url = str(getattr(page, "url", "") or "").strip()
    lowered = current_url.lower()
    if not current_url or current_url == expected_url:
        return page
    if not any(marker in lowered for marker in COOKIE_REDIRECT_URL_MARKERS):
        return page

    try:
        await runtime.session.navigate(url=expected_url, page=page)
        await sleep_ms(750)
        return page
    except Exception:
        return page


async def _recover_apply_page_after_login_redirect(
    runtime: StagehandRuntime,
    *,
    page: Any,
    source: str,
    job_url: str,
) -> tuple[Any | None, str, str]:
    try:
        await runtime.session.navigate(url=job_url, page=page)
        await accept_cookies(runtime, page)
        page = await _restore_offer_page_after_cookie_redirect(runtime, page, expected_url=job_url)
        if source == "pracuj":
            await dismiss_pracuj_popups(page)
        recovered_page, recovered_url, _ = await _open_apply_flow(runtime, page)
        if recovered_page is None:
            return None, "", ""
        return recovered_page, recovered_url, "Recovered apply flow after login redirect."
    except Exception:
        return None, "", ""


async def _recover_apply_page_after_successful_login(
    runtime: StagehandRuntime,
    *,
    page: Any,
    source: str,
    job_url: str,
) -> tuple[Any | None, str, str]:
    try:
        await runtime.save_storage_state()
        await sleep_ms(900)
        await runtime.session.navigate(url=job_url, page=page)
        await accept_cookies(runtime, page)
        page = await _restore_offer_page_after_cookie_redirect(runtime, page, expected_url=job_url)
        if source == "pracuj":
            await dismiss_pracuj_popups(page)
        reopened_page, reopened_url, reopened_note = await _open_apply_flow(runtime, page)
        if reopened_page is None:
            return None, "", reopened_note or "Could not reopen the apply flow after login."
        return reopened_page, reopened_url, "Reopened apply flow after successful login."
    except Exception as exc:  # noqa: BLE001
        return None, "", f"Apply recovery after login failed: {exc}"


async def _open_apply_flow(runtime: StagehandRuntime, page: Any) -> tuple[Any | None, str, str]:
    before_url = page.url
    before_pages = set(runtime.context.pages)
    _log_apply_event(
        "APPLY_OPEN_START",
        page_url=before_url,
        page_count=len(runtime.context.pages),
    )

    heuristic = await page.evaluate(
        """({ applyMarkers, excludeMarkers }) => {
            const normalize = (value) => (value || "").trim().toLowerCase().replace(/\\s+/g, " ");
            const visible = (el) => {
              const style = window.getComputedStyle(el);
              if (style.visibility === "hidden" || style.display === "none") return false;
              const rect = el.getBoundingClientRect();
              return rect.width > 0 && rect.height > 0;
            };
            const nodes = Array.from(document.querySelectorAll("a, button, [role='button']"));
            let best = null;
            let index = 0;
            for (const el of nodes) {
              if (!visible(el)) continue;
              const text = normalize(el.textContent || el.getAttribute("aria-label") || "");
              const href = el.tagName === "A" ? (el.getAttribute("href") || el.href || "") : "";
              const haystack = `${text} ${normalize(href)}`;
              if (!applyMarkers.some((marker) => haystack.includes(marker))) continue;
              if (excludeMarkers.some((marker) => haystack.includes(marker))) continue;
              el.setAttribute("data-jsb-apply-trigger-index", String(index));
              const rect = el.getBoundingClientRect();
              const score = (text.length ? 100 : 0) + (href ? 10 : 0);
              if (!best || score > best.score) {
                best = {
                  text,
                  href,
                  score,
                  selector: `[data-jsb-apply-trigger-index="${index}"]`,
                  tag: (el.tagName || "").toLowerCase(),
                  x: Math.round(rect.x),
                  y: Math.round(rect.y),
                  width: Math.round(rect.width),
                  height: Math.round(rect.height),
                };
              }
              index += 1;
            }
            if (!best) {
              return {
                found: false,
                href: "",
                label: "",
                selector: "",
                tag: "",
                x: 0,
                y: 0,
                width: 0,
                height: 0,
              };
            }
            return {
              found: true,
              href: best.href,
              label: best.text,
              selector: best.selector,
              tag: best.tag,
              x: best.x,
              y: best.y,
              width: best.width,
              height: best.height,
            };
        }""",
        {"applyMarkers": list(APPLY_TEXT_MARKERS), "excludeMarkers": list(APPLY_EXCLUDE_MARKERS)},
    )
    _log_apply_event(
        "APPLY_CTA_HEURISTIC_FOUND",
        found=str(bool((heuristic or {}).get("found"))).lower(),
        href=str((heuristic or {}).get("href") or ""),
        label=str((heuristic or {}).get("label") or ""),
        selector=str((heuristic or {}).get("selector") or ""),
        tag=str((heuristic or {}).get("tag") or ""),
        box=f"{(heuristic or {}).get('x', 0)},{(heuristic or {}).get('y', 0)},{(heuristic or {}).get('width', 0)},{(heuristic or {}).get('height', 0)}",
    )

    selector = str((heuristic or {}).get("selector") or "").strip()
    if selector:
        try:
            await page.locator(selector).first.click(timeout=3000)
            _log_apply_event("APPLY_CTA_HEURISTIC_CLICKED", selector=selector)
        except Exception as exc:
            _log_apply_event("APPLY_CTA_HEURISTIC_CLICK_FAILED", selector=selector, error=str(exc)[:240])

    result_page, result_url, result_note = await _wait_for_apply_surface(
        runtime,
        page,
        before_url,
        before_pages,
        source="heuristic",
        phase="heuristic_click",
    )
    if result_page is not None:
        _log_apply_event("APPLY_MODAL_DETECTED", source="heuristic", result_url=result_url, note=result_note)
        return result_page, result_url, result_note

    href = str((heuristic or {}).get("href") or "").strip()
    if href and page.url == before_url:
        target = urljoin(before_url, href)
        _log_apply_event("APPLY_CTA_DIRECT_NAVIGATE", target=target)
        await runtime.session.navigate(url=target, page=page)
        await accept_cookies(runtime, page)
        if href.startswith("#"):
            target_info = await _inspect_inpage_apply_target(page, href)
            _log_apply_event(
                "APPLY_INPAGE_TARGET",
                found=str(bool(target_info.get("found"))).lower(),
                closed=str(bool(target_info.get("closed"))).lower(),
                action_found=str(bool(target_info.get("action_found"))).lower(),
                action_href=str(target_info.get("action_href") or "")[:240],
                action_label=str(target_info.get("action_label") or "")[:120],
                input_count=str(target_info.get("input_count") or 0),
                text=str(target_info.get("text") or "")[:240],
            )
            if target_info.get("closed"):
                return page, page.url, "Apply panel is visible, but this offer is no longer accepting applications."
            if target_info.get("action_found"):
                nested_target = str(target_info.get("action_href") or "").strip()
                if nested_target and not nested_target.startswith("#"):
                    nested_url = urljoin(before_url, nested_target)
                    _log_apply_event("APPLY_INPAGE_NESTED_NAVIGATE", target=nested_url)
                    await runtime.session.navigate(url=nested_url, page=page)
                    await accept_cookies(runtime, page)
                else:
                    nested_selector = str(target_info.get("action_selector") or "").strip()
                    if nested_selector:
                        try:
                            await page.locator(nested_selector).first.click(timeout=5000)
                            _log_apply_event("APPLY_INPAGE_NESTED_CLICKED", selector=nested_selector)
                        except Exception as exc:
                            _log_apply_event("APPLY_INPAGE_NESTED_CLICK_FAILED", selector=nested_selector, error=str(exc)[:240])
                result_page, result_url, result_note = await _wait_for_apply_surface(
                    runtime,
                    page,
                    before_url,
                    before_pages,
                    source="inpage",
                    phase="inpage_apply",
                )
                if result_page is not None:
                    _log_apply_event("APPLY_MODAL_DETECTED", source="inpage", result_url=result_url, note=result_note)
                    return result_page, result_url, result_note
            if int(target_info.get("input_count") or 0) > 0:
                return page, page.url, "Apply panel opened on the same page."
        return page, page.url, "Navigated to detected apply URL."

    if not runtime.session.should_skip("observe"):
        try:
            observed = await runtime.session.observe(
                instruction=(
                    "Find the main apply action for this job offer. "
                    "Return only one safe action that starts the real application flow."
                ),
                options=runtime.model_opts,
                page=page,
            )
            actions = getattr(observed.data, "result", []) or []
            _log_apply_event(
                "APPLY_CTA_STAGEHAND_FOUND",
                action_count=len(actions),
                first_action=str(actions[0])[:240] if actions else "",
            )
            if actions:
                await runtime.session.act(input=actions[0], page=page)
                _log_apply_event("APPLY_CTA_STAGEHAND_CLICKED", action=str(actions[0])[:240])
                result_page, result_url, result_note = await _wait_for_apply_surface(
                    runtime,
                    page,
                    before_url,
                    before_pages,
                    source="stagehand",
                    phase="stagehand_click",
                )
                if result_page is not None:
                    _log_apply_event("APPLY_MODAL_DETECTED", source="stagehand", result_url=result_url, note=result_note)
                    return result_page, result_url, "Apply action opened via Stagehand."
        except Exception:
            pass

    _log_apply_event("APPLY_MODAL_NOT_DETECTED", page_url=page.url)
    return None, "", "No safe apply action was detected."


async def _select_latest_context_page(runtime: StagehandRuntime, before_pages: set[Any]) -> Any | None:
    pages = [candidate for candidate in runtime.context.pages if candidate not in before_pages]
    if pages:
        latest = pages[-1]
        runtime.page = latest
        return latest
    if runtime.context.pages:
        runtime.page = runtime.context.pages[-1]
    return None


def _stdin_is_interactive() -> bool:
    try:
        return sys.stdin.isatty()
    except Exception:
        return False


def _log_apply_event(event: str, **fields: Any) -> None:
    payload = " ".join(f"{key}={_log_apply_value(value)}" for key, value in fields.items())
    console.print(f"{event} {payload}".strip())


def _log_apply_value(value: Any) -> str:
    text = str(value).replace("\n", "\\n").strip()
    return text if text else "-"


async def _sample_apply_surface(page: Any) -> dict[str, Any]:
    try:
        snapshot = await page.evaluate(
            """() => {
                const modalRoot = () => {
                  const candidates = Array.from(document.querySelectorAll(
                    '[role="dialog"], [aria-modal="true"], dialog, [data-state="open"]'
                  ));
                  const visibleCandidate = candidates.find((el) => {
                    const style = window.getComputedStyle(el);
                    if (style.visibility === "hidden" || style.display === "none") return false;
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                  });
                  return visibleCandidate || document;
                };
                const visible = (el) => {
                  const style = window.getComputedStyle(el);
                  if (style.visibility === "hidden" || style.display === "none") return false;
                  const rect = el.getBoundingClientRect();
                  return rect.width > 0 && rect.height > 0;
                };
                const root = modalRoot();
                const nodes = Array.from(root.querySelectorAll('input, textarea, select'))
                  .filter((el) => visible(el))
                  .map((el) => {
                    const text = [
                      el.getAttribute('name') || '',
                      el.getAttribute('id') || '',
                      el.getAttribute('placeholder') || '',
                      el.getAttribute('aria-label') || '',
                      el.getAttribute('autocomplete') || '',
                      el.getAttribute('accept') || '',
                      el.closest('label')?.innerText || '',
                    ].join(' ').replace(/\\s+/g, ' ').trim();
                    return `${(el.tagName || '').toLowerCase()}:${(el.getAttribute('type') || '').toLowerCase()}:${text}`;
                  });
                return { fieldCount: nodes.length, samples: nodes.slice(0, 5) };
            }"""
        )
    except Exception:
        return {"fieldCount": 0, "samples": []}
    return snapshot if isinstance(snapshot, dict) else {"fieldCount": 0, "samples": []}


async def _wait_for_apply_surface(
    runtime: StagehandRuntime,
    page: Any,
    before_url: str,
    before_pages: set[Any],
    *,
    source: str,
    phase: str,
    attempts: int = 5,
    delay_ms: int = 600,
) -> tuple[Any | None, str, str]:
    for attempt in range(1, attempts + 1):
        if attempt > 1:
            await sleep_ms(delay_ms)
        candidate_page = await _select_latest_context_page(runtime, before_pages)
        active_page = candidate_page or page
        current_url = str(getattr(active_page, "url", "") or "")
        url_changed = current_url != before_url
        form_detected = await _page_has_apply_form(active_page)
        surface = await _sample_apply_surface(active_page)
        _log_apply_event(
            "APPLY_POST_CLICK_CHECK",
            source=source,
            phase=phase,
            attempt=attempt,
            url_changed=str(url_changed).lower(),
            new_page=str(candidate_page is not None).lower(),
            form_detected=str(form_detected).lower(),
            field_count=surface.get("fieldCount", 0),
            samples=" | ".join(surface.get("samples", [])),
            page_url=current_url or before_url,
        )
        if candidate_page is not None and current_url != before_url:
            await accept_cookies(runtime, candidate_page)
            return candidate_page, current_url, "Apply action opened."
        if form_detected:
            await accept_cookies(runtime, active_page)
            if active_page is page:
                return page, current_url or before_url, "Apply form opened on the same page."
            return active_page, current_url or before_url, "Apply form opened."
        if active_page is page and current_url != before_url:
            await accept_cookies(runtime, page)
            return page, current_url, "Apply action opened."
    return None, "", ""


async def _handle_login_handoff(page: Any) -> tuple[bool, bool]:
    if not await _page_requires_login(page):
        return False, False

    auto_login_note = ""
    page_url = str(getattr(page, "url", "") or "")
    if _is_pracuj_login_url(page_url):
        credentials = _pracuj_credentials_from_env()
        if credentials is not None:
            login_attempted = await _attempt_credentials_login(
                page,
                email=credentials[0],
                password=credentials[1],
                source="pracuj",
            )
            if login_attempted:
                await sleep_ms(1200)
                if not await _page_requires_login(page):
                    return True, False
                auto_login_note = "I reached the Pracuj login page and tried the credentials from env."

    console.print(
        Panel(
            (
                f"{auto_login_note or 'Login appears to be required.'} "
                "Complete the sign-in flow in the browser, then press Enter here to continue."
            ),
            title="Apply Login",
            style="yellow",
        )
    )
    if not _stdin_is_interactive():
        return True, True
    try:
        input()
    except EOFError:
        return True, True
    await sleep_ms(1000)
    return True, await _page_requires_login(page)


async def _handle_justjoin_pre_login(
    runtime: StagehandRuntime,
    *,
    page: Any,
    job_url: str,
) -> tuple[Any, bool, bool, str]:
    if await _justjoin_logged_in(page):
        return page, False, False, ""

    auto_sign_in_clicked, auto_sign_in_note = await _attempt_justjoin_sign_in_click(page)
    credentials_note = ""
    if auto_sign_in_clicked:
        try:
            await sleep_ms(1600)
            await _log_justjoin_sign_in_surface(page)
            await _advance_justjoin_sign_in_flow(page)
            credentials = _justjoin_credentials_from_env()
            if credentials is not None:
                login_attempted = await _attempt_credentials_login(
                    page,
                    email=credentials[0],
                    password=credentials[1],
                    source="justjoin",
                )
                if login_attempted:
                    credentials_note = "I clicked JustJoin sign-in and tried the credentials from env."
            await sleep_ms(1200)
            auto_handoff_url = str(getattr(page, "url", "") or "")
            await runtime.save_storage_state()
            await runtime.session.navigate(url=job_url, page=page)
            await accept_cookies(runtime, page)
            page = await _restore_offer_page_after_cookie_redirect(runtime, page, expected_url=job_url)
            auto_logged_in = await _justjoin_logged_in(page)
            _log_apply_event(
                "JUSTJOIN_PRELOGIN_AUTO_STATE",
                clicked="true",
                logged_in=str(auto_logged_in).lower(),
                handoff_url=auto_handoff_url or "-",
                page_url=str(getattr(page, "url", "") or ""),
            )
            if auto_logged_in:
                return page, True, False, "JustJoin login completed automatically before opening apply."
        except Exception as exc:  # noqa: BLE001
            _log_apply_event("JUSTJOIN_PRELOGIN_AUTO_ERROR", error=str(exc)[:240])

    console.print(
        Panel(
            (
                f"{credentials_note or auto_sign_in_note or 'JustJoin sign-in is visible on the offer page.'} Sign in in the browser now, "
                "then press Enter here so I can save the session before opening apply."
            ),
            title="JustJoin Login",
            style="yellow",
        )
    )
    if not _stdin_is_interactive():
        return page, True, True, "JustJoin login is required before apply can continue."
    try:
        input()
    except EOFError:
        return page, True, True, "JustJoin login is required before apply can continue."

    try:
        handoff_url = str(getattr(page, "url", "") or "")
        await runtime.save_storage_state()
        await sleep_ms(1000)
        await runtime.session.navigate(url=job_url, page=page)
        await accept_cookies(runtime, page)
        page = await _restore_offer_page_after_cookie_redirect(runtime, page, expected_url=job_url)
        logged_in = await _justjoin_logged_in(page)
        _log_apply_event(
            "JUSTJOIN_PRELOGIN_STATE",
            logged_in=str(logged_in).lower(),
            handoff_url=handoff_url or "-",
            page_url=str(getattr(page, "url", "") or ""),
        )
        if not logged_in:
            return page, True, True, "JustJoin login was not detected on the offer page after handoff."
        return page, True, False, "JustJoin login completed and session state saved before opening apply."
    except Exception as exc:  # noqa: BLE001
        return page, True, True, f"JustJoin pre-login handoff failed: {exc}"


async def _attempt_justjoin_sign_in_click(page: Any) -> tuple[bool, str]:
    clicked, details = await _click_visible_action_by_texts(page, ("sign in", "log in", "zaloguj"))
    if not clicked:
        return False, "JustJoin sign-in is visible on the offer page."

    _log_apply_event(
        "JUSTJOIN_PRELOGIN_AUTO_CLICKED",
        text=str(details.get("text") or "-")[:120],
        href=str(details.get("href") or "-")[:240],
        index=str(details.get("index") or "-"),
        page_url=str(getattr(page, "url", "") or ""),
    )
    return True, "I clicked the visible JustJoin sign-in action, but the session still needs confirmation."


async def _advance_justjoin_sign_in_flow(page: Any) -> None:
    steps = (
        ("sign in to candidate's profile", "candidate’s profile", "candidate profile"),
        ("sign in using email address", "use email address", "email address"),
    )
    for step_index, labels in enumerate(steps, start=1):
        clicked = False
        details: dict[str, Any] = {}
        for attempt in range(1, 4):
            clicked, details = await _click_visible_action_by_texts(page, labels)
            if clicked:
                _log_apply_event(
                    "JUSTJOIN_PRELOGIN_STEP",
                    step=str(step_index),
                    attempt=str(attempt),
                    clicked="true",
                    labels=" | ".join(labels),
                    text=str(details.get("text") or "-")[:120],
                    href=str(details.get("href") or "-")[:240],
                    index=str(details.get("index") or "-"),
                    page_url=str(getattr(page, "url", "") or ""),
                )
                await sleep_ms(1200)
                break
            if attempt < 3:
                await sleep_ms(700)
        if clicked:
            continue
        _log_apply_event(
            "JUSTJOIN_PRELOGIN_STEP",
            step=str(step_index),
            attempt="3",
            clicked="false",
            labels=" | ".join(labels),
            text=str(details.get("text") or "-")[:120],
            href=str(details.get("href") or "-")[:240],
            index=str(details.get("index") or "-"),
            page_url=str(getattr(page, "url", "") or ""),
        )


async def _log_justjoin_sign_in_surface(page: Any) -> None:
    try:
        context = page.context
        pages = context.pages
    except Exception:
        pages = [page]

    summaries: list[str] = []
    for idx, candidate in enumerate(pages[:5]):
        try:
            current_url = str(getattr(candidate, "url", "") or "")
        except Exception:
            current_url = ""
        try:
            title = str(await candidate.title())
        except Exception:
            title = ""
        summaries.append(f"{idx}:{title[:40] or '-'}::{current_url[:120] or '-'}")

    _log_apply_event(
        "JUSTJOIN_SIGNIN_SURFACE",
        page_count=str(len(pages)),
        pages=" || ".join(summaries)[:500] or "-",
        page_url=str(getattr(page, "url", "") or ""),
    )


async def _click_visible_action_by_texts(page: Any, labels: tuple[str, ...]) -> tuple[bool, dict[str, Any]]:
    normalized_labels = [label.strip().lower() for label in labels if label.strip()]
    locator = page.locator("a, button, [role='button'], [role='menuitem']")
    try:
        count = await locator.count()
    except Exception as exc:  # noqa: BLE001
        _log_apply_event("JUSTJOIN_ACTION_CLICK_FAILED", error=str(exc)[:240], stage="count")
        return False, {}

    candidates: list[dict[str, Any]] = []
    for index in range(min(count, 250)):
        candidate = locator.nth(index)
        try:
            if not await candidate.is_visible():
                continue
            text = str((await candidate.inner_text()) or "").strip()
            if not text:
                text = str((await candidate.get_attribute("aria-label")) or "").strip()
            normalized = text.lower()
            href = str((await candidate.get_attribute("href")) or "").strip()
            candidates.append(
                {
                    "locator": candidate,
                    "text": text,
                    "normalized": normalized,
                    "href": href,
                    "index": index,
                }
            )
        except Exception:
            continue

    for mode in ("exact", "contains"):
        for meta in candidates:
            normalized = str(meta["normalized"])
            if mode == "exact":
                if normalized not in normalized_labels:
                    continue
            else:
                if not any(label in normalized for label in normalized_labels):
                    continue
            try:
                locator_handle = meta["locator"]
                await locator_handle.scroll_into_view_if_needed(timeout=2000)
                try:
                    await locator_handle.click(timeout=5000)
                except Exception:
                    await locator_handle.click(timeout=5000, force=True)
            except Exception as exc:  # noqa: BLE001
                _log_apply_event(
                    "JUSTJOIN_ACTION_CLICK_FAILED",
                    error=str(exc)[:240],
                    index=str(meta["index"]),
                    labels=" | ".join(labels),
                    mode=mode,
                )
                return False, {}

            return True, {"text": meta["text"], "href": meta["href"], "index": meta["index"], "mode": mode}

    return False, {}


def _justjoin_credentials_from_env() -> tuple[str, str] | None:
    email = str(os.getenv("JUSTJOIN_EMAIL") or "").strip()
    password = str(os.getenv("JUSTJOIN_PASSWORD") or "").strip()
    if email and password:
        return email, password
    return None


def _pracuj_credentials_from_env() -> tuple[str, str] | None:
    email = str(os.getenv("PRACUJ_EMAIL") or "").strip()
    password = str(os.getenv("PRACUJ_PASSWORD") or "").strip()
    if email and password:
        return email, password
    return None


def _is_pracuj_login_url(url: str) -> bool:
    lowered = str(url or "").strip().lower()
    return lowered.startswith("https://login.pracuj.pl/") or "login.pracuj.pl" in lowered


async def _auth_surface_snapshot(page: Any) -> dict[str, Any]:
    try:
        snapshot = await page.evaluate(
            """() => {
                const visible = (el) => {
                  if (!el) return false;
                  const style = window.getComputedStyle(el);
                  if (style.visibility === "hidden" || style.display === "none") return false;
                  const rect = el.getBoundingClientRect();
                  return rect.width > 0 && rect.height > 0;
                };
                const inputs = Array.from(document.querySelectorAll('input')).filter((el) => visible(el));
                const classify = (el) => {
                  const type = (el.getAttribute('type') || '').toLowerCase();
                  const auto = (el.getAttribute('autocomplete') || '').toLowerCase();
                  const name = (el.getAttribute('name') || '').toLowerCase();
                  const id = (el.getAttribute('id') || '').toLowerCase();
                  const placeholder = (el.getAttribute('placeholder') || '').toLowerCase();
                  const label = (el.closest('label')?.innerText || '').toLowerCase();
                  const text = `${type} ${auto} ${name} ${id} ${placeholder} ${label}`;
                  if (type === 'password' || auto.includes('password')) return 'password';
                  if (
                    type === 'email' ||
                    auto.includes('email') ||
                    auto.includes('username') ||
                    text.includes('email') ||
                    text.includes('login') ||
                    text.includes('adres e-mail')
                  ) {
                    return 'email';
                  }
                  return 'other';
                };
                const kinds = inputs.map((el) => classify(el));
                return {
                  emailVisible: kinds.includes('email'),
                  passwordVisible: kinds.includes('password'),
                  inputCount: inputs.length,
                  title: (document.title || '').toLowerCase(),
                  url: (location.href || '').toLowerCase(),
                };
            }"""
        )
    except Exception:
        return {"emailVisible": False, "passwordVisible": False, "inputCount": 0, "title": "", "url": ""}
    return snapshot if isinstance(snapshot, dict) else {"emailVisible": False, "passwordVisible": False, "inputCount": 0, "title": "", "url": ""}


async def _find_visible_auth_control(page: Any, selectors: tuple[str, ...]) -> Any | None:
    for selector in selectors:
        try:
            locator = page.locator(selector)
            count = await locator.count()
        except Exception:
            continue
        for index in range(min(count, 8)):
            candidate = locator.nth(index)
            try:
                if await candidate.is_visible():
                    return candidate
            except Exception:
                continue
    return None


async def _find_visible_auth_button(page: Any) -> Any | None:
    locator = page.locator("button, [role='button'], input[type='submit']")
    try:
        count = await locator.count()
    except Exception:
        return None
    labels = ("dalej", "continue", "next", "sign in", "log in", "zaloguj")
    fallback = None
    for index in range(min(count, 20)):
        candidate = locator.nth(index)
        try:
            if not await candidate.is_visible():
                continue
            text = str((await candidate.inner_text()) or "").strip().lower()
            if not text:
                text = str((await candidate.get_attribute("value")) or (await candidate.get_attribute("aria-label")) or "").strip().lower()
            if fallback is None:
                fallback = candidate
            if text in labels or any(label in text for label in labels):
                return candidate
        except Exception:
            continue
    return fallback


async def _attempt_playwright_auth_step(page: Any, *, email: str, password: str) -> dict[str, Any]:
    email_input = await _find_visible_auth_control(
        page,
        (
            'input[type="email"]',
            'input[autocomplete*="email"]',
            'input[autocomplete*="username"]',
            'input[name*="email" i]',
            'input[id*="email" i]',
        ),
    )
    password_input = await _find_visible_auth_control(
        page,
        (
            'input[type="password"]',
            'input[autocomplete*="password"]',
            'input[name*="password" i]',
            'input[id*="password" i]',
        ),
    )

    if email_input is None and password_input is None:
        return {"attempted": False, "reason": "no_auth_inputs"}

    submit_button = await _find_visible_auth_button(page)

    if email_input is not None and password_input is None:
        await email_input.fill(email)
        if submit_button is not None:
            await submit_button.click(timeout=5000)
            return {"attempted": True, "reason": "email_only_step", "submitted": "continue"}
        await email_input.press("Enter")
        return {"attempted": True, "reason": "email_only_step", "submitted": "enter"}

    if email_input is None and password_input is not None:
        await password_input.fill(password)
        if submit_button is not None:
            await submit_button.click(timeout=5000)
            return {"attempted": True, "reason": "password_only_step", "submitted": "continue"}
        await password_input.press("Enter")
        return {"attempted": True, "reason": "password_only_step", "submitted": "enter"}

    await email_input.fill(email)
    await password_input.fill(password)
    if submit_button is not None:
        await submit_button.click(timeout=5000)
        return {"attempted": True, "reason": "email_and_password", "submitted": "continue"}
    await password_input.press("Enter")
    return {"attempted": True, "reason": "email_and_password", "submitted": "enter"}


async def _attempt_credentials_login(page: Any, *, email: str, password: str, source: str) -> bool:
    last_result: dict[str, Any] = {}
    for attempt in range(1, 6):
        surface_before = await _auth_surface_snapshot(page)
        try:
            result = await _attempt_playwright_auth_step(page, email=email, password=password)
        except Exception as exc:  # noqa: BLE001
            _log_apply_event("APPLY_CREDENTIAL_LOGIN_ERROR", source=source, error=str(exc)[:240], attempt=str(attempt))
            return False

        last_result = dict(result or {})
        attempted = bool(last_result.get("attempted"))
        _log_apply_event(
            "APPLY_CREDENTIAL_LOGIN_ATTEMPT",
            source=source,
            attempt=str(attempt),
            attempted=str(attempted).lower(),
            reason=str(last_result.get("reason") or "-")[:120],
            submitted=str(last_result.get("submitted") or "-")[:120],
            email_visible=str(bool(surface_before.get("emailVisible"))).lower(),
            password_visible=str(bool(surface_before.get("passwordVisible"))).lower(),
            page_url=str(getattr(page, "url", "") or ""),
        )

        reason = str(last_result.get("reason") or "")
        if attempted and reason == "email_only_step":
            password_appeared = False
            for wait_index in range(1, 7):
                await sleep_ms(900)
                surface_after = await _auth_surface_snapshot(page)
                _log_apply_event(
                    "APPLY_CREDENTIAL_LOGIN_WAIT",
                    source=source,
                    attempt=str(attempt),
                    wait_step=str(wait_index),
                    email_visible=str(bool(surface_after.get("emailVisible"))).lower(),
                    password_visible=str(bool(surface_after.get("passwordVisible"))).lower(),
                    input_count=str(surface_after.get("inputCount") or 0),
                    page_url=str(getattr(page, "url", "") or ""),
                )
                if bool(surface_after.get("passwordVisible")):
                    password_appeared = True
                    break
            if password_appeared:
                continue
        if attempted and reason not in {"no_auth_inputs", "email_only_step"}:
            return True
        if attempt < 5:
            await sleep_ms(700)

    return bool(last_result.get("attempted"))


async def _handle_justjoin_login_handoff(
    runtime: StagehandRuntime,
    *,
    page: Any,
    apply_url: str,
    job_url: str,
    field_snapshot: list[dict[str, Any]],
) -> tuple[Any, str, list[dict[str, Any]], bool, bool, str]:
    if not await _justjoin_guest_apply_detected(page, field_snapshot):
        return page, apply_url, field_snapshot, False, False, ""

    console.print(
        Panel(
            (
                "JustJoin appears to be using the guest account-creation flow. "
                "Sign in in the browser now, then press Enter here so I can save the session and reopen apply."
            ),
            title="JustJoin Login",
            style="yellow",
        )
    )
    if not _stdin_is_interactive():
        return page, apply_url, field_snapshot, True, True, "JustJoin login is required before apply can continue."
    try:
        input()
    except EOFError:
        return page, apply_url, field_snapshot, True, True, "JustJoin login is required before apply can continue."

    try:
        handoff_url = str(getattr(page, "url", "") or "")
        await runtime.save_storage_state()
        await sleep_ms(1000)
        await runtime.session.navigate(url=job_url, page=page)
        await accept_cookies(runtime, page)
        page = await _restore_offer_page_after_cookie_redirect(runtime, page, expected_url=job_url)
        logged_in = await _justjoin_logged_in(page)
        _log_apply_event(
            "JUSTJOIN_LOGIN_STATE",
            logged_in=str(logged_in).lower(),
            handoff_url=handoff_url or "-",
            page_url=str(getattr(page, "url", "") or ""),
        )
        if not logged_in:
            return (
                page,
                apply_url,
                field_snapshot,
                True,
                True,
                "JustJoin login was not detected after handoff on the reopened offer page.",
            )
        reopened_page, reopened_url, reopened_note = await _open_apply_flow(runtime, page)
        if reopened_page is None:
            return page, apply_url, field_snapshot, True, True, reopened_note or "Could not reopen JustJoin apply after login."
        reopened_fields = await _collect_form_fields(reopened_page)
        if await _justjoin_guest_apply_detected(reopened_page, reopened_fields):
            return (
                reopened_page,
                reopened_url or apply_url,
                reopened_fields,
                True,
                True,
                "JustJoin login completed, but the reopened apply flow still looks like guest account creation.",
            )
        return (
            reopened_page,
            reopened_url or apply_url,
            reopened_fields,
            True,
            False,
            "JustJoin login completed and session state saved.",
        )
    except Exception as exc:  # noqa: BLE001
        return page, apply_url, field_snapshot, True, True, f"JustJoin login handoff failed: {exc}"


async def _justjoin_guest_apply_detected(page: Any, field_snapshot: list[dict[str, Any]]) -> bool:
    labels = " ".join(str(item.get("label") or "").lower() for item in field_snapshot)
    names = {str(item.get("name") or "").strip().lower() for item in field_snapshot}
    if "create_account_accepted" in names:
        return True
    if "creating an account" in labels:
        return True
    return await _page_has_visible_sign_in(page)


async def _justjoin_logged_in(page: Any) -> bool:
    return not await _page_has_visible_sign_in(page)


async def _page_has_visible_sign_in(page: Any) -> bool:
    try:
        return bool(
            await page.evaluate(
                """() => {
                    const visible = (el) => {
                      const style = window.getComputedStyle(el);
                      if (style.visibility === "hidden" || style.display === "none") return false;
                      const rect = el.getBoundingClientRect();
                      return rect.width > 0 && rect.height > 0;
                    };
                    const nodes = Array.from(document.querySelectorAll('a, button, [role="button"]'));
                    return nodes.some((el) => {
                      if (!visible(el)) return false;
                      const text = ((el.textContent || el.getAttribute('aria-label') || '')).trim().toLowerCase();
                      return text === 'sign in' || text === 'log in' || text === 'zaloguj';
                    });
                }"""
            )
        )
    except Exception:
        return False


async def _page_requires_login(page: Any) -> bool:
    try:
        snapshot = await page.evaluate(
            """(markers) => {
                const modalRoot = () => {
                  const candidates = Array.from(document.querySelectorAll(
                    '[role="dialog"], [aria-modal="true"], dialog, [data-state="open"]'
                  ));
                  const visibleCandidate = candidates.find((el) => {
                    const style = window.getComputedStyle(el);
                    if (style.visibility === "hidden" || style.display === "none") return false;
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                  });
                  return visibleCandidate || document;
                };
                const bodyText = (document.body?.innerText || "").toLowerCase();
                const hasPassword = !!document.querySelector('input[type="password"]');
                const markerHit = markers.some((marker) => bodyText.includes(marker));
                const visible = (el) => {
                  const style = window.getComputedStyle(el);
                  if (style.visibility === "hidden" || style.display === "none") return false;
                  const rect = el.getBoundingClientRect();
                  return rect.width > 0 && rect.height > 0;
                };
                const root = modalRoot();
                const fields = Array.from(root.querySelectorAll('input, textarea, select'))
                  .filter((el) => visible(el))
                  .map((el) => {
                    const parts = [
                      el.getAttribute('name') || '',
                      el.getAttribute('id') || '',
                      el.getAttribute('placeholder') || '',
                      el.getAttribute('aria-label') || '',
                      el.getAttribute('autocomplete') || '',
                      el.getAttribute('accept') || '',
                      el.closest('label')?.innerText || '',
                    ];
                    return `${(el.tagName || '').toLowerCase()} ${(el.getAttribute('type') || '').toLowerCase()} ${parts.join(' ')}`.toLowerCase();
                  });
                const authFieldCount = fields.filter((value) =>
                  value.includes('email') ||
                  value.includes('username') ||
                  value.includes('password') ||
                  value.includes('login') ||
                  value.includes('adres e-mail')
                ).length;
                const applyFieldCount = fields.filter((value) =>
                  value.includes('email') ||
                  value.includes('first and last name') ||
                  value.includes('full name') ||
                  value.includes('resume') ||
                  value.includes('cv') ||
                  value.includes('document') ||
                  value.includes('upload')
                ).length;
                return {
                  hasPassword,
                  markerHit,
                  title: (document.title || "").toLowerCase(),
                  url: (location.href || "").toLowerCase(),
                  authFieldCount,
                  applyFieldCount,
                };
            }""",
            list(LOGIN_TEXT_MARKERS),
        )
    except Exception:
        return False

    if bool(snapshot.get("hasPassword")):
        return True

    page_url = str(snapshot.get("url") or "")
    title = str(snapshot.get("title") or "")
    auth_field_count = int(snapshot.get("authFieldCount") or 0)
    if "login." in page_url or "/login" in page_url:
        return True
    if auth_field_count >= 1 and (bool(snapshot.get("markerHit")) or "login" in title or "logowanie" in title):
        return True

    if int(snapshot.get("applyFieldCount") or 0) >= 2:
        return False

    return bool(snapshot.get("markerHit")) or any(
        marker in title for marker in LOGIN_TEXT_MARKERS
    )


async def _advance_apply_prelude(page: Any) -> tuple[bool, str]:
    clicked, details = await _click_visible_action_by_texts(
        page,
        (
            "kontynuuj aplikowanie",
            "continue application",
            "continue applying",
        ),
    )
    if not clicked:
        return False, ""
    _log_apply_event(
        "APPLY_PRELUDE_CLICKED",
        text=str(details.get("text") or "-")[:120],
        href=str(details.get("href") or "-")[:240],
        index=str(details.get("index") or "-"),
        page_url=str(getattr(page, "url", "") or ""),
    )
    await sleep_ms(1200)
    return True, "Advanced the application prelude screen."


async def _detect_application_completion_state(page: Any) -> str:
    try:
        snapshot = await page.evaluate(
            """(markers) => {
                const text = ((document.body?.innerText || '') + ' ' + (document.title || '')).toLowerCase();
                const marker = markers.find((item) => text.includes(item));
                return {
                  marker: marker || '',
                  url: (location.href || '').toLowerCase(),
                  title: (document.title || '').toLowerCase(),
                };
            }""",
            list(APPLICATION_SUCCESS_TEXT_MARKERS),
        )
    except Exception:
        return ""
    if not isinstance(snapshot, dict):
        return ""
    page_url = str(snapshot.get("url") or "")
    marker = str(snapshot.get("marker") or "").strip()
    if "dziekujemy.pracuj.pl" in page_url:
        return "Application confirmation page detected after opening the apply flow."
    if marker:
        return f"Application confirmation state detected ({marker})."
    return ""


async def _page_has_apply_form(page: Any) -> bool:
    try:
        snapshot = await page.evaluate(
            """() => {
                const modalRoot = () => {
                  const candidates = Array.from(document.querySelectorAll(
                    '[role="dialog"], [aria-modal="true"], dialog, [data-state="open"]'
                  ));
                  const visibleCandidate = candidates.find((el) => {
                    const style = window.getComputedStyle(el);
                    if (style.visibility === "hidden" || style.display === "none") return false;
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                  });
                  return visibleCandidate || document;
                };
                const visible = (el) => {
                  const style = window.getComputedStyle(el);
                  if (style.visibility === "hidden" || style.display === "none") return false;
                  const rect = el.getBoundingClientRect();
                  return rect.width > 0 && rect.height > 0;
                };
                const root = modalRoot();
                const nodes = Array.from(root.querySelectorAll('input, textarea, select'))
                  .filter((el) => visible(el))
                  .map((el) => {
                    const parts = [
                      el.getAttribute('name') || '',
                      el.getAttribute('id') || '',
                      el.getAttribute('placeholder') || '',
                      el.getAttribute('aria-label') || '',
                      el.getAttribute('autocomplete') || '',
                      el.getAttribute('accept') || '',
                      el.closest('label')?.innerText || '',
                    ];
                    return `${(el.tagName || '').toLowerCase()} ${(el.getAttribute('type') || '').toLowerCase()} ${parts.join(' ')}`.toLowerCase();
                  });
                const applyFieldCount = nodes.filter((value) =>
                  value.includes('email') ||
                  value.includes('first and last name') ||
                  value.includes('full name') ||
                  value.includes('resume') ||
                  value.includes('cv') ||
                  value.includes('document') ||
                  value.includes('upload')
                ).length;
                return { applyFieldCount };
            }"""
        )
    except Exception:
        return False
    return int(snapshot.get("applyFieldCount") or 0) >= 2


async def _inspect_inpage_apply_target(page: Any, href: str) -> dict[str, Any]:
    target_id = href[1:] if href.startswith("#") else href
    if not target_id:
        return {"found": False}
    try:
        snapshot = await page.evaluate(
            """({ targetId, applyMarkers, excludeMarkers, closedMarkers }) => {
                const normalize = (value) => (value || "").toLowerCase().replace(/\\s+/g, " ").trim();
                const visible = (el) => {
                  if (!el) return false;
                  const style = window.getComputedStyle(el);
                  if (style.visibility === "hidden" || style.display === "none") return false;
                  const rect = el.getBoundingClientRect();
                  return rect.width > 0 && rect.height > 0;
                };
                const root = document.getElementById(targetId);
                if (!root) return { found: false };
                const text = normalize(root.innerText || "");
                const actions = Array.from(root.querySelectorAll('a, button, [role="button"]'));
                let action = null;
                let actionIndex = 0;
                for (const el of actions) {
                  if (!visible(el)) continue;
                  const label = normalize(el.textContent || el.getAttribute('aria-label') || '');
                  const href = el.tagName === 'A' ? (el.getAttribute('href') || el.href || '') : '';
                  const haystack = `${label} ${normalize(href)}`;
                  if (!applyMarkers.some((marker) => haystack.includes(marker))) continue;
                  if (excludeMarkers.some((marker) => haystack.includes(marker))) continue;
                  el.setAttribute('data-jsb-inpage-apply-index', String(actionIndex));
                  action = {
                    label,
                    href,
                    selector: `[data-jsb-inpage-apply-index="${actionIndex}"]`,
                  };
                  break;
                }
                return {
                  found: true,
                  text,
                  closed: closedMarkers.some((marker) => text.includes(marker)),
                  action_found: !!action,
                  action_label: action?.label || '',
                  action_href: action?.href || '',
                  action_selector: action?.selector || '',
                  input_count: root.querySelectorAll('input, textarea, select').length,
                };
            }""",
            {
                "targetId": target_id,
                "applyMarkers": list(APPLY_TEXT_MARKERS),
                "excludeMarkers": list(APPLY_EXCLUDE_MARKERS),
                "closedMarkers": list(APPLY_CLOSED_TEXT_MARKERS),
            },
        )
    except Exception:
        return {"found": False}
    return snapshot if isinstance(snapshot, dict) else {"found": False}


async def _detect_closed_apply_state(page: Any) -> str:
    try:
        snapshot = await page.evaluate(
            """(closedMarkers) => {
                const normalize = (value) => (value || "").toLowerCase().replace(/\\s+/g, " ").trim();
                const roots = [
                  document.querySelector('#offer-apply-panel'),
                  ...Array.from(document.querySelectorAll('[role="dialog"], [aria-modal="true"], dialog, [data-state="open"]')),
                  document.body,
                ].filter(Boolean);
                for (const root of roots) {
                  const text = normalize(root.innerText || '');
                  const marker = closedMarkers.find((item) => text.includes(item));
                  if (marker) {
                    return { closed: true, marker, text: text.slice(0, 240) };
                  }
                }
                return { closed: false };
            }""",
            list(APPLY_CLOSED_TEXT_MARKERS),
        )
    except Exception:
        return ""
    if not isinstance(snapshot, dict) or not snapshot.get("closed"):
        return ""
    marker = str(snapshot.get("marker") or "").strip()
    if marker:
        return f"This offer is no longer accepting applications ({marker})."
    return "This offer is no longer accepting applications."


@dataclass
class FillResult:
    filled_fields: list[str] = field(default_factory=list)
    prefilled_fields: list[str] = field(default_factory=list)
    semantic_answers: list[str] = field(default_factory=list)
    cv_uploaded: bool = False
    cover_letter_inserted: bool = False
    skipped_fields: list[str] = field(default_factory=list)

    @property
    def note(self) -> str:
        parts: list[str] = []
        if self.filled_fields:
            parts.append(f"Filled: {', '.join(self.filled_fields[:6])}")
        if self.prefilled_fields:
            parts.append(f"Prefilled: {', '.join(self.prefilled_fields[:6])}")
        if self.semantic_answers:
            parts.append(f"Answered: {', '.join(self.semantic_answers[:6])}")
        if self.cv_uploaded:
            parts.append("CV uploaded")
        if self.cover_letter_inserted:
            parts.append("cover-letter draft inserted")
        if self.skipped_fields:
            parts.append(f"Skipped ambiguous: {', '.join(self.skipped_fields[:4])}")
        return "; ".join(parts)


async def _collect_form_fields(page: Any) -> list[dict[str, Any]]:
    try:
        payload = await page.evaluate(
            """() => {
                const modalRoot = () => {
                  const candidates = Array.from(document.querySelectorAll(
                    '[role="dialog"], [aria-modal="true"], dialog, [data-state="open"]'
                  ));
                  const visibleCandidate = candidates.find((el) => {
                    const style = window.getComputedStyle(el);
                    if (style.visibility === "hidden" || style.display === "none") return false;
                    const rect = el.getBoundingClientRect();
                    return rect.width > 0 && rect.height > 0;
                  });
                  return visibleCandidate || document;
                };
                const visible = (el) => {
                  const style = window.getComputedStyle(el);
                  if (style.visibility === "hidden" || style.display === "none") return false;
                  const rect = el.getBoundingClientRect();
                  return rect.width > 0 && rect.height > 0;
                };
                const labelFor = (el) => {
                  const id = el.id;
                  if (id) {
                    const explicit = document.querySelector(`label[for="${CSS.escape(id)}"]`);
                    if (explicit) return explicit.innerText.trim();
                  }
                  const wrapped = el.closest("label");
                  if (wrapped) return wrapped.innerText.trim();
                  return "";
                };
                const root = modalRoot();
                const nodes = Array.from(root.querySelectorAll('input, textarea, select'));
                let index = 0;
                return nodes
                  .filter((el) => visible(el))
                  .map((el) => {
                    el.setAttribute('data-jsb-apply-index', String(index));
                    const options = el.tagName === 'SELECT'
                      ? Array.from(el.options || []).map((opt) => ({
                          value: (opt.value || '').trim(),
                          label: (opt.textContent || '').trim()
                        }))
                      : [];
                    const meta = {
                      selector: `[data-jsb-apply-index="${index}"]`,
                      tag: el.tagName.toLowerCase(),
                      type: (el.getAttribute('type') || '').toLowerCase(),
                      name: (el.getAttribute('name') || '').trim(),
                      id: (el.getAttribute('id') || '').trim(),
                      placeholder: (el.getAttribute('placeholder') || '').trim(),
                      ariaLabel: (el.getAttribute('aria-label') || '').trim(),
                      autocomplete: (el.getAttribute('autocomplete') || '').trim(),
                      inputMode: (el.getAttribute('inputmode') || '').trim(),
                      label: labelFor(el),
                      accept: (el.getAttribute('accept') || '').trim(),
                      required: el.required || el.getAttribute('aria-required') === 'true',
                      disabled: !!el.disabled,
                      readOnly: !!el.readOnly,
                      value: typeof el.value === 'string' ? el.value : '',
                      checked: !!el.checked,
                      options,
                    };
                    index += 1;
                    return meta;
                  });
            }"""
        )
    except Exception:
        return []
    return payload if isinstance(payload, list) else []


async def _fill_detected_fields(
    page: Any,
    field_snapshot: list[dict[str, Any]],
    profile: CandidateApplicationProfile,
    cover_letter_draft: str,
) -> FillResult:
    result = FillResult()
    filled_once: set[str] = set()
    for field_meta in field_snapshot:
        field_type = _classify_field(field_meta)
        selector = str(field_meta.get("selector") or "").strip()
        if not selector or not field_type:
            continue
        if bool(field_meta.get("disabled")):
            continue

        try:
            locator = page.locator(selector).first
        except Exception:
            continue

        if field_type == "cv_upload":
            try:
                await locator.set_input_files(str(profile.canonical_cv_path))
                result.cv_uploaded = True
            except Exception:
                result.skipped_fields.append("cv_upload")
            continue

        if field_type == "cover_letter":
            if not cover_letter_draft:
                result.skipped_fields.append("cover_letter")
                continue
            try:
                await locator.fill(cover_letter_draft)
                result.cover_letter_inserted = True
            except Exception:
                result.skipped_fields.append("cover_letter")
            continue

        value = _field_value(field_type, profile)
        if value is None:
            semantic_key = _common_answer_key_for_field(field_meta)
            semantic_value = _common_answer_for_field(field_meta, profile.common_answers)
            if semantic_value is None:
                continue
            try:
                await _fill_semantic_answer(locator, field_meta, semantic_value)
                result.semantic_answers.append(semantic_key or field_type)
            except Exception:
                result.skipped_fields.append(semantic_key or field_type)
            continue

        if field_type in {"first_name", "last_name", "full_name", "phone", "email"}:
            current_value = str(field_meta.get("value") or "").strip()
            if field_type in filled_once:
                continue
            if current_value:
                filled_once.add(field_type)
                result.prefilled_fields.append(field_type)
                continue
            if bool(field_meta.get("readOnly")) and current_value:
                filled_once.add(field_type)
                result.prefilled_fields.append(field_type)
                continue

        try:
            if field_meta.get("tag") == "select":
                await locator.select_option(label=value)
            else:
                await locator.fill(value)
            result.filled_fields.append(field_type)
            if field_type in {"first_name", "last_name", "full_name", "phone", "email"}:
                filled_once.add(field_type)
        except Exception:
            result.skipped_fields.append(field_type)
    return result


def _looks_like_search_page(field_snapshot: list[dict[str, Any]]) -> bool:
    if len(field_snapshot) > 6 or not field_snapshot:
        return False
    names = {str(item.get("name") or "").strip().lower() for item in field_snapshot}
    placeholders = " ".join(str(item.get("placeholder") or "").lower() for item in field_snapshot)
    labels = " ".join(str(item.get("label") or "").lower() for item in field_snapshot)
    if {"q", "l"}.issubset(names):
        return True
    combined = f"{placeholders} {labels}"
    return "stanowisko" in combined and "miasto" in combined


def _looks_like_company_page(field_snapshot: list[dict[str, Any]], page_url: str) -> bool:
    lowered_url = page_url.lower()
    if "/cmp/" in lowered_url:
        return True
    names = {str(item.get("name") or "").strip().lower() for item in field_snapshot}
    placeholders = " ".join(str(item.get("placeholder") or "").lower() for item in field_snapshot)
    labels = " ".join(str(item.get("label") or "").lower() for item in field_snapshot)
    combined = f"{placeholders} {labels}"
    if "company-search" in names:
        return True
    return "znajdź inną firmę" in combined or "więcej firm" in combined


def _classify_field(field_meta: dict[str, Any]) -> str:
    text = " ".join(
        str(field_meta.get(key) or "").lower()
        for key in ("label", "placeholder", "name", "id", "ariaLabel", "autocomplete")
    )
    tag = str(field_meta.get("tag") or "").lower()
    field_type = str(field_meta.get("type") or "").lower()
    accept = str(field_meta.get("accept") or "").lower()
    autocomplete = str(field_meta.get("autocomplete") or "").lower()

    if autocomplete == "given-name":
        return "first_name"
    if autocomplete == "family-name":
        return "last_name"
    if autocomplete == "name":
        return "full_name"
    if autocomplete == "email":
        return "email"
    if autocomplete in {"tel", "tel-national"}:
        return "phone"
    if autocomplete in {"address-level2", "address-level1"}:
        return "location_city"

    if field_type == "file" or any(marker in accept for marker in CV_FIELD_MARKERS):
        if any(marker in text or marker in accept for marker in CV_FIELD_MARKERS):
            return "cv_upload"
    if field_type == "file" and any(marker in text for marker in ("attachment", "document", "upload")):
        return "cv_upload"

    if tag == "textarea" and any(marker in text for marker in COVER_LETTER_MARKERS):
        return "cover_letter"

    if field_type == "checkbox":
        return "common_answer"
    if field_type == "radio":
        return "common_answer"

    if any(pattern in text for pattern in FIELD_PATTERNS["full_name"]):
        return "full_name"
    if any(pattern in text for pattern in FIELD_PATTERNS["first_name"]):
        return "first_name"
    if any(pattern in text for pattern in FIELD_PATTERNS["last_name"]):
        return "last_name"
    if any(pattern in text for pattern in FIELD_PATTERNS["email"]):
        return "email"
    if any(pattern in text for pattern in FIELD_PATTERNS["phone"]):
        return "phone"
    if any(pattern in text for pattern in FIELD_PATTERNS["location_city"]):
        return "location_city"

    for field_name, patterns in FIELD_PATTERNS.items():
        if field_name in {"full_name", "first_name", "last_name", "email", "phone", "location_city"}:
            continue
        if any(pattern in text for pattern in patterns):
            return field_name

    if tag in {"select"}:
        return "common_answer"
    return ""


def _field_value(field_type: str, profile: CandidateApplicationProfile) -> str | None:
    mapping = {
        "first_name": profile.first_name,
        "last_name": profile.last_name,
        "full_name": profile.full_name,
        "email": profile.email,
        "phone": profile.phone,
        "location_city": profile.location_city,
        "linkedin_url": profile.linkedin_url,
        "github_url": profile.github_url,
        "portfolio_url": profile.portfolio_url or None,
    }
    value = mapping.get(field_type)
    return value if isinstance(value, str) and value.strip() else None


def _common_answer_key_for_field(field_meta: dict[str, Any]) -> str | None:
    text = " ".join(
        str(field_meta.get(key) or "").lower()
        for key in ("label", "placeholder", "name", "id", "ariaLabel")
    )
    for semantic_key, patterns in COMMON_ANSWER_PATTERNS.items():
        if any(pattern in text for pattern in patterns):
            return semantic_key
    return None


def _common_answer_for_field(field_meta: dict[str, Any], common_answers: dict[str, Any]) -> Any:
    semantic_key = _common_answer_key_for_field(field_meta)
    if semantic_key:
        if semantic_key == "required_consent":
            return True
        return common_answers.get(semantic_key)
    return None


async def _fill_semantic_answer(locator: Any, field_meta: dict[str, Any], semantic_value: Any) -> None:
    tag = str(field_meta.get("tag") or "").lower()
    field_type = str(field_meta.get("type") or "").lower()
    if tag == "select":
        label = str(semantic_value).strip()
        await locator.select_option(label=label)
        return

    if field_type == "checkbox":
        desired = bool(semantic_value)
        checked = await locator.is_checked()
        if desired != checked:
            await locator.click()
        return

    if field_type == "radio":
        desired_label = str(semantic_value).strip().lower()
        if desired_label in {"yes", "true", "1"}:
            await locator.click()
        return

    await locator.fill(str(semantic_value))


def _generate_cover_letter_draft(fields: dict[str, Any], profile: CandidateApplicationProfile) -> str:
    company = str(fields.get("Company") or "").strip()
    position = str(fields.get("Position") or "").strip()
    matched = [item.strip() for item in str(fields.get("MatchedSkills") or "").split(",") if item.strip()]
    matched_text = ", ".join(matched[:3]) if matched else "Python and backend work"
    return _truncate_text(
        (
            f"Hello,\n\n"
            f"I'm {profile.full_name} and I'm applying for the {position} role at {company}. "
            f"My recent work includes projects built with {matched_text}, and I'm especially interested in "
            f"this opportunity because it aligns with the kind of engineering and automation work I want to keep growing in.\n\n"
            f"Thank you for your consideration."
        ),
        1200,
    )


def _post_fill_review(fields: dict[str, Any], fill_result: FillResult) -> tuple[str, str]:
    company = str(fields.get("Company") or "").strip() or "this company"
    position = str(fields.get("Position") or "").strip() or "this role"
    if not _stdin_is_interactive():
        return (
            APPLICATION_STATUS_NEEDS_REVIEW,
            f"Form filled for {position} at {company}; final submit intentionally withheld.",
        )

    console.print(
        Panel(
            (
                f"Review the browser for {position} at {company}.\n"
                "Type 'applied' if you submitted it manually, 'skip' to mark it skipped, or press Enter to keep Needs Review."
            ),
            title="Apply Review",
            style="blue",
        )
    )
    try:
        response = input().strip().lower()
    except EOFError:
        response = ""

    if response == "applied":
        return APPLICATION_STATUS_APPLIED, "Application reviewed and submitted manually."
    if response == "skip":
        return APPLICATION_STATUS_SKIPPED, "Application intentionally skipped after review."
    return (
        APPLICATION_STATUS_NEEDS_REVIEW,
        f"Form prepared ({fill_result.note or 'fields filled'}); final submit intentionally withheld.",
    )


def _print_field_debug(field_snapshot: list[dict[str, Any]]) -> None:
    if not field_snapshot:
        console.print(Panel("No visible form fields detected.", title="Apply Field Debug", style="yellow"))
        return

    rows: list[str] = []
    for index, field_meta in enumerate(field_snapshot[:20], start=1):
        rows.append(
            (
                f"{index}. classify={_classify_field(field_meta) or '-'} "
                f"tag={field_meta.get('tag') or '-'} type={field_meta.get('type') or '-'} "
                f"label={field_meta.get('label') or '-'} "
                f"name={field_meta.get('name') or '-'} "
                f"id={field_meta.get('id') or '-'} "
                f"autocomplete={field_meta.get('autocomplete') or '-'} "
                f"placeholder={field_meta.get('placeholder') or '-'}"
            )
        )
    console.print(
        Panel(
            "\n".join(rows),
            title=f"Apply Field Debug ({len(field_snapshot)} fields)",
            style="cyan",
        )
    )


def _truncate_text(value: str, max_chars: int) -> str:
    cleaned = re.sub(r"\s+", " ", value).strip()
    if len(cleaned) <= max_chars:
        return cleaned
    return cleaned[: max_chars - 3].rstrip(" ,;:-") + "..."


def _print_shortlist_preview(updates: list[dict[str, Any]], threshold: int, *, source: str | None) -> None:
    console.print(
        Panel(
            f"Dry run: {len(updates)} job(s) would be marked Ready at threshold {threshold} "
            f"for {source or 'all supported sources'}.",
            title="Apply Shortlist",
            style="blue",
        )
    )
    for update in updates[:20]:
        console.print(update)


def _print_apply_preview(records: list[dict[str, Any]], batch_size: int, *, source: str | None) -> None:
    console.print(
        Panel(
            f"Dry run: {len(records)} approved job(s) would be processed (batch size {batch_size}) "
            f"for {source or 'all supported sources'}.",
            title="Apply",
            style="blue",
        )
    )
    for record in records[:20]:
        fields = record.get("fields", {})
        console.print(
            {
                "id": record["id"],
                "company": fields.get("Company"),
                "position": fields.get("Position"),
                "score": fields.get("Score"),
                "status": fields.get("ApplicationStatus"),
                "link": fields.get("Link"),
            }
        )
