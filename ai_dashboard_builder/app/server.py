"""Flask web server: ingress UI + REST API for HA Analyst add-on."""
import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from flask import Flask, jsonify, render_template, request

from claude_analyst import list_anthropic_models, run_analysis
from executor import execute_plan
from ha_collector import collect_ha_snapshot

DATA_DIR = Path("/data")
PROPOSALS_DIR = DATA_DIR / "proposals"
OPTIONS_PATH = DATA_DIR / "options.json"
USER_PREFS_PATH = DATA_DIR / "user_prefs.json"
USAGE_PATH = DATA_DIR / "usage.json"

DEFAULT_MONTHLY_BUDGET = 50.0
MAX_MONTHLY_BUDGET = 200.0

app = Flask(__name__, template_folder="templates")

_analysis_lock = threading.Lock()
_analysis_running = False
_analysis_status: Dict[str, Any] = {"running": False, "message": "Idle"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def load_options() -> Dict[str, Any]:
    defaults: Dict[str, Any] = {
        "anthropic_api_key": "",
        "anthropic_model": "claude-3-5-sonnet-latest",
        "github_token": "",
        "poll_interval_minutes": 0,
        "include_logs": False,
        "logs_max_lines": 100,
        "allow_write_homeassistant_config": False,
        "monthly_budget_usd": DEFAULT_MONTHLY_BUDGET,
    }
    if OPTIONS_PATH.exists():
        try:
            defaults.update(json.loads(OPTIONS_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    # Merge user-editable preferences (model selection, budget) on top
    if USER_PREFS_PATH.exists():
        try:
            defaults.update(json.loads(USER_PREFS_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    return defaults


def _latest_proposal() -> Optional[Dict[str, Any]]:
    files = sorted(PROPOSALS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return None
    try:
        return json.loads(files[0].read_text(encoding="utf-8"))
    except Exception:
        return None


def _load_proposal(proposal_id: str) -> Optional[Dict[str, Any]]:
    path = PROPOSALS_DIR / f"{proposal_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_proposal(proposal: Dict[str, Any]) -> None:
    proposal_id = proposal.get("id", str(uuid.uuid4()))
    path = PROPOSALS_DIR / f"{proposal_id}.json"
    path.write_text(json.dumps(proposal, indent=2, ensure_ascii=False), encoding="utf-8")


def _load_usage() -> Dict[str, Any]:
    if USAGE_PATH.exists():
        try:
            return json.loads(USAGE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"month": "", "current_month_usd": 0.0, "analyses_this_month": 0}


def _record_usage(cost_usd: float) -> None:
    now = datetime.now(timezone.utc)
    month_key = now.strftime("%Y-%m")
    usage = _load_usage()
    if usage.get("month") != month_key:
        usage = {"month": month_key, "current_month_usd": 0.0, "analyses_this_month": 0}
    usage["current_month_usd"] = round(usage.get("current_month_usd", 0.0) + cost_usd, 4)
    usage["analyses_this_month"] = usage.get("analyses_this_month", 0) + 1
    USAGE_PATH.write_text(json.dumps(usage, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Background analysis worker
# ---------------------------------------------------------------------------

def _run_analysis_background() -> None:
    global _analysis_running, _analysis_status
    try:
        options = load_options()

        # Budget guard: refuse if this month's spend already exceeds the cap
        monthly_budget = float(options.get("monthly_budget_usd", DEFAULT_MONTHLY_BUDGET))
        usage = _load_usage()
        now_month = datetime.now(timezone.utc).strftime("%Y-%m")
        spent = usage.get("current_month_usd", 0.0) if usage.get("month") == now_month else 0.0
        if monthly_budget > 0 and spent >= monthly_budget:
            _analysis_status = {
                "running": False,
                "message": (
                    f"Monthly budget cap of ${monthly_budget:.2f} reached "
                    f"(spent ${spent:.2f} this month). Increase the budget to continue."
                ),
            }
            return

        _analysis_status = {"running": True, "message": "Collecting HA context…"}
        snapshot = collect_ha_snapshot(options)

        _analysis_status["message"] = "Calling Claude for analysis…"
        proposal = run_analysis(snapshot, options)

        # Record cost for budget tracking
        cost_usd = proposal.pop("_cost_usd", 0.0)
        if cost_usd > 0:
            _record_usage(cost_usd)

        proposal_id = str(uuid.uuid4())
        proposal["id"] = proposal_id
        proposal["status"] = "pending"
        proposal["timestamp"] = datetime.now(timezone.utc).isoformat()
        proposal["snapshot_summary"] = {
            "entities": len(snapshot.get("entities", [])),
            "new_entities": len(snapshot.get("new_entities", [])),
            "new_devices": len(snapshot.get("new_devices", [])),
            "addons": len(snapshot.get("addons", [])),
            "errors": snapshot.get("errors", []),
        }
        _save_proposal(proposal)
        _analysis_status = {"running": False, "message": f"Done. Proposal {proposal_id} ready."}
    except Exception as exc:
        _analysis_status = {"running": False, "message": f"Error: {exc}"}
    finally:
        with _analysis_lock:
            _analysis_running = False


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/status")
def status():
    return jsonify(
        {
            "analysis": _analysis_status,
            "proposals_count": len(list(PROPOSALS_DIR.glob("*.json"))),
        }
    )


@app.route("/proposal")
@app.route("/proposal/latest")
def get_proposal():
    """Return the latest proposal."""
    proposal = _latest_proposal()
    if not proposal:
        return jsonify({"error": "No proposals found"}), 404
    return jsonify(proposal)


@app.route("/proposal/<proposal_id>")
def get_proposal_by_id(proposal_id: str):
    """Return a specific proposal by ID."""
    proposal = _load_proposal(proposal_id)
    if not proposal:
        return jsonify({"error": "Proposal not found"}), 404
    return jsonify(proposal)


@app.route("/proposals")
def list_proposals():
    """Return a list of all proposals (summary only)."""
    items = []
    for f in sorted(PROPOSALS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:20]:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            items.append(
                {
                    "id": data.get("id"),
                    "timestamp": data.get("timestamp"),
                    "status": data.get("status"),
                    "summary": (data.get("summary") or "")[:200],
                    "has_error": bool(data.get("error")),
                }
            )
        except Exception:
            pass
    return jsonify(items)


@app.route("/generate", methods=["POST"])
def generate():
    """Trigger a new analysis in the background."""
    global _analysis_running
    with _analysis_lock:
        if _analysis_running:
            return jsonify({"error": "Analysis already running"}), 409
        _analysis_running = True

    thread = threading.Thread(target=_run_analysis_background, daemon=True)
    thread.start()
    return jsonify({"message": "Analysis started"}), 202


@app.route("/approve", methods=["POST"])
def approve():
    """Apply an approved execution plan."""
    body = request.get_json(silent=True) or {}
    proposal_id = body.get("proposal_id")
    if not proposal_id:
        return jsonify({"error": "proposal_id is required"}), 400

    proposal = _load_proposal(proposal_id)
    if not proposal:
        return jsonify({"error": "Proposal not found"}), 404
    if proposal.get("status") == "applied":
        return jsonify({"error": "Proposal already applied"}), 400

    options = load_options()
    results = execute_plan(proposal, options)

    proposal["status"] = "applied"
    proposal["applied_at"] = datetime.now(timezone.utc).isoformat()
    proposal["execution_result"] = results
    _save_proposal(proposal)

    return jsonify({"message": "Execution plan applied", "results": results})


@app.route("/diff")
def diff():
    """Preview the execution plan for a proposal (defaults to latest)."""
    proposal_id = request.args.get("proposal_id")
    if proposal_id:
        proposal = _load_proposal(proposal_id)
    else:
        proposal = _latest_proposal()

    if not proposal:
        return jsonify({"error": "No proposal found"}), 404

    plan = proposal.get("execution_plan", [])
    preview = []
    for step in plan:
        preview.append(
            {
                "step": step.get("step"),
                "action": step.get("action"),
                "description": step.get("description"),
                "path": step.get("path"),
                "requires_approval": step.get("requires_approval", True),
                "rollback": step.get("rollback"),
                "content_preview": (step.get("content") or "")[:500],
            }
        )
    return jsonify(
        {
            "proposal_id": proposal.get("id"),
            "status": proposal.get("status"),
            "plan": preview,
        }
    )


@app.route("/models")
def list_models():
    """Fetch available Claude models from the Anthropic API."""
    options = load_options()
    api_key = options.get("anthropic_api_key", "")
    if not api_key:
        return jsonify({"error": "No API key configured"}), 400
    try:
        models = list_anthropic_models(api_key)
        return jsonify({"models": models})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/settings", methods=["GET"])
def get_settings():
    """Return current non-sensitive options."""
    options = load_options()
    return jsonify({
        "has_api_key": bool(options.get("anthropic_api_key", "")),
        "anthropic_model": options.get("anthropic_model", ""),
        "poll_interval_minutes": options.get("poll_interval_minutes", 0),
        "monthly_budget_usd": options.get("monthly_budget_usd", DEFAULT_MONTHLY_BUDGET),
        "include_logs": options.get("include_logs", False),
        "allow_write_homeassistant_config": options.get("allow_write_homeassistant_config", False),
    })


@app.route("/settings", methods=["POST"])
def update_settings():
    """Update UI-editable settings (model, budget). Stored in /data/user_prefs.json."""
    body = request.get_json(silent=True) or {}
    prefs: Dict[str, Any] = {}
    if USER_PREFS_PATH.exists():
        try:
            prefs = json.loads(USER_PREFS_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass

    if "anthropic_model" in body:
        prefs["anthropic_model"] = str(body["anthropic_model"])
    if "monthly_budget_usd" in body:
        budget = float(body["monthly_budget_usd"])
        prefs["monthly_budget_usd"] = round(min(max(budget, 0.0), MAX_MONTHLY_BUDGET), 4)
    if "poll_interval_minutes" in body:
        prefs["poll_interval_minutes"] = int(body["poll_interval_minutes"])

    USER_PREFS_PATH.write_text(json.dumps(prefs, indent=2, ensure_ascii=False), encoding="utf-8")
    # Return merged (non-sensitive) view
    merged = load_options()
    return jsonify({
        "message": "Settings updated",
        "anthropic_model": merged.get("anthropic_model"),
        "monthly_budget_usd": merged.get("monthly_budget_usd"),
        "poll_interval_minutes": merged.get("poll_interval_minutes"),
    })


@app.route("/budget")
def budget_status():
    """Return current month's estimated spending vs. the configured cap."""
    usage = _load_usage()
    options = load_options()
    monthly_budget = float(options.get("monthly_budget_usd", DEFAULT_MONTHLY_BUDGET))
    now_month = datetime.now(timezone.utc).strftime("%Y-%m")
    spent = usage.get("current_month_usd", 0.0) if usage.get("month") == now_month else 0.0
    return jsonify({
        "month": now_month,
        "current_month_usd": spent,
        "monthly_budget_usd": monthly_budget,
        "remaining_usd": round(max(0.0, monthly_budget - spent), 4),
        "analyses_this_month": usage.get("analyses_this_month", 0) if usage.get("month") == now_month else 0,
    })


# ---------------------------------------------------------------------------
# Auto-poll scheduler
# ---------------------------------------------------------------------------

def _start_auto_poll(interval_minutes: int) -> None:
    """Start a background thread that triggers analysis every N minutes."""
    import time

    def _scheduler() -> None:
        while True:
            time.sleep(interval_minutes * 60)
            global _analysis_running
            with _analysis_lock:
                if _analysis_running:
                    continue
                _analysis_running = True
            _run_analysis_background()

    thread = threading.Thread(target=_scheduler, daemon=True)
    thread.start()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    PROPOSALS_DIR.mkdir(parents=True, exist_ok=True)
    opts = load_options()
    poll_interval = int(opts.get("poll_interval_minutes", 0))
    if poll_interval > 0:
        _start_auto_poll(poll_interval)
    port = int(os.environ.get("INGRESS_PORT", 8099))
    app.run(host="0.0.0.0", port=port, threaded=True)
