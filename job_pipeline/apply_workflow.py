from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from dotenv import load_dotenv
from requests import HTTPError
from rich.console import Console
from rich.panel import Panel

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
APPLY_SUPPORTED_SOURCES = set(supported_site_names())
DEFAULT_APPLY_THRESHOLD = 80
DEFAULT_APPLY_BATCH_SIZE = 3
DEFAULT_APPLY_SESSION_STATE_PATH = ".session_states/apply-session.json"
DEFAULT_APPLY_RUN_LOCK_PATH = ".session_states/apply-run.lock"
MAX_APPLY_FLOW_RECOVERY_ATTEMPTS = 3
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
    "location_city": ("city", "location", "miasto", "miejscowość"),
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


def shortlist_jobs(
    client: AirtableClient,
    *,
    threshold: int,
    record_ids: list[str] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    ensure_application_fields(client, dry_run=dry_run)
    records = _fetch_offer_records(client)
    updates = _prepare_shortlist_updates(records, threshold=threshold, record_ids=record_ids or [])
    if dry_run:
        _print_shortlist_preview(updates, threshold)
        return {"updated": 0, "candidates": len(updates), "dry_run": True}
    result = client.batch_update_records(updates)
    console.print(Panel(f"Shortlisted {len(result)} job(s) as Ready.", title="Apply Shortlist", style="green"))
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
) -> dict[str, Any]:
    ensure_application_fields(client, dry_run=dry_run)
    records = _fetch_offer_records(client)
    selected = _select_apply_candidates(records, batch_size=batch_size, record_ids=record_ids or [])
    if dry_run:
        _print_apply_preview(selected, batch_size)
        return {"processed": 0, "candidates": len(selected), "dry_run": True}

    if not selected:
        console.print(Panel("No Approved jobs matched this apply run.", title="Apply", style="yellow"))
        return {"processed": 0, "candidates": 0, "dry_run": False}
    if candidate_profile is None:
        raise CandidateApplicationProfileError(
            "A candidate profile is required for apply mode. Create candidate_profile.json from "
            "candidate_profile.example.json or pass --candidate-profile."
        )

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
) -> list[dict[str, Any]]:
    selected = _select_shortlist_candidates(records, threshold=threshold, record_ids=record_ids)
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
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    record_id_filter = set(record_ids)
    for record in records:
        if record_id_filter and record["id"] not in record_id_filter:
            continue
        fields = record.get("fields", {})
        if str(fields.get("Source") or "").strip() not in APPLY_SUPPORTED_SOURCES:
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
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    record_id_filter = set(record_ids)
    for record in records:
        if record_id_filter and record["id"] not in record_id_filter:
            continue
        fields = record.get("fields", {})
        if str(fields.get("Source") or "").strip() not in APPLY_SUPPORTED_SOURCES:
            continue
        if _application_status(fields) != APPLICATION_STATUS_APPROVED:
            continue
        if not str(fields.get("Link") or "").strip():
            continue
        selected.append(record)
    selected.sort(key=_queue_sort_key, reverse=True)
    return selected[:batch_size]


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
        field_snapshot = await _collect_form_fields(apply_page)
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
        recovery_notes: list[str] = []
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

    console.print(
        Panel(
            "Login appears to be required. Complete the sign-in flow in the browser, then press Enter here to continue.",
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
                login_attempted = await _attempt_justjoin_credentials_login(
                    page,
                    email=credentials[0],
                    password=credentials[1],
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


async def _attempt_justjoin_credentials_login(page: Any, *, email: str, password: str) -> bool:
    last_result: dict[str, Any] = {}
    for attempt in range(1, 6):
        try:
            result = await page.evaluate(
                """({ email, password }) => {
                    const visible = (el) => {
                      if (!el) return false;
                      const style = window.getComputedStyle(el);
                      if (style.visibility === "hidden" || style.display === "none") return false;
                      const rect = el.getBoundingClientRect();
                      return rect.width > 0 && rect.height > 0;
                    };
                    const inputs = Array.from(document.querySelectorAll('input'));
                    const passwordInput = inputs.find((el) =>
                      visible(el) && (
                        (el.getAttribute('type') || '').toLowerCase() === 'password' ||
                        ((el.getAttribute('autocomplete') || '').toLowerCase().includes('password'))
                      )
                    );
                    const emailInput = inputs.find((el) => {
                      if (!visible(el) || el === passwordInput) return false;
                      const type = (el.getAttribute('type') || '').toLowerCase();
                      const auto = (el.getAttribute('autocomplete') || '').toLowerCase();
                      const name = (el.getAttribute('name') || '').toLowerCase();
                      const id = (el.getAttribute('id') || '').toLowerCase();
                      const placeholder = (el.getAttribute('placeholder') || '').toLowerCase();
                      const label = (el.closest('label')?.innerText || '').toLowerCase();
                      return (
                        type === 'email' ||
                        auto.includes('email') ||
                        auto.includes('username') ||
                        name.includes('email') ||
                        name.includes('login') ||
                        id.includes('email') ||
                        id.includes('login') ||
                        placeholder.includes('email') ||
                        placeholder.includes('login') ||
                        label.includes('email')
                      );
                    });

                    const inputEvent = new Event('input', { bubbles: true });
                    const changeEvent = new Event('change', { bubbles: true });
                    const buttons = Array.from(document.querySelectorAll('button, [role="button"], input[type="submit"]'));
                    const continueButton = buttons.find((el) => {
                      if (!visible(el)) return false;
                      const text = ((el.textContent || el.getAttribute('value') || el.getAttribute('aria-label') || '')).trim().toLowerCase();
                      return (
                        text === 'continue' ||
                        text === 'next' ||
                        text === 'dalej' ||
                        text === 'sign in' ||
                        text === 'log in' ||
                        text === 'zaloguj'
                      );
                    });

                    if (!emailInput && !passwordInput) {
                      return { attempted: false, reason: 'no_auth_inputs' };
                    }

                    if (emailInput && !passwordInput) {
                      emailInput.focus();
                      emailInput.value = email;
                      emailInput.dispatchEvent(inputEvent);
                      emailInput.dispatchEvent(changeEvent);
                      if (continueButton) {
                        continueButton.click();
                        return { attempted: true, reason: 'email_only_step', submitted: 'continue' };
                      }
                      return { attempted: true, reason: 'email_only_step', submitted: 'filled_only' };
                    }

                    if (!emailInput && passwordInput) {
                      passwordInput.focus();
                      passwordInput.value = password;
                      passwordInput.dispatchEvent(inputEvent);
                      passwordInput.dispatchEvent(changeEvent);
                      if (continueButton) {
                        continueButton.click();
                        return { attempted: true, reason: 'password_only_step', submitted: 'continue' };
                      }
                      return { attempted: true, reason: 'password_only_step', submitted: 'filled_only' };
                    }

                    emailInput.focus();
                    emailInput.value = email;
                    emailInput.dispatchEvent(inputEvent);
                    emailInput.dispatchEvent(changeEvent);
                    passwordInput.focus();
                    passwordInput.value = password;
                    passwordInput.dispatchEvent(inputEvent);
                    passwordInput.dispatchEvent(changeEvent);

                    const form = passwordInput.form || emailInput.form;
                    if (form) {
                      const submitter = form.querySelector('button[type="submit"], input[type="submit"]');
                      if (submitter && visible(submitter)) {
                        submitter.click();
                        return { attempted: true, reason: 'email_and_password', submitted: 'button' };
                      }
                      if (typeof form.requestSubmit === 'function') {
                        form.requestSubmit();
                        return { attempted: true, reason: 'email_and_password', submitted: 'requestSubmit' };
                      }
                      form.submit();
                      return { attempted: true, reason: 'email_and_password', submitted: 'submit' };
                    }

                    if (continueButton) {
                      continueButton.click();
                      return { attempted: true, reason: 'email_and_password', submitted: 'continue' };
                    }

                    return { attempted: true, reason: 'email_and_password', submitted: 'filled_only' };
                }""",
                {"email": email, "password": password},
            )
        except Exception as exc:  # noqa: BLE001
            _log_apply_event("JUSTJOIN_CREDENTIAL_LOGIN_ERROR", error=str(exc)[:240], attempt=str(attempt))
            return False

        last_result = dict(result or {})
        attempted = bool(last_result.get("attempted"))
        _log_apply_event(
            "JUSTJOIN_CREDENTIAL_LOGIN_ATTEMPT",
            attempt=str(attempt),
            attempted=str(attempted).lower(),
            reason=str(last_result.get("reason") or "-")[:120],
            submitted=str(last_result.get("submitted") or "-")[:120],
            page_url=str(getattr(page, "url", "") or ""),
        )
        if attempted and str(last_result.get("reason") or "") not in {"no_auth_inputs", "email_only_step"}:
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
                  applyFieldCount,
                };
            }""",
            list(LOGIN_TEXT_MARKERS),
        )
    except Exception:
        return False

    if bool(snapshot.get("hasPassword")):
        return True

    if int(snapshot.get("applyFieldCount") or 0) >= 2:
        return False

    title = str(snapshot.get("title") or "")
    return bool(snapshot.get("markerHit")) or any(
        marker in title for marker in LOGIN_TEXT_MARKERS
    )


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


@dataclass
class FillResult:
    filled_fields: list[str] = field(default_factory=list)
    semantic_answers: list[str] = field(default_factory=list)
    cv_uploaded: bool = False
    cover_letter_inserted: bool = False
    skipped_fields: list[str] = field(default_factory=list)

    @property
    def note(self) -> str:
        parts: list[str] = []
        if self.filled_fields:
            parts.append(f"Filled: {', '.join(self.filled_fields[:6])}")
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
    for field_meta in field_snapshot:
        field_type = _classify_field(field_meta)
        selector = str(field_meta.get("selector") or "").strip()
        if not selector or not field_type:
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

        try:
            if field_meta.get("tag") == "select":
                await locator.select_option(label=value)
            else:
                await locator.fill(value)
            result.filled_fields.append(field_type)
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


def _print_shortlist_preview(updates: list[dict[str, Any]], threshold: int) -> None:
    console.print(
        Panel(
            f"Dry run: {len(updates)} job(s) would be marked Ready at threshold {threshold}.",
            title="Apply Shortlist",
            style="blue",
        )
    )
    for update in updates[:20]:
        console.print(update)


def _print_apply_preview(records: list[dict[str, Any]], batch_size: int) -> None:
    console.print(
        Panel(
            f"Dry run: {len(records)} approved job(s) would be processed (batch size {batch_size}).",
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
