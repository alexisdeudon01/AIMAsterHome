"""Claude (Anthropic) analyst: builds prompt, calls API, validates JSON response."""
import json
import re
from typing import Any, Dict

import requests

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
TIMEOUT = 180
MAX_TOKENS = 8192

SYSTEM_PROMPT = """\
You are an expert Home Assistant analyst. You receive a snapshot of a running \
Home Assistant instance and produce structured recommendations.

You MUST respond with STRICT valid JSON (no markdown fences, no text outside the JSON) \
that matches this exact schema:

{
  "summary": "<Brief overview of the HA setup>",
  "key_findings": ["<finding>"],
  "new_devices_or_entities": ["<entity_id or device name>"],
  "recommended_dashboards": [
    {
      "title": "<Dashboard title>",
      "description": "<What this dashboard shows>",
      "filename": "<safe_filename.yaml>",
      "yaml": "<Complete Lovelace YAML content>"
    }
  ],
  "recommended_integrations": [
    {
      "name": "<Integration name>",
      "reason": "<Why to add it>",
      "url": "https://www.home-assistant.io/integrations/<slug>"
    }
  ],
  "recommended_addons": [
    {
      "name": "<Add-on display name>",
      "slug": "<supervisor_slug>",
      "reason": "<Why to add it>",
      "url": "<GitHub or HA add-on store URL>"
    }
  ],
  "recommended_hacs": [
    {
      "name": "<HACS repo name>",
      "reason": "<Why to add it>",
      "url": "https://github.com/<owner>/<repo>",
      "category": "<integration|lovelace|theme|automation|appdaemon|python_script>"
    }
  ],
  "execution_plan": [
    {
      "step": 1,
      "action": "<create_file|update_file|call_service>",
      "description": "<Human-readable description>",
      "requires_approval": true,
      "path": "/config/dashboards/<filename>.yaml",
      "content": "<file content when action is create_file or update_file>",
      "rollback": "<How to undo this step>"
    }
  ],
  "questions": ["<Clarifying question for the user>"]
}

Rules:
- Only use entity_ids that appear in the provided entity list. NEVER invent entity_ids.
- Every execution_plan step MUST have requires_approval = true.
- Dashboard YAML must be valid Lovelace storage-mode config (title, views, etc.).
- execution_plan steps should only include create_file / update_file for dashboards.
  Do NOT include addon installation steps in the execution_plan.
- Limit to 3 dashboard proposals max. Keep YAML concise but functional.
- For HACS recommendations, suggest real well-known repos (e.g. custom-cards, HACS integrations).
- Return ONLY the JSON object. No preamble, no markdown, no trailing text.
"""


def call_claude(api_key: str, model: str, user_message: str) -> str:
    """Call Anthropic Messages API and return raw text response."""
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_message}],
    }
    resp = requests.post(ANTHROPIC_URL, headers=headers, json=payload, timeout=TIMEOUT)
    resp.raise_for_status()
    parts = [c["text"] for c in resp.json().get("content", []) if c.get("type") == "text"]
    return "\n".join(parts).strip()


def _extract_json(text: str) -> Dict[str, Any]:
    """Strip optional markdown fences and parse JSON."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\s*```\s*$", "", text, flags=re.MULTILINE)
    return json.loads(text.strip())


def _build_user_prompt(snapshot: Dict[str, Any]) -> str:
    states = snapshot.get("states", [])
    entity_ids = snapshot.get("entities", [])

    # Domain counts
    domain_counts: Dict[str, int] = {}
    for eid in entity_ids:
        domain = eid.split(".")[0]
        domain_counts[domain] = domain_counts.get(domain, 0) + 1

    # State samples: up to 5 per domain
    domain_samples: Dict[str, list] = {}
    for state in states:
        eid = state.get("entity_id", "")
        domain = eid.split(".")[0] if "." in eid else "unknown"
        if domain not in domain_samples:
            domain_samples[domain] = []
        if len(domain_samples[domain]) < 5:
            domain_samples[domain].append(
                {
                    "entity_id": eid,
                    "state": state.get("state"),
                    "friendly_name": state.get("attributes", {}).get("friendly_name"),
                    "device_class": state.get("attributes", {}).get("device_class"),
                }
            )

    addons = snapshot.get("addons", [])
    addon_list = [
        {"name": a.get("name"), "slug": a.get("slug"), "state": a.get("state"), "version": a.get("version")}
        for a in addons[:40]
    ]

    areas = [a.get("name") for a in snapshot.get("areas", [])]

    config = snapshot.get("config", {})
    new_entities = snapshot.get("new_entities", [])
    new_devices = snapshot.get("new_devices", [])
    logs = snapshot.get("logs", {})

    # Truncate logs to keep prompt manageable
    log_text = ""
    if logs:
        for src, content in logs.items():
            log_text += f"\n### {src} logs (last lines)\n{content[-2000:]}\n"
    else:
        log_text = "Log collection disabled."

    return f"""\
Analyze this Home Assistant setup and return recommendations as strict JSON.

## HA Core Information
- Location: {config.get("location_name", "Unknown")}
- HA Version: {config.get("version", "Unknown")}
- Unit system: {config.get("unit_system", {}).get("length", "unknown")}
- Total entities: {len(entity_ids)}
- Areas: {json.dumps(areas)}
- Total devices: {len(snapshot.get("devices", []))}

## Entity Counts by Domain
{json.dumps(domain_counts, indent=2)}

## Entity Samples by Domain (up to 5 per domain)
{json.dumps(domain_samples, indent=2)}

## Complete entity_id List (USE ONLY THESE)
{json.dumps(sorted(entity_ids), indent=2)}

## Installed Supervisor Add-ons ({len(addons)} total)
{json.dumps(addon_list, indent=2)}

## New Entities Since Last Run ({len(new_entities)} detected)
{json.dumps(new_entities, indent=2)}

## New Devices Since Last Run ({len(new_devices)} detected)
{json.dumps(new_devices, indent=2)}

## Recent Logs
{log_text}

## Instructions
1. Write a brief summary of this HA setup.
2. List key findings: issues, missing integrations, improvement opportunities.
3. Propose up to 3 Lovelace dashboards (each as a complete YAML, only using entity_ids from the list above).
4. Recommend core HA integrations not yet installed but relevant to detected devices/domains.
5. Recommend useful Supervisor add-ons (not already installed).
6. Recommend HACS repos (custom components or Lovelace cards) that would improve this setup.
7. Build an execution_plan with create_file steps for each dashboard proposal.
8. Ask any clarifying questions if you need more context.

Remember: return ONLY the JSON object. No markdown, no preamble.
"""


def run_analysis(snapshot: Dict[str, Any], options: Dict[str, Any]) -> Dict[str, Any]:
    """Run Claude analysis and return parsed proposal dict."""
    api_key = options.get("anthropic_api_key", "")
    model = options.get("anthropic_model", "claude-3-5-sonnet-latest")

    if not api_key:
        return {
            "error": "No Anthropic API key configured",
            "summary": "Configuration error: please set anthropic_api_key in add-on options.",
            "key_findings": ["anthropic_api_key is not set"],
            "new_devices_or_entities": [],
            "recommended_dashboards": [],
            "recommended_integrations": [],
            "recommended_addons": [],
            "recommended_hacs": [],
            "execution_plan": [],
            "questions": [],
        }

    prompt = _build_user_prompt(snapshot)

    raw = ""
    try:
        raw = call_claude(api_key, model, prompt)
        result = _extract_json(raw)
        # Enforce requires_approval on all plan steps
        for step in result.get("execution_plan", []):
            step["requires_approval"] = True
        # Cap dashboards at 3 as specified in the system prompt
        if len(result.get("recommended_dashboards", [])) > 3:
            result["recommended_dashboards"] = result["recommended_dashboards"][:3]
        result["_model_used"] = model
        return result
    except json.JSONDecodeError as exc:
        return {
            "error": f"JSON parse error: {exc}",
            "raw_response": raw[:3000],
            "summary": "Analysis failed – Claude returned invalid JSON.",
            "key_findings": [],
            "new_devices_or_entities": [],
            "recommended_dashboards": [],
            "recommended_integrations": [],
            "recommended_addons": [],
            "recommended_hacs": [],
            "execution_plan": [],
            "questions": [],
        }
    except Exception as exc:
        return {
            "error": str(exc),
            "summary": "Analysis failed.",
            "key_findings": [],
            "new_devices_or_entities": [],
            "recommended_dashboards": [],
            "recommended_integrations": [],
            "recommended_addons": [],
            "recommended_hacs": [],
            "execution_plan": [],
            "questions": [],
        }
