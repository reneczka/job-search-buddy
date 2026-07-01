from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from openai import AsyncOpenAI
from rich.console import Console
from rich.panel import Panel

from airtable_client import AirtableClient
from config import DEFAULT_OPENAI_MAX_RETRIES, DEFAULT_OPENAI_MODEL, DEFAULT_OPENAI_TIMEOUT


console = Console()
SCORING_FLUSH_CHUNK_SIZE = 10

BASELINE_SCORE = 50
MATCHED_CORE_WEIGHT = 12
MATCHED_SECONDARY_WEIGHT = 6
MISSING_CORE_WEIGHT = -8
MISSING_SECONDARY_WEIGHT = -3
PREFERRED_MATCH_BONUS = 2
BONUS_MATCH_BONUS = 1
EXPERIENCE_STRETCH_PENALTY_CORE = 5
EXPERIENCE_STRETCH_PENALTY_SECONDARY = 3
COMPLEXITY_STRETCH_PENALTY_CORE = 3
COMPLEXITY_STRETCH_PENALTY_SECONDARY = 2
MAX_EXPERIENCE_STRETCH_PENALTY = 12
MAX_COMPLEXITY_STRETCH_PENALTY = 8
MAX_ADVANCED_CLUSTER_PENALTY = 18
INSUFFICIENT_REQUIREMENTS_SCORE = 10
NO_CONCRETE_TECH_SCORE = 22

REQUIRED_FIELDS = [
    "Position",
    "Company",
    "Requirements",
    "Score",
    "ScoreReason",
    "MatchedSkills",
    "MissingSkills",
]
MISSING_REQUIREMENT_MARKERS = {"", "n/a", "na", "none", "null", "not available", "not provided"}

SECONDARY_MARKERS = (
    "nice to have",
    "nice-to-have",
    "optional",
    "plus",
    "bonus",
    "preferred",
    "mile widziane",
    "atutem będzie",
)
CORE_MARKERS = (
    "must have",
    "required",
    "minimum",
    "at least",
    "experience with",
    "experience in",
    "strong proficiency",
    "solid proficiency",
    "bardzo dobrze znasz",
    "dobra znajomość",
    "znajomość",
    "co najmniej",
)
SOFT_SKILL_MARKERS = (
    "communication",
    "communicate",
    "problem-solving",
    "problem solving",
    "analytical",
    "mindset",
    "teamwork",
    "team work",
    "self-motivation",
    "samodzielność",
    "komunikatywność",
    "creative",
    "creativity",
    "responsibility",
    "proactive",
    "proaktyw",
)
EXPERIENCE_STRETCH_PATTERNS = (
    r"\b\d+\+?\s+(?:years?|year|lata|lat|rok)\b",
    r"\bcommercial experience\b",
    r"\bexperience as a backend engineer\b",
    r"\bexperience as a backend developer\b",
    r"\bbackend engineer\b",
    r"\bbackend developer\b",
    r"\bdoświadczeni[ea]\s+(?:komercyjn\w+|zawodow\w+)\b",
    r"\bdoświadczenie jako deweloper\b",
    r"\bdoświadczenie na podobnym stanowisku\b",
)
COMPLEXITY_STRETCH_PATTERNS = (
    r"\barchitecture\b",
    r"\barchitectural patterns?\b",
    r"\bdistributed systems?\b",
    r"\bproduction systems?\b",
    r"\breliability\b",
    r"\blarge[- ]scale systems?\b",
    r"\bscalable systems?\b",
    r"\bprojektowani[ea] systemów it\b",
    r"\bwzorce architektoniczne\b",
    r"\bniezawodność\b",
    r"\bsystemy rozproszone\b",
)

SKILL_ALIASES: dict[str, tuple[str, ...]] = {
    "Python": (r"\bpython\b",),
    "Django": (r"\bdjango\b",),
    "Flask": (r"\bflask\b",),
    "FastAPI": (r"\bfastapi\b",),
    "PostgreSQL": (r"\bpostgresql\b", r"\bpostgres\b"),
    "SQLAlchemy": (r"\bsqlalchemy\b",),
    "SQL": (r"\bsql\b",),
    "Pandas": (r"\bpandas\b",),
    "NumPy": (r"\bnumpy\b",),
    "Docker": (r"\bdocker\b",),
    "Kubernetes": (r"\bkubernetes\b", r"\bk8s\b"),
    "Angular": (r"\bangular\b",),
    "Git": (r"\bgit\b",),
    "GitHub Actions": (r"\bgithub actions\b",),
    "GitLab CI": (r"\bgitlab ci\b",),
    "CI/CD": (r"\bci\s*/\s*cd\b", r"\bci cd\b"),
    "Jenkins": (r"\bjenkins\b",),
    "AWS": (r"\baws\b", r"\bamazon web services\b"),
    "GCP": (r"\bgcp\b", r"\bgoogle cloud\b", r"\bgoogle cloud platform\b"),
    "Azure": (r"\bazure\b",),
    "Terraform": (r"\bterraform\b",),
    "Ansible": (r"\bansible\b",),
    "Linux": (r"\blinux\b",),
    "Bash": (r"\bbash\b",),
    "PowerShell": (r"\bpowershell\b",),
    "Node.js": (r"\bnode\.?js\b",),
    "JavaScript": (r"\bjavascript\b", r"\bjs\b"),
    "TypeScript": (r"\btypescript\b", r"\bts\b"),
    "React": (r"\breact\b", r"\breactjs\b"),
    "HTML": (r"\bhtml(?:5)?\b",),
    "CSS": (r"\bcss(?:3)?\b",),
    "Go": (r"\bgolang\b", r"\bgo language\b"),
    "Java": (r"\bjava\b",),
    "C++": (r"\bc\+\+\b",),
    "C#": (r"\bc#\b", r"\bc sharp\b", r"\.net\b"),
    "PHP": (r"\bphp\b",),
    "PyTorch": (r"\bpytorch\b",),
    "TensorFlow": (r"\btensorflow\b",),
    "LLM": (r"\bllms?\b", r"\blarge language models?\b"),
    "RAG": (r"\brag\b", r"\bretrieval[- ]augmented generation\b"),
    "GenAI": (r"\bgenai\b", r"\bgenerative ai\b"),
    "LangChain": (r"\blangchain\b",),
    "LlamaIndex": (r"\bllamaindex\b",),
    "LangGraph": (r"\blanggraph\b",),
    "Streamlit": (r"\bstreamlit\b",),
    "BPMN": (r"\bbpmn\b",),
    "Excel": (r"\bexcel\b",),
    "Power BI": (r"\bpower bi\b", r"\bpowerbi\b"),
    "PowerPoint": (r"\bpowerpoint\b",),
    "Databricks": (r"\bdatabricks\b",),
    "Spark": (r"\bspark\b",),
    "Hive": (r"\bhive\b",),
    "Presto": (r"\bpresto\b",),
    "PySpark": (r"\bpyspark\b",),
    "DRF": (r"\bdrf\b", r"\bdjango rest framework\b"),
    "REST APIs": (r"\brest api\b", r"\brest apis\b", r"\bapis\b", r"\bapi\b"),
    "Oracle": (r"\boracle\b",),
    "SAP": (r"\bsap\b",),
    "OpenAI API": (r"\bopenai api\b", r"\bopenai agents sdk\b"),
    "Airtable": (r"\bairtable\b",),
    "Playwright": (r"\bplaywright\b",),
    "AsyncIO": (r"\basyncio\b",),
    "OpenSearch": (r"\bopensearch\b",),
    "MySQL": (r"\bmysql\b",),
    "Grafana": (r"\bgrafana\b",),
    "Prometheus": (r"\bprometheus\b",),
    "Zabbix": (r"\bzabbix\b",),
}

ADVANCED_SKILL_CLUSTERS: dict[str, str] = {
    "Pandas": "ml_data",
    "NumPy": "ml_data",
    "PyTorch": "ml_data",
    "TensorFlow": "ml_data",
    "LLM": "ml_data",
    "RAG": "ml_data",
    "GenAI": "ml_data",
    "LangChain": "ml_data",
    "LlamaIndex": "ml_data",
    "LangGraph": "ml_data",
    "Streamlit": "ml_data",
    "AWS": "cloud_platform",
    "GCP": "cloud_platform",
    "Azure": "cloud_platform",
    "Docker": "cloud_platform",
    "Kubernetes": "cloud_platform",
    "Terraform": "cloud_platform",
    "Ansible": "cloud_platform",
    "Linux": "cloud_platform",
    "PowerShell": "cloud_platform",
    "Databricks": "data_engineering",
    "Spark": "data_engineering",
    "Hive": "data_engineering",
    "Presto": "data_engineering",
    "PySpark": "data_engineering",
    "SQL": "data_engineering",
    "PostgreSQL": "data_engineering",
    "MySQL": "data_engineering",
    "React": "frontend",
    "Angular": "frontend",
    "HTML": "frontend",
    "CSS": "frontend",
    "TypeScript": "frontend",
    "JavaScript": "frontend",
}

ADVANCED_CLUSTER_RULES: dict[str, tuple[int, int, int]] = {
    "ml_data": (8, 2, 14),
    "cloud_platform": (4, 1, 8),
    "data_engineering": (4, 1, 8),
    "frontend": (3, 1, 6),
}

ADVANCED_CLUSTER_LABELS: dict[str, str] = {
    "ml_data": "ML/data-science",
    "cloud_platform": "cloud/platform",
    "data_engineering": "data-engineering",
    "frontend": "frontend",
}


@dataclass
class CandidateProfile:
    cv_text_raw: str
    preferences_raw: Dict[str, Any]
    evidenced_skills: set[str]
    preferred_skills: set[str]
    bonus_skills: set[str]
    excluded_seniority_markers: set[str]


@dataclass
class RequirementItem:
    raw_text: str
    technologies: list[str]
    importance: str
    ambiguous_importance: bool = False


@dataclass
class ScoreBreakdown:
    score: int
    matched_skills: list[str] = field(default_factory=list)
    missing_skills: list[str] = field(default_factory=list)
    matched_core: list[str] = field(default_factory=list)
    matched_secondary: list[str] = field(default_factory=list)
    missing_core: list[str] = field(default_factory=list)
    missing_secondary: list[str] = field(default_factory=list)
    stretch_penalty: int = 0
    advanced_cluster_penalty: int = 0
    advanced_clusters: list[str] = field(default_factory=list)
    insufficient_signal: bool = False


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

    preferred_skills = _normalize_skill_set(
        preferences.get("required_keywords", [])
    ) | _normalize_skill_set(preferences.get("preferred_keywords", []))
    bonus_skills = _normalize_skill_set(preferences.get("bonus_keywords", []))
    evidenced_seed_skills = preferred_skills | bonus_skills | _normalize_skill_set(preferences.get("required_keywords", []))
    evidenced_skills = _extract_skills_from_text(cv_text, hinted_skills=evidenced_seed_skills)
    excluded_seniority_markers = set(_normalize_list(preferences.get("excluded_keywords", [])))

    return CandidateProfile(
        cv_text_raw=cv_text,
        preferences_raw=preferences,
        evidenced_skills=evidenced_skills,
        preferred_skills=preferred_skills,
        bonus_skills=bonus_skills,
        excluded_seniority_markers=excluded_seniority_markers,
    )


def _normalize_list(values: Any) -> List[str]:
    if not isinstance(values, list):
        return []
    normalized: List[str] = []
    for value in values:
        if value is None:
            continue
        cleaned = str(value).strip()
        if cleaned:
            normalized.append(cleaned)
    return normalized


def _normalize_skill_set(values: Any) -> set[str]:
    return {canonical for canonical in (_canonical_skill(value) for value in _normalize_list(values)) if canonical}


def _canonical_skill(value: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned:
        return ""
    for canonical, patterns in SKILL_ALIASES.items():
        if any(re.search(pattern, cleaned, flags=re.IGNORECASE) for pattern in patterns):
            return canonical
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > 40:
        return ""
    if re.search(r"[a-zA-Z]", cleaned) is None:
        return ""
    return cleaned


def _extract_skills_from_text(text: str, hinted_skills: Iterable[str] = ()) -> set[str]:
    lowered_text = text.lower()
    skills: set[str] = set()
    for canonical, patterns in SKILL_ALIASES.items():
        if _matches_skill_patterns(text, canonical, patterns) or _contains_skill_variant(text, canonical):
            skills.add(canonical)
    for skill in hinted_skills:
        if skill and (skill.lower() in lowered_text or _contains_skill_variant(text, skill)):
            skills.add(skill)
    return skills


def _split_requirement_lines(value: Any) -> list[str]:
    text = str(value or "").strip()
    if not text:
        return []
    if text.lower() in MISSING_REQUIREMENT_MARKERS:
        return []

    lines = []
    for line in re.split(r"[\r\n]+", text):
        cleaned = re.sub(r"^[\-\*\u2022]+\s*", "", line).strip()
        if cleaned and cleaned.lower() not in MISSING_REQUIREMENT_MARKERS:
            lines.append(cleaned)
    if len(lines) > 1:
        return lines

    compact = re.sub(r"\s+", " ", text)
    if " - " in compact:
        split = [part.strip() for part in re.split(r"\s+-\s+", compact) if part.strip()]
        if len(split) > 1:
            return split
    return [compact]


def _extract_requirement_items(fields: Dict[str, Any]) -> list[RequirementItem]:
    requirement_lines = _split_requirement_lines(fields.get("Requirements", ""))
    items: list[RequirementItem] = []
    total = max(len(requirement_lines), 1)
    for index, raw in enumerate(requirement_lines):
        technologies = _extract_requirement_technologies(raw)
        importance, ambiguous = _initial_importance(raw, index=index, total=total)
        items.append(
            RequirementItem(
                raw_text=raw,
                technologies=technologies,
                importance=importance,
                ambiguous_importance=ambiguous,
            )
        )
    return items


def _extract_requirement_technologies(text: str) -> list[str]:
    if not text:
        return []
    lowered = text.lower()
    if any(marker in lowered for marker in SOFT_SKILL_MARKERS) and not any(
        re.search(pattern, text, flags=re.IGNORECASE)
        for patterns in SKILL_ALIASES.values()
        for pattern in patterns
    ):
        return []

    found: list[str] = []
    for canonical, patterns in SKILL_ALIASES.items():
        if _matches_skill_patterns(text, canonical, patterns) or _contains_skill_variant(text, canonical):
            found.append(canonical)

    deduped: list[str] = []
    seen: set[str] = set()
    for skill in found:
        if skill not in seen:
            seen.add(skill)
            deduped.append(skill)
    return deduped


def _contains_skill_variant(text: str, skill: str) -> bool:
    normalized_text = re.sub(r"[^a-z0-9+#.]+", " ", text.lower())
    normalized_skill = re.sub(r"[^a-z0-9+#.]+", " ", skill.lower()).strip()
    if not normalized_skill or len(normalized_skill) < 4:
        return False
    if " " in normalized_skill:
        return normalized_skill in normalized_text
    pattern = rf"\b{re.escape(normalized_skill)}[a-z]{{0,2}}\b"
    return re.search(pattern, normalized_text, flags=re.IGNORECASE) is not None


def _matches_skill_patterns(text: str, canonical: str, patterns: tuple[str, ...]) -> bool:
    if canonical == "Go" and re.search(r"\bGo\b", text):
        return True
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def _initial_importance(text: str, *, index: int, total: int) -> tuple[str, bool]:
    lowered = text.lower()
    if any(marker in lowered for marker in SECONDARY_MARKERS):
        return "secondary", False
    if any(marker in lowered for marker in CORE_MARKERS):
        return "core", False
    if re.search(r"\b\d+\+?\s+(?:years?|lata|rok)\b", lowered):
        return "core", False
    if index < max(1, total // 2):
        return "core", True
    return "secondary", True


def _apply_importance_overrides(
    items: list[RequirementItem],
    overrides: dict[int, str],
) -> list[RequirementItem]:
    updated: list[RequirementItem] = []
    for idx, item in enumerate(items):
        override = overrides.get(idx)
        if override in {"core", "secondary"}:
            updated.append(
                RequirementItem(
                    raw_text=item.raw_text,
                    technologies=item.technologies,
                    importance=override,
                    ambiguous_importance=False,
                )
            )
            continue
        updated.append(item)
    return updated


def _stretch_penalty(items: list[RequirementItem]) -> int:
    experience_penalty = 0
    complexity_penalty = 0

    for item in items:
        lowered = item.raw_text.lower()
        if any(re.search(pattern, lowered, flags=re.IGNORECASE) for pattern in EXPERIENCE_STRETCH_PATTERNS):
            if item.importance == "core":
                experience_penalty += EXPERIENCE_STRETCH_PENALTY_CORE
            else:
                experience_penalty += EXPERIENCE_STRETCH_PENALTY_SECONDARY
        if any(re.search(pattern, lowered, flags=re.IGNORECASE) for pattern in COMPLEXITY_STRETCH_PATTERNS):
            if item.importance == "core":
                complexity_penalty += COMPLEXITY_STRETCH_PENALTY_CORE
            else:
                complexity_penalty += COMPLEXITY_STRETCH_PENALTY_SECONDARY

    experience_penalty = min(experience_penalty, MAX_EXPERIENCE_STRETCH_PENALTY)
    complexity_penalty = min(complexity_penalty, MAX_COMPLEXITY_STRETCH_PENALTY)
    return experience_penalty + complexity_penalty


def _has_concrete_requirement_signal(items: list[RequirementItem]) -> bool:
    return any(item.technologies for item in items)


def _advanced_cluster_penalty(missing_skills: list[str]) -> tuple[int, list[str]]:
    cluster_counts: dict[str, int] = {}
    for skill in missing_skills:
        cluster = ADVANCED_SKILL_CLUSTERS.get(skill)
        if not cluster:
            continue
        cluster_counts[cluster] = cluster_counts.get(cluster, 0) + 1

    total_penalty = 0
    triggered_clusters: list[str] = []
    for cluster, count in cluster_counts.items():
        if count < 2:
            continue
        base, extra_per_skill, cluster_cap = ADVANCED_CLUSTER_RULES[cluster]
        cluster_penalty = min(cluster_cap, base + max(0, count - 2) * extra_per_skill)
        total_penalty += cluster_penalty
        triggered_clusters.append(cluster)

    total_penalty = min(total_penalty, MAX_ADVANCED_CLUSTER_PENALTY)
    triggered_clusters.sort(key=lambda name: cluster_counts[name], reverse=True)
    return total_penalty, triggered_clusters


def _calculate_score(profile: CandidateProfile, items: list[RequirementItem]) -> ScoreBreakdown:
    stretch_penalty = _stretch_penalty(items)
    if not items:
        return ScoreBreakdown(
            score=INSUFFICIENT_REQUIREMENTS_SCORE,
            stretch_penalty=stretch_penalty,
            advanced_cluster_penalty=0,
            insufficient_signal=True,
        )

    if not _has_concrete_requirement_signal(items):
        score = max(0, NO_CONCRETE_TECH_SCORE - stretch_penalty)
        return ScoreBreakdown(
            score=score,
            stretch_penalty=stretch_penalty,
            advanced_cluster_penalty=0,
            insufficient_signal=True,
        )

    technology_importance: dict[str, str] = {}
    for item in items:
        if not item.technologies:
            continue
        for technology in item.technologies:
            previous = technology_importance.get(technology)
            if previous == "core":
                continue
            technology_importance[technology] = item.importance

    matched_core: list[str] = []
    matched_secondary: list[str] = []
    missing_core: list[str] = []
    missing_secondary: list[str] = []

    for technology, importance in sorted(technology_importance.items()):
        has_evidence = technology in profile.evidenced_skills
        if has_evidence and importance == "core":
            matched_core.append(technology)
        elif has_evidence:
            matched_secondary.append(technology)
        elif importance == "core":
            missing_core.append(technology)
        else:
            missing_secondary.append(technology)

    score = BASELINE_SCORE
    score += len(matched_core) * MATCHED_CORE_WEIGHT
    score += len(matched_secondary) * MATCHED_SECONDARY_WEIGHT
    score += len(missing_core) * MISSING_CORE_WEIGHT
    score += len(missing_secondary) * MISSING_SECONDARY_WEIGHT

    score += sum(1 for skill in matched_core if skill in profile.preferred_skills) * PREFERRED_MATCH_BONUS
    score += sum(1 for skill in matched_secondary if skill in profile.preferred_skills) * PREFERRED_MATCH_BONUS
    score += sum(1 for skill in matched_core if skill in profile.bonus_skills) * BONUS_MATCH_BONUS
    score += sum(1 for skill in matched_secondary if skill in profile.bonus_skills) * BONUS_MATCH_BONUS

    advanced_cluster_penalty, advanced_clusters = _advanced_cluster_penalty(missing_core + missing_secondary)
    score -= stretch_penalty
    score -= advanced_cluster_penalty

    score = max(0, min(100, score))
    return ScoreBreakdown(
        score=score,
        matched_skills=matched_core + matched_secondary,
        missing_skills=missing_core + missing_secondary,
        matched_core=matched_core,
        matched_secondary=matched_secondary,
        missing_core=missing_core,
        missing_secondary=missing_secondary,
        stretch_penalty=stretch_penalty,
        advanced_cluster_penalty=advanced_cluster_penalty,
        advanced_clusters=advanced_clusters,
        insufficient_signal=False,
    )


def _build_fallback_reason(breakdown: ScoreBreakdown) -> str:
    if breakdown.insufficient_signal:
        return "Insufficient concrete requirement data to assess fit reliably."

    if breakdown.matched_core:
        lead = f"Strongest fit: {', '.join(breakdown.matched_core[:3])}"
    elif breakdown.matched_secondary:
        lead = f"Some alignment: {', '.join(breakdown.matched_secondary[:3])}"
    else:
        lead = "Limited direct technology overlap"

    if breakdown.missing_core:
        gap = f"Main gap: {', '.join(breakdown.missing_core[:3])}"
    elif breakdown.missing_secondary:
        gap = f"Secondary gap: {', '.join(breakdown.missing_secondary[:3])}"
    else:
        gap = "No major missing technologies detected"

    if breakdown.stretch_penalty >= 8:
        stretch = " Role looks like a stretch on experience or systems depth."
    elif breakdown.stretch_penalty >= 4:
        stretch = " Some stretch-role expectations are present."
    else:
        stretch = ""

    if breakdown.advanced_clusters:
        cluster_label = ADVANCED_CLUSTER_LABELS.get(breakdown.advanced_clusters[0], breakdown.advanced_clusters[0])
        cluster_note = f" Missing several advanced {cluster_label} technologies."
    else:
        cluster_note = ""

    return f"{lead}. {gap}.{stretch}{cluster_note}"


async def _call_llm(
    client: AsyncOpenAI,
    model: str,
    prompt: str,
    max_attempts: int = DEFAULT_OPENAI_MAX_RETRIES,
) -> str:
    last_error: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "Respond only with valid JSON."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
            )
            message = response.choices[0].message.content or ""
            return message.strip()
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            await asyncio.sleep(1.0 * attempt)
    raise RuntimeError(f"LLM scoring failed after {max_attempts} attempts: {last_error}") from last_error


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


def _build_importance_prompt(items: list[RequirementItem]) -> str:
    item_lines = "\n".join(f"{idx}: {item.raw_text}" for idx, item in enumerate(items))
    return (
        "Classify each requirement item as core or secondary for ranking a junior-friendly job offer. "
        "Return strict JSON with format "
        '{"items":[{"index":0,"importance":"core"}]}. '
        "Prefer secondary for bonus/nice-to-have wording. Prefer core for real must-have technologies. "
        f"Items:\n{item_lines}"
    )


def _build_reason_prompt(
    fields: Dict[str, Any],
    breakdown: ScoreBreakdown,
) -> str:
    position = str(fields.get("Position") or "").strip()
    company = str(fields.get("Company") or "").strip()
    return (
        "Write one short English reason for this compatibility score. "
        "Use only the provided matched and missing technologies. "
        "Return strict JSON: {\"ScoreReason\":\"...\"}.\n"
        f"Job: {position} at {company}\n"
        f"Score: {breakdown.score}\n"
        f"Matched core: {', '.join(breakdown.matched_core) or 'none'}\n"
        f"Matched secondary: {', '.join(breakdown.matched_secondary) or 'none'}\n"
        f"Missing core: {', '.join(breakdown.missing_core) or 'none'}\n"
        f"Missing secondary: {', '.join(breakdown.missing_secondary) or 'none'}"
    )


async def _resolve_ambiguous_importance(
    client: Optional[AsyncOpenAI],
    model: str,
    items: list[RequirementItem],
) -> dict[int, str]:
    if client is None:
        return {}
    ambiguous = [item for item in items if item.ambiguous_importance]
    if not ambiguous:
        return {}

    prompt = _build_importance_prompt(ambiguous)
    try:
        payload = await _call_llm(client, model, prompt)
    except Exception:
        return {}
    parsed = _parse_llm_json(payload)
    if not parsed or not isinstance(parsed.get("items"), list):
        return {}

    overrides: dict[int, str] = {}
    for local_index, raw in enumerate(parsed["items"]):
        if not isinstance(raw, dict):
            continue
        item_index = raw.get("index")
        importance = str(raw.get("importance") or "").strip().lower()
        if not isinstance(item_index, int) or importance not in {"core", "secondary"}:
            continue
        if 0 <= item_index < len(ambiguous):
            original = ambiguous[item_index]
            original_index = next((idx for idx, item in enumerate(items) if item.raw_text == original.raw_text), None)
            if original_index is not None:
                overrides[original_index] = importance
    return overrides


async def _generate_reason(
    client: Optional[AsyncOpenAI],
    model: str,
    fields: Dict[str, Any],
    breakdown: ScoreBreakdown,
) -> str:
    fallback = _build_fallback_reason(breakdown)
    if client is None:
        return fallback
    try:
        payload = await _call_llm(client, model, _build_reason_prompt(fields, breakdown))
    except Exception:
        return fallback
    parsed = _parse_llm_json(payload)
    if not parsed:
        return fallback
    reason = str(parsed.get("ScoreReason") or "").strip()
    return reason or fallback


def _format_update(record_id: str, result: Dict[str, Any]) -> Dict[str, Any]:
    insufficient_signal = bool(result.get("InsufficientSignal"))
    fields = {
        "Score": result.get("Score", 0),
        "ScoreReason": result.get("ScoreReason", ""),
        "MatchedSkills": "N/A" if insufficient_signal and not result.get("MatchedSkills") else ", ".join(result.get("MatchedSkills", [])),
        "MissingSkills": "N/A" if insufficient_signal and not result.get("MissingSkills") else ", ".join(result.get("MissingSkills", [])),
    }
    return {"id": record_id, "fields": fields}


def _changed_scoring_fields(record: Dict[str, Any], update_fields: Dict[str, str]) -> dict[str, Any]:
    existing = record.get("fields", {}) if isinstance(record.get("fields"), dict) else {}
    changed: dict[str, Any] = {}
    for key, value in update_fields.items():
        current = str(existing.get(key) or "").strip()
        candidate = str(value or "").strip()
        if current != candidate:
            changed[key] = value
    return changed


def _flush_scoring_updates(
    airtable_client: AirtableClient,
    updates: List[Dict[str, Any]],
) -> int:
    if not updates:
        return 0

    airtable_client.batch_update_records(updates)
    flushed = len(updates)
    updates.clear()
    return flushed


async def _score_record_fields(
    profile: CandidateProfile,
    fields: Dict[str, Any],
    client: Optional[AsyncOpenAI],
    model: str,
) -> Dict[str, Any]:
    requirement_items = _extract_requirement_items(fields)
    overrides = await _resolve_ambiguous_importance(client, model, requirement_items)
    requirement_items = _apply_importance_overrides(requirement_items, overrides)
    breakdown = _calculate_score(profile, requirement_items)
    score_reason = await _generate_reason(client, model, fields, breakdown)
    return {
        "Score": breakdown.score,
        "ScoreReason": score_reason,
        "MatchedSkills": breakdown.matched_skills,
        "MissingSkills": breakdown.missing_skills,
        "InsufficientSignal": breakdown.insufficient_signal,
    }


async def score_records(
    airtable_client: AirtableClient,
    record_ids: List[str],
    cv_path: str,
    preferences_path: str,
    *,
    model: str = DEFAULT_OPENAI_MODEL,
) -> None:
    if not record_ids:
        console.print("[dim]No records to score.[/]")
        return

    try:
        profile = load_candidate_profile(cv_path, preferences_path)
    except CandidateProfileError as exc:
        console.print(Panel(str(exc), title="Scoring", style="red"))
        return

    api_key = os.getenv("OPENAI_API_KEY")
    client: Optional[AsyncOpenAI] = None
    if api_key:
        client = AsyncOpenAI(
            api_key=api_key,
            base_url=os.getenv("OPENAI_API_BASE"),
            timeout=DEFAULT_OPENAI_TIMEOUT,
            max_retries=DEFAULT_OPENAI_MAX_RETRIES,
        )
    else:
        console.print(Panel("OPENAI_API_KEY missing - using deterministic scoring only.", title="Scoring", style="yellow"))

    updates: List[Dict[str, Any]] = []
    updated_total = 0

    for record_id in record_ids:
        record = airtable_client.get_record(record_id)
        if not record or "fields" not in record:
            console.print(Panel(f"Skipped {record_id} - missing Airtable data.", title="Scoring", style="red"))
            continue

        fields = record["fields"]
        result = await _score_record_fields(profile, fields, client, model)
        changed_fields = _changed_scoring_fields(record, _format_update(record_id, result)["fields"])
        if not changed_fields:
            console.print(f"[dim]Skipping unchanged scoring row: {record_id}[/]")
            continue
        updates.append({"id": record_id, "fields": changed_fields})
        if len(updates) >= SCORING_FLUSH_CHUNK_SIZE:
            updated_total += _flush_scoring_updates(airtable_client, updates)
            console.print(f"[dim]Persisted scoring progress: {updated_total}/{len(record_ids)}[/]")

    if updates:
        updated_total += _flush_scoring_updates(airtable_client, updates)
        console.print(
            Panel(
                f"Updated scoring for {updated_total} record(s).",
                title="Scoring",
                style="green",
            )
        )
    elif updated_total:
        console.print(
            Panel(
                f"Updated scoring for {updated_total} record(s).",
                title="Scoring",
                style="green",
            )
        )
    else:
        console.print("[dim]No scoring updates were needed.[/]")
