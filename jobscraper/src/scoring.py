from __future__ import annotations

import asyncio
import json
import os
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from openai import AsyncOpenAI
from rich.console import Console
from rich.panel import Panel

from airtable_client import AirtableClient
from config import DEFAULT_OPENAI_MODEL


console = Console()


@dataclass
class CandidateProfile:
    cv_text: str
    preferences: Dict[str, Any]


class CandidateProfileError(RuntimeError):
    """Raised when candidate profile files are missing or invalid."""


def load_candidate_profile(cv_path: str, preferences_path: str) -> CandidateProfile:
    cv_file = Path(cv_path)
    prefs_file = Path(preferences_path)

    if not cv_file.exists():
        raise CandidateProfileError(f"Candidate CV file not found: {cv_file}")
    if not prefs_file.exists():
        raise CandidateProfileError(f"Preferences file not found: {prefs_file}")

    cv_text = cv_file.read_text(encoding="utf-8").strip()
    if not cv_text:
        raise CandidateProfileError("Candidate CV file is empty.")

    try:
        preferences = json.loads(prefs_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CandidateProfileError(f"Invalid JSON in preferences file: {exc}") from exc

    return CandidateProfile(cv_text=cv_text, preferences=preferences)


def _collect_job_text(fields: Dict[str, Any]) -> str:
    relevant_fields = [
        "Position",
        "Company",
        "Location",
        "Salary",
        "Requirements",
        "Notes",
        "Company description",
        "Employment Type",
    ]
    chunks = []
    for field in relevant_fields:
        value = fields.get(field)
        if not value:
            continue
        if isinstance(value, list):
            value = ", ".join(str(item) for item in value if item)
        chunks.append(f"{field}: {value}")
    return "\n".join(chunks)


def _normalize_list(values: Any) -> List[str]:
    if not isinstance(values, list):
        return []
    normalized: List[str] = []
    for value in values:
        if value is None:
            continue
        normalized.append(str(value).strip().lower())
    return [item for item in normalized if item]


def _apply_hard_rules(job_text: str, preferences: Dict[str, Any]) -> Optional[str]:
    lowered = job_text.lower()
    required_keywords = _normalize_list(preferences.get("required_keywords", []))
    experience_keywords = _normalize_list(preferences.get("experience_levels", []))
    excluded_keywords = _normalize_list(preferences.get("excluded_keywords", []))

    messages = preferences.get("hard_rule_messages", {})
    required_msg = messages.get("missing_required_keywords", "Score 0: required keywords missing ({keywords}).")
    experience_msg = messages.get("missing_junior_keywords", "Score 0: required experience keywords missing ({keywords}).")
    excluded_msg = messages.get("contains_excluded_keywords", "Score 0: listing contains excluded wording.")

    if required_keywords and not any(keyword in lowered for keyword in required_keywords):
        required_display = ", ".join(required_keywords)
        return required_msg.format(keywords=required_display)
    if experience_keywords and not any(keyword in lowered for keyword in experience_keywords):
        experience_display = ", ".join(experience_keywords)
        return experience_msg.format(keywords=experience_display)
    if excluded_keywords and any(keyword in lowered for keyword in excluded_keywords):
        return excluded_msg
    return None


async def _call_llm(
    client: AsyncOpenAI,
    model: str,
    prompt: str,
    max_attempts: int = 3,
) -> str:
    last_error: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    {
                        "role": "system",
                        "content": "You are a precise job-matching assistant. Respond ONLY with valid JSON.",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
            )
            message = response.choices[0].message.content or ""
            return message.strip()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            await asyncio.sleep(1.5 * attempt)
    raise RuntimeError(f"LLM scoring failed after {max_attempts} attempts: {last_error}") from last_error


def _build_prompt(
    profile: CandidateProfile,
    job_fields: Dict[str, Any],
    job_text: str,
) -> str:
    prefs_json = json.dumps(profile.preferences, ensure_ascii=False, indent=2)
    job_json = json.dumps(job_fields, ensure_ascii=False, indent=2)
    return textwrap.dedent(
        f"""
        Kandydat (CV markdown):
        ---
        {profile.cv_text}
        ---

        Preferencje kandydata (JSON):
        {prefs_json}

        Oferta pracy (JSON z Airtable):
        {job_json}

        Tekst oferty (złożony z kluczowych pól):
        {job_text}

        Oceń dopasowanie w skali 0-100 i zwróć STRICT JSON w formacie:
        {{
          "Score": <number 0-100>,
          "ScoreReason": "<1-2 zdania po polsku>",
          "MatchedSkills": ["skill1", "skill2", ...],
          "MissingSkills": ["skill_a", ...]
        }}
        """
    ).strip()


def _parse_llm_json(payload: str) -> Optional[Dict[str, Any]]:
    try:
        cleaned = payload.strip()
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start == -1 or end == -1:
            return None
        return json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError:
        return None


def _format_update(record_id: str, result: Dict[str, Any]) -> Dict[str, Any]:
    fields = {
        "Score": result.get("Score", 0),
        "ScoreReason": result.get("ScoreReason", "Brak uzasadnienia."),
        "MatchedSkills": ", ".join(result.get("MatchedSkills", [])),
        "MissingSkills": ", ".join(result.get("MissingSkills", [])),
    }
    return {"id": record_id, "fields": fields}


async def score_records(
    airtable_client: AirtableClient,
    record_ids: List[str],
    cv_path: str,
    preferences_path: str,
    *,
    model: str = DEFAULT_OPENAI_MODEL,
) -> None:
    if not record_ids:
        console.print("[dim]Brak nowych rekordów do scoringu.[/]")
        return

    try:
        profile = load_candidate_profile(cv_path, preferences_path)
    except CandidateProfileError as exc:
        console.print(Panel(str(exc), title="Scoring", style="red"))
        return

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        console.print(Panel("Brak OPENAI_API_KEY – scoring pominięty.", title="Scoring", style="yellow"))
        return

    client = AsyncOpenAI(
        api_key=api_key,
        base_url=os.getenv("OPENAI_API_BASE"),
    )

    updates: List[Dict[str, Any]] = []

    for record_id in record_ids:
        record = airtable_client.get_record(record_id)
        if not record or "fields" not in record:
            console.print(Panel(f"Pominięto rekord {record_id} – brak danych.", title="Scoring", style="red"))
            continue

        fields = record["fields"]
        job_text = _collect_job_text(fields)
        hard_rule_reason = _apply_hard_rules(job_text, profile.preferences)

        if hard_rule_reason:
            updates.append(_format_update(record_id, {
                "Score": 0,
                "ScoreReason": hard_rule_reason,
                "MatchedSkills": [],
                "MissingSkills": [],
            }))
            continue

        prompt = _build_prompt(profile, fields, job_text)
        try:
            llm_raw = await _call_llm(client, model, prompt)
            parsed = _parse_llm_json(llm_raw)
        except Exception as exc:  # noqa: BLE001
            console.print(Panel(f"LLM scoring failed for {record_id}: {exc}", title="Scoring", style="red"))
            parsed = None

        if not parsed:
            parsed = {
                "Score": 0,
                "ScoreReason": "Score 0: nie udało się sparsować odpowiedzi LLM.",
                "MatchedSkills": [],
                "MissingSkills": [],
            }
        updates.append(_format_update(record_id, parsed))

    if updates:
        airtable_client.batch_update_records(updates)
        console.print(Panel(
            f"Zaktualizowano wyniki scoringu dla {len(updates)} rekordów.",
            title="Scoring",
            style="green",
        ))
