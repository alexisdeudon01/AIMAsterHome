"""Collect Home Assistant context: entities, devices, areas, add-ons, logs."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Set

import requests

TIMEOUT = 30
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


def ha_get(path: str, ha_url: str = "http://supervisor/core") -> Any:
    resp = requests.get(f"{ha_url}{path}", headers=_ha_headers(), timeout=TIMEOUT)
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
    """Fetch HA entities, devices, areas, supervisor add-ons and optional logs."""
    ha_url = options.get("ha_url", "http://supervisor/core")
    collect_logs = options.get("collect_logs", False)
    log_lines = int(options.get("log_lines", 100))

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
        "errors": [],
    }

    # States (entities + attributes)
    try:
        states = ha_get("/api/states", ha_url)
        if isinstance(states, list):
            snapshot["states"] = states
            snapshot["entities"] = [s["entity_id"] for s in states if "entity_id" in s]
    except Exception as exc:
        snapshot["errors"].append(f"states: {exc}")

    # Core config
    try:
        snapshot["config"] = ha_get("/api/config", ha_url)
    except Exception as exc:
        snapshot["errors"].append(f"config: {exc}")

    # Area registry (REST endpoint, may not exist on all versions)
    try:
        areas = ha_get("/api/config/area_registry/list", ha_url)
        if isinstance(areas, list):
            snapshot["areas"] = areas
    except Exception as exc:
        snapshot["errors"].append(f"area_registry: {exc}")

    # Device registry
    try:
        devices = ha_get("/api/config/device_registry/list", ha_url)
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
    if collect_logs:
        for log_source in ("core", "supervisor"):
            try:
                resp = requests.get(
                    f"http://supervisor/{log_source}/logs",
                    headers=_ha_headers(),
                    timeout=TIMEOUT,
                )
                if resp.status_code == 200:
                    lines = resp.text.splitlines()[-log_lines:]
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

    return snapshot
