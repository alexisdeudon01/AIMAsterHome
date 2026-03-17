#!/usr/bin/env python3
"""
AI Dashboard Builder — HAOS Add-on
Generates Lovelace dashboards via Ollama (local) with Anthropic as fallback.
Writes only to /share/ai_dashboard_builder — never touches existing dashboards.
"""

import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SHARE_DIR = Path("/share/ai_dashboard_builder")
GEN_DIR = SHARE_DIR / "generated"
APPROVED_DIR = SHARE_DIR / "approved"
FAILED_DIR = SHARE_DIR / "failed"
KNOWLEDGE_DIR = SHARE_DIR / "knowledge"
LOG_DIR = SHARE_DIR / "logs"

HA_BASE = "http://supervisor/core/api"
SUPERVISOR_BASE = "http://supervisor"

OPTIONS_FILE = Path(os.environ.get("ADDON_OPTIONS", "/data/options.json"))


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
def setup_logging():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / "run.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(sys.stdout),
        ],
    )


def log(msg, level="info"):
    getattr(logging, level)(msg)


# ---------------------------------------------------------------------------
# Options
# ---------------------------------------------------------------------------
def load_options() -> dict:
    if not OPTIONS_FILE.exists():
        log(f"Options file not found: {OPTIONS_FILE}", "warning")
        return {}
    with OPTIONS_FILE.open() as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# JSONL helper
# ---------------------------------------------------------------------------
def append_jsonl(path: Path, record: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Home Assistant API
# ---------------------------------------------------------------------------
def ha_get(path: str, token: str) -> dict | list | None:
    """GET from HA Core API via Supervisor proxy."""
    url = f"{HA_BASE}{path}"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        log(f"HA API error on {path}: {exc}", "warning")
        return None


def export_ha_context(token: str) -> dict:
    """Fetch config, states, services, and Lovelace from HA."""
    log("Fetching Home Assistant context...")
    context = {}
    context["config"] = ha_get("/config", token) or {}
    context["states"] = ha_get("/states", token) or []
    context["services"] = ha_get("/services", token) or []
    lovelace = ha_get("/lovelace/config", token)
    if lovelace:
        context["lovelace"] = lovelace
    return context


# ---------------------------------------------------------------------------
# Entity analysis
# ---------------------------------------------------------------------------
def analyze_entities(states: list) -> dict:
    """Group entity_id by domain. Return {domain: [entity_id, ...]}."""
    domains: dict[str, list] = {}
    for state in states:
        entity_id = state.get("entity_id", "")
        if not entity_id:
            continue
        domain = entity_id.split(".")[0]
        domains.setdefault(domain, []).append(entity_id)

    # Save a summary
    GEN_DIR.mkdir(parents=True, exist_ok=True)
    summary_path = GEN_DIR / "entity_summary.json"
    summary = {
        "timestamp": _now_iso(),
        "total": sum(len(v) for v in domains.values()),
        "by_domain": {k: len(v) for k, v in domains.items()},
        "domains": domains,
    }
    summary_path.write_text(json.dumps(summary, indent=2, ensure_ascii=False))
    log(f"Entities: {summary['total']} across {len(domains)} domains")
    return domains


# ---------------------------------------------------------------------------
# Lessons
# ---------------------------------------------------------------------------
def load_lessons(n: int = 5) -> list[str]:
    """Return last n lesson strings from lessons_learned.jsonl."""
    path = KNOWLEDGE_DIR / "lessons_learned.jsonl"
    if not path.exists():
        return []
    lines = path.read_text().strip().splitlines()
    lessons = []
    for line in reversed(lines[-n:]):
        try:
            rec = json.loads(line)
            lesson = rec.get("lesson", "")
            if lesson:
                lessons.append(lesson)
        except json.JSONDecodeError:
            pass
    return lessons


# ---------------------------------------------------------------------------
# Prompt builders
# ---------------------------------------------------------------------------
def build_generation_prompt(domains: dict, lessons: list[str]) -> str:
    entity_list = "\n".join(
        f"  {domain}: {', '.join(ids)}" for domain, ids in sorted(domains.items())
    )
    lesson_block = ""
    if lessons:
        lesson_block = (
            "\nPrevious lessons learned:\n"
            + "\n".join(f"- {l}" for l in lessons)
            + "\n"
        )

    return f"""You are a Home Assistant Lovelace dashboard expert.
Generate a VALID Lovelace dashboard YAML for a mobile-first Home Assistant UI.

RULES:
- Return ONLY raw YAML. No markdown fences, no explanations, no comments.
- Use ONLY the entity_ids listed below. Never invent entity_ids.
- Structure with these views (skip a view if no relevant entities exist):
  - System (HA info, uptime)
  - Presence (person, device_tracker)
  - Lights (light)
  - Switches (switch, input_boolean)
  - Sensors (sensor, binary_sensor)
  - Media (media_player)
- Mobile-first: use vertical-stack and entities cards.
- Each view needs a title and an icon.
{lesson_block}
Available entities:
{entity_list}

Output the Lovelace YAML now:"""


def build_repair_prompt(broken_yaml: str, errors: list[str], domains: dict, lessons: list[str]) -> str:
    entity_list = "\n".join(
        f"  {domain}: {', '.join(ids)}" for domain, ids in sorted(domains.items())
    )
    lesson_block = ""
    if lessons:
        lesson_block = (
            "\nPrevious lessons learned:\n"
            + "\n".join(f"- {l}" for l in lessons)
            + "\n"
        )
    error_block = "\n".join(f"- {e}" for e in errors)

    return f"""You are a Home Assistant Lovelace YAML repair expert.

The following Lovelace YAML is broken. Fix it and return ONLY the corrected YAML.

RULES:
- Return ONLY raw YAML. No markdown fences, no explanations.
- Use ONLY the entity_ids in the allowed list below. Never invent entity_ids.
- Fix ALL listed validation errors exactly.
{lesson_block}
Validation errors to fix:
{error_block}

Allowed entity_ids:
{entity_list}

Broken YAML:
{broken_yaml}

Corrected YAML:"""


# ---------------------------------------------------------------------------
# LLM calls
# ---------------------------------------------------------------------------
def call_ollama(prompt: str, url: str, model: str) -> str | None:
    """Call Ollama generate endpoint. Returns raw text or None on failure."""
    endpoint = url.rstrip("/") + "/api/generate"
    payload = {"model": model, "prompt": prompt, "stream": False}
    try:
        resp = requests.post(endpoint, json=payload, timeout=120)
        resp.raise_for_status()
        return resp.json().get("response", "")
    except Exception as exc:
        log(f"Ollama call failed: {exc}", "warning")
        return None


def call_anthropic(prompt: str, api_key: str, model: str) -> str | None:
    """Call Anthropic Messages API. Returns raw text or None on failure."""
    endpoint = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": prompt}],
    }
    try:
        resp = requests.post(endpoint, headers=headers, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        return data["content"][0]["text"]
    except Exception as exc:
        log(f"Anthropic call failed: {exc}", "warning")
        return None


# ---------------------------------------------------------------------------
# YAML extraction & validation
# ---------------------------------------------------------------------------
def extract_yaml(text: str) -> str:
    """Strip markdown fences and trim whitespace from LLM output."""
    # Remove ```yaml ... ``` or ``` ... ``` blocks
    text = re.sub(r"```[a-z]*\n?", "", text)
    text = re.sub(r"```", "", text)
    return text.strip()


def find_entities(yaml_text: str) -> list[str]:
    """Extract all entity_id values appearing in the YAML text."""
    # Match domain.entity patterns; case-insensitive to handle any casing edge cases
    pattern = re.compile(r"\b([a-zA-Z_]+\.[a-zA-Z0-9_]+)\b", re.IGNORECASE)
    return list(set(pattern.findall(yaml_text)))


def validate_dashboard(yaml_text: str, known_entities: set) -> dict:
    """
    Parse YAML and check that all referenced entity_ids exist.
    Returns {"valid": bool, "errors": [str]}.
    """
    errors = []

    # 1. YAML parse check
    try:
        parsed = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        return {"valid": False, "errors": [f"YAML parse error: {exc}"]}

    if not isinstance(parsed, dict):
        errors.append("Root YAML must be a mapping (dict), got: " + type(parsed).__name__)

    # 2. Entity existence check
    found = find_entities(yaml_text)
    for eid in found:
        if eid not in known_entities:
            errors.append(f"Unknown entity_id: {eid}")

    return {"valid": len(errors) == 0, "errors": errors}


# ---------------------------------------------------------------------------
# Storage helpers
# ---------------------------------------------------------------------------
def store_attempt(source: str, yaml_text: str, validation: dict):
    """Write LLM output and its validation result to generated/."""
    GEN_DIR.mkdir(parents=True, exist_ok=True)
    yaml_path = GEN_DIR / f"latest_{source}.yaml"
    val_path = GEN_DIR / f"latest_{source}_validation.json"
    yaml_path.write_text(yaml_text)
    val_path.write_text(json.dumps(validation, indent=2))
    log(f"Stored {source} attempt → {yaml_path}")


def save_success(yaml_text: str, source: str):
    """Save validated dashboard to approved/."""
    APPROVED_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = APPROVED_DIR / f"dashboard_{ts}_{source}.yaml"
    out.write_text(yaml_text)
    log(f"Dashboard approved → {out}")
    return out


def save_failure(yaml_text: str, validation: dict, source: str):
    """Save failed attempt to failed/ and record lesson."""
    FAILED_DIR.mkdir(parents=True, exist_ok=True)
    KNOWLEDGE_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = FAILED_DIR / f"dashboard_failed_{ts}_{source}.yaml"
    out.write_text(yaml_text or "")
    log(f"Dashboard failed → {out}")

    # Record lessons
    for error in validation.get("errors", []):
        lesson = f"[{source}] {error}"
        append_jsonl(
            KNOWLEDGE_DIR / "lessons_learned.jsonl",
            {"timestamp": _now_iso(), "source": source, "lesson": lesson},
        )

    # Record repair pair for future fine-tuning
    append_jsonl(
        KNOWLEDGE_DIR / "repair_pairs.jsonl",
        {
            "timestamp": _now_iso(),
            "source": source,
            "yaml": yaml_text or "",
            "errors": validation.get("errors", []),
        },
    )


# ---------------------------------------------------------------------------
# Git (optional, local only)
# ---------------------------------------------------------------------------
def maybe_git_commit(options: dict):
    if not options.get("git_auto_commit", False):
        return
    share = SHARE_DIR
    if options.get("repo_subdir"):
        share = SHARE_DIR / options["repo_subdir"]
    git_dir = share / ".git"
    if not git_dir.exists():
        log("git_auto_commit=true but /share/... is not a git repo — skipping", "warning")
        return
    try:
        msg = f"chore: ai-dashboard-builder run {_now_iso()}"
        subprocess.run(["git", "-C", str(share), "add", "."], check=True)
        subprocess.run(["git", "-C", str(share), "commit", "-m", msg], check=True)
        log(f"Git commit done in {share}")
    except subprocess.CalledProcessError as exc:
        log(f"Git commit failed: {exc}", "warning")


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    setup_logging()
    log("=== AI Dashboard Builder starting ===")

    # Ensure output dirs exist
    for d in (GEN_DIR, APPROVED_DIR, FAILED_DIR, KNOWLEDGE_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)

    # Load add-on options
    options = load_options()
    ollama_url = options.get("ollama_url", "http://localhost:11434")
    ollama_model = options.get("ollama_model", "mistral")
    anthropic_key = options.get("anthropic_api_key", "")
    anthropic_model = options.get("anthropic_model_fast", "claude-haiku-20240307")

    # Supervisor token (injected by HAOS)
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        log("SUPERVISOR_TOKEN not set — cannot reach Home Assistant API", "error")
        sys.exit(1)

    # Fetch HA context
    context = export_ha_context(token)
    states = context.get("states", [])
    if not states:
        log("No states retrieved from HA — aborting", "error")
        sys.exit(1)

    # Analyse entities
    domains = analyze_entities(states)
    all_entity_ids = {eid for ids in domains.values() for eid in ids}

    # Load past lessons
    lessons = load_lessons()

    # --- Ollama generation ---
    log(f"Calling Ollama ({ollama_model}) for dashboard generation...")
    prompt = build_generation_prompt(domains, lessons)
    raw_ollama = call_ollama(prompt, ollama_url, ollama_model)

    if raw_ollama:
        yaml_ollama = extract_yaml(raw_ollama)
        val_ollama = validate_dashboard(yaml_ollama, all_entity_ids)
        store_attempt("ollama", yaml_ollama, val_ollama)
    else:
        yaml_ollama = ""
        val_ollama = {"valid": False, "errors": ["Ollama returned no response"]}
        store_attempt("ollama", "", val_ollama)

    if val_ollama["valid"]:
        log("Ollama dashboard validated successfully.")
        out = save_success(yaml_ollama, "ollama")
        maybe_git_commit(options)
        log(f"=== Done. Dashboard saved to {out} ===")
        return

    # Ollama failed — record and attempt Anthropic fallback
    log(f"Ollama validation failed: {val_ollama['errors']}")
    save_failure(yaml_ollama, val_ollama, "ollama")

    if not anthropic_key:
        log("No Anthropic API key configured — cannot attempt repair. Exiting.", "warning")
        sys.exit(1)

    # --- Anthropic repair ---
    log(f"Calling Anthropic ({anthropic_model}) for YAML repair...")
    repair_prompt = build_repair_prompt(yaml_ollama, val_ollama["errors"], domains, lessons)
    raw_anthropic = call_anthropic(repair_prompt, anthropic_key, anthropic_model)

    if raw_anthropic:
        yaml_anthropic = extract_yaml(raw_anthropic)
        val_anthropic = validate_dashboard(yaml_anthropic, all_entity_ids)
        store_attempt("anthropic", yaml_anthropic, val_anthropic)
    else:
        yaml_anthropic = ""
        val_anthropic = {"valid": False, "errors": ["Anthropic returned no response"]}
        store_attempt("anthropic", "", val_anthropic)

    if val_anthropic["valid"]:
        log("Anthropic repair validated successfully.")
        out = save_success(yaml_anthropic, "anthropic")
        maybe_git_commit(options)
        log(f"=== Done. Dashboard saved to {out} ===")
        return

    # Both failed
    log(f"Anthropic repair also failed: {val_anthropic['errors']}", "error")
    save_failure(yaml_anthropic, val_anthropic, "anthropic")
    log("=== All attempts failed. Check /share/ai_dashboard_builder/failed/ ===", "error")
    sys.exit(1)


if __name__ == "__main__":
    main()
