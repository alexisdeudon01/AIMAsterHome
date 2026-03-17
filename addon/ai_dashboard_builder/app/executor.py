"""Execution engine: apply approved execution plan steps to /config."""
import os
from pathlib import Path
from typing import Any, Dict, List

import requests

TIMEOUT = 30
CONFIG_DIR = Path("/config")


def _ha_headers() -> Dict[str, str]:
    token = os.getenv("SUPERVISOR_TOKEN", "")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _safe_config_path(path_str: str) -> Path:
    """Resolve path and ensure it stays within /config."""
    candidate = Path(path_str)
    if not candidate.is_absolute():
        candidate = CONFIG_DIR / candidate
    try:
        resolved = candidate.resolve()
        config_resolved = CONFIG_DIR.resolve()
        resolved.relative_to(config_resolved)
        return resolved
    except ValueError:
        raise ValueError(f"Path escapes /config: {path_str}")


def _execute_step(step: Dict[str, Any], ha_url: str) -> Dict[str, Any]:
    action = step.get("action", "")
    result: Dict[str, Any] = {
        "step": step.get("step"),
        "action": action,
        "success": False,
        "message": "",
    }

    try:
        if action in ("create_file", "update_file"):
            path = _safe_config_path(step.get("path", ""))
            content = step.get("content", "")
            path.parent.mkdir(parents=True, exist_ok=True)

            rollback_content = None
            if path.exists():
                rollback_content = path.read_text(encoding="utf-8")

            path.write_text(content, encoding="utf-8")
            result["success"] = True
            result["message"] = f"Written: {path}"
            if rollback_content is not None:
                result["rollback_content"] = rollback_content

        elif action == "call_service":
            domain = step.get("domain", "")
            service = step.get("service", "")
            data = step.get("data", {})
            resp = requests.post(
                f"{ha_url}/api/services/{domain}/{service}",
                headers=_ha_headers(),
                json=data,
                timeout=TIMEOUT,
            )
            resp.raise_for_status()
            result["success"] = True
            result["message"] = f"Service called: {domain}.{service}"

        else:
            result["success"] = True
            result["message"] = f"Skipped unknown action: {action}"

    except Exception as exc:
        result["message"] = str(exc)

    return result


def execute_plan(proposal: Dict[str, Any], options: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Execute all steps in the proposal's execution_plan after approval."""
    ha_url = options.get("ha_url", "http://supervisor/core")
    plan = proposal.get("execution_plan", [])
    results: List[Dict[str, Any]] = []

    for step in plan:
        # Skip any step that does not require approval (safety guard; normally all steps do)
        if not step.get("requires_approval", True):
            continue
        result = _execute_step(step, ha_url)
        results.append(result)
        if not result["success"]:
            # Stop on first failure to avoid cascading issues
            break

    return results
