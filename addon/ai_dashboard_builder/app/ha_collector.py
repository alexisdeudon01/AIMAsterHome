"""Collect Home Assistant context: entities, devices, areas, add-ons, logs."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Set

import requests

from github_search import discover_hacs_resources

TIMEOUT = 30
HA_URL = "http://supervisor/core"
KNOWN_FILE = Path("/data/known.json")


def _ha_headers() -> Dict[str, str]:
    token = os.getenv("SUPERVISOR_TOKEN", "")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _extract_data(payload: Any) -> Any:
    if isinstance(payload, dict) and "data" in payload and payload.get("result") in {"ok", True}:
        return payload["data"]
    return payload


def ha_get(path: str) -> Any:
    resp = requests.get(f"{HA_URL}{path}", headers=_ha_headers(), timeout=TIMEOUT)
    resp.raise_for_status()
    return _extract_data(resp.json())


def supervisor_get(path: str) -> Any:
    resp = requests.get(f"http://supervisor{path}", headers=_ha_headers(), timeout=TIMEOUT)
    resp.raise_for_status()
    return _extract_data(resp.json())


def load_known() -> Dict[str, List[str]]:
    if KNOWN_FILE.exists():
        try:
            return json.loads(KNOWN_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"entity_ids": [], "device_ids": []}


def save_known(known: Dict[str, List[str]]) -> None:
    KNOWN_FILE.parent.mkdir(parents=True, exist_ok=True)
    KNOWN_FILE.write_text(json.dumps(known, indent=2, ensure_ascii=False), encoding="utf-8")


def collect_ha_snapshot(options: Dict[str, Any]) -> Dict[str, Any]:
    """Fetch HA entities, devices, areas, supervisor add-ons, optional logs, and GitHub hints."""
    include_logs = options.get("include_logs", False)
    logs_max_lines = int(options.get("logs_max_lines", 100))
    github_token = options.get("github_token") or None

    snapshot: Dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "entities": [],
        "states": [],
        "config": {},
        "areas": [],
        "devices": [],
        "addons": [],
        "logs": {},
        "new_entities": [],
        "new_devices": [],
        "github_hints": {},
        "errors": [],
    }

    # States (entities + attributes)
    try:
        states = ha_get("/api/states")
        if isinstance(states, list):
            snapshot["states"] = states
            snapshot["entities"] = [s["entity_id"] for s in states if "entity_id" in s]
    except Exception as exc:
        snapshot["errors"].append(f"states: {exc}")

    # Core config
    try:
        snapshot["config"] = ha_get("/api/config")
    except Exception as exc:
        snapshot["errors"].append(f"config: {exc}")

    # Area registry
    try:
        areas = ha_get("/api/config/area_registry/list")
        if isinstance(areas, list):
            snapshot["areas"] = areas
    except Exception as exc:
        snapshot["errors"].append(f"area_registry: {exc}")

    # Device registry
    try:
        devices = ha_get("/api/config/device_registry/list")
        if isinstance(devices, list):
            snapshot["devices"] = devices
    except Exception as exc:
        snapshot["errors"].append(f"device_registry: {exc}")

    # Supervisor add-ons
    try:
        addons_data = supervisor_get("/addons")
        snapshot["addons"] = (
            addons_data.get("addons", []) if isinstance(addons_data, dict) else []
        )
    except Exception as exc:
        snapshot["errors"].append(f"addons: {exc}")

    # Optional log collection
    if include_logs:
        for log_source in ("core", "supervisor"):
            try:
                resp = requests.get(
                    f"http://supervisor/{log_source}/logs",
                    headers=_ha_headers(),
                    timeout=TIMEOUT,
                )
                if resp.status_code == 200:
                    lines = resp.text.splitlines()[-logs_max_lines:]
                    snapshot["logs"][log_source] = "\n".join(lines)
            except Exception:
                pass

    # Detect newly added entities / devices
    known = load_known()
    current_entity_ids: Set[str] = set(snapshot["entities"])
    known_entity_ids: Set[str] = set(known.get("entity_ids", []))
    snapshot["new_entities"] = sorted(current_entity_ids - known_entity_ids)

    current_device_ids: Set[str] = {d.get("id", "") for d in snapshot["devices"] if d.get("id")}
    known_device_ids: Set[str] = set(known.get("device_ids", []))
    snapshot["new_devices"] = sorted(current_device_ids - known_device_ids)

    # Persist updated known IDs
    save_known(
        {
            "entity_ids": sorted(current_entity_ids),
            "device_ids": sorted(current_device_ids),
            "updated_at": snapshot["timestamp"],
        }
    )

    # GitHub discovery (optional — degrades gracefully without token)
    domains = sorted({eid.split(".")[0] for eid in snapshot["entities"] if "." in eid})
    new_device_names = [
        d.get("name", d.get("id", ""))
        for d in snapshot["devices"]
        if d.get("id", "") in current_device_ids - known_device_ids
    ]
    try:
        snapshot["github_hints"] = discover_hacs_resources(new_device_names, domains, github_token)
    except Exception as exc:
        snapshot["errors"].append(f"github_search: {exc}")

    return snapshot
