import datetime as dt
import json
import os
import re
import subprocess
from pathlib import Path

import requests
import yaml

OPTIONS_PATH = Path("/data/options.json")
SAFE_BASE = Path("/share/ai_dashboard_builder")
TIMEOUT = 30


def safe_output_base(raw_path):
    candidate = Path(raw_path or SAFE_BASE)
    if not candidate.is_absolute():
        candidate = SAFE_BASE / candidate
    try:
        resolved = candidate.resolve()
        safe_resolved = SAFE_BASE.resolve()
        resolved.relative_to(safe_resolved)
        return resolved
    except Exception:
        return SAFE_BASE


def load_options():
    defaults = {
        "ollama_url": "http://homeassistant.local:11434/api/generate",
        "ollama_model": "llama3.1",
        "anthropic_api_key": "",
        "anthropic_model_fast": "claude-3-5-haiku-latest",
        "repo_subdir": str(SAFE_BASE),
        "git_auto_commit": False,
        "dashboard_profile": "mobile",
        "generation_mode": "once",
        "include_existing_dashboards_analysis": True,
        "include_supervisor_analysis": True,
    }
    if OPTIONS_PATH.exists():
        try:
            defaults.update(json.loads(OPTIONS_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    defaults["repo_subdir"] = str(safe_output_base(defaults.get("repo_subdir")))
    if defaults.get("dashboard_profile") not in {"mobile", "tablet", "desktop"}:
        defaults["dashboard_profile"] = "mobile"
    if defaults.get("generation_mode") != "once":
        defaults["generation_mode"] = "once"
    return defaults


def log(message, log_file):
    timestamp = dt.datetime.utcnow().isoformat()
    line = f"[{timestamp}] {message}"
    print(line, flush=True)
    with log_file.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def append_jsonl(path, payload):
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _extract_data(payload):
    if isinstance(payload, dict) and "data" in payload and payload.get("result") in {"ok", True}:
        return payload["data"]
    return payload


def ha_get(path):
    token = os.getenv("SUPERVISOR_TOKEN", "")
    base = os.getenv("SUPERVISOR_URL", "http://supervisor")
    if not token:
        raise RuntimeError("SUPERVISOR_TOKEN missing")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    response = requests.get(f"{base}/core{path}", headers=headers, timeout=TIMEOUT)
    response.raise_for_status()
    return _extract_data(response.json())


def supervisor_get(path):
    token = os.getenv("SUPERVISOR_TOKEN", "")
    base = os.getenv("SUPERVISOR_URL", "http://supervisor")
    if not token:
        raise RuntimeError("SUPERVISOR_TOKEN missing")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    response = requests.get(f"{base}{path}", headers=headers, timeout=TIMEOUT)
    response.raise_for_status()
    return _extract_data(response.json())


def export_ha_context(generated_dir, log_file):
    context = {}
    for path in ["/api/config", "/api/states", "/api/services", "/api/lovelace/config"]:
        try:
            context[path] = ha_get(path)
        except Exception as exc:
            context[path] = {"error": str(exc)}
            log(f"HA fetch failed for {path}: {exc}", log_file)
    (generated_dir / "ha_context.json").write_text(json.dumps(context, indent=2, ensure_ascii=False), encoding="utf-8")
    return context


def export_existing_dashboards_readonly(generated_dir, log_file):
    snapshot = {"api": None, "files": []}
    try:
        snapshot["api"] = ha_get("/api/lovelace/config")
    except Exception as exc:
        snapshot["api"] = {"error": str(exc)}
        log(f"Existing Lovelace API read failed: {exc}", log_file)

    config_dir = Path("/config")
    candidates = []
    for pattern in ["*lovelace*.yaml", "dashboards/*.yaml", "dashboards/**/*.yaml", "ui-lovelace*.yaml"]:
        candidates.extend(config_dir.glob(pattern))

    for item in sorted({p for p in candidates if p.is_file()})[:10]:
        try:
            snapshot["files"].append({"path": str(item), "preview": item.read_text(encoding="utf-8")[:5000]})
        except Exception as exc:
            log(f"Readonly dashboard file read failed {item}: {exc}", log_file)

    (generated_dir / "existing_dashboards_snapshot.json").write_text(
        json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return snapshot


def export_supervisor_context(generated_dir, log_file):
    context = {}
    for path in ["/supervisor/info", "/addons", "/host/info"]:
        try:
            context[path] = supervisor_get(path)
        except Exception as exc:
            context[path] = {"error": str(exc)}
            log(f"Supervisor fetch failed for {path}: {exc}", log_file)
    (generated_dir / "supervisor_context.json").write_text(json.dumps(context, indent=2, ensure_ascii=False), encoding="utf-8")
    return context


def analyze_entities(states):
    entity_ids = sorted(s.get("entity_id") for s in states if isinstance(s, dict) and s.get("entity_id"))
    by_domain = {}
    for entity_id in entity_ids:
        by_domain[entity_id.split(".", 1)[0]] = by_domain.get(entity_id.split(".", 1)[0], 0) + 1

    def prefixed(prefixes):
        return [entity_id for entity_id in entity_ids if any(entity_id.startswith(f"{prefix}.") for prefix in prefixes)]

    return {
        "total": len(entity_ids),
        "entity_ids": entity_ids,
        "counts_by_domain": dict(sorted(by_domain.items())),
        "lights": prefixed(["light"]),
        "switches": prefixed(["switch", "input_boolean"]),
        "sensors": prefixed(["sensor", "binary_sensor"]),
        "presence": prefixed(["person", "device_tracker", "zone"]),
        "media": prefixed(["media_player", "camera"]),
    }


def load_lessons(knowledge_dir):
    lessons_path = knowledge_dir / "lessons_learned.jsonl"
    if not lessons_path.exists():
        return []
    lessons = []
    for line in lessons_path.read_text(encoding="utf-8").splitlines()[-20:]:
        try:
            lessons.append(json.loads(line))
        except Exception:
            continue
    return lessons


def build_profile_rules(profile):
    rules = {
        "mobile": "Use few columns, compact cards, short sections, phone readability first.",
        "tablet": "Use medium density layout with grouped sections and moderate detail.",
        "desktop": "Use denser layout with richer sections and more visible info.",
    }
    return rules.get(profile, rules["mobile"])


def build_existing_dashboard_context(snapshot):
    if not snapshot:
        return "No existing dashboard snapshot."
    file_paths = [item.get("path", "") for item in snapshot.get("files", [])]
    api_keys = list(snapshot.get("api", {}).keys()) if isinstance(snapshot.get("api"), dict) else []
    return f"Existing dashboard api_keys={api_keys}, yaml_files={len(file_paths)}, file_paths={file_paths}."


def build_supervisor_context_summary(context):
    if not context:
        return "No supervisor context."
    addon_count = len(context.get("/addons", {}).get("addons", [])) if isinstance(context.get("/addons"), dict) else 0
    return f"Supervisor context available. Add-ons detected: {addon_count}."


def build_generation_prompt(entity_analysis, lessons, profile, existing_summary, supervisor_summary):
    return (
        "Generate a Home Assistant Lovelace YAML dashboard.\n"
        "Output only YAML. No markdown fences. No explanations.\n"
        "Do not invent entity_id.\n"
        f"Allowed entities: {json.dumps(entity_analysis.get('entity_ids', []))}\n"
        f"Profile rules: {build_profile_rules(profile)}\n"
        f"Existing dashboards summary: {existing_summary}\n"
        f"Supervisor/add-ons summary: {supervisor_summary}\n"
        f"Recent lessons: {json.dumps(lessons[-5:], ensure_ascii=False)}\n"
        "Keep compatibility and simplicity as top priority."
    )


def call_ollama(url, model, prompt):
    response = requests.post(url, json={"model": model, "prompt": prompt, "stream": False}, timeout=120)
    response.raise_for_status()
    return response.json().get("response", "").strip()


def call_anthropic(api_key, model, prompt):
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
    response = requests.post("https://api.anthropic.com/v1/messages", headers=headers, json=payload, timeout=120)
    response.raise_for_status()
    parts = [chunk.get("text", "") for chunk in response.json().get("content", []) if chunk.get("type") == "text"]
    return "\n".join(parts).strip()


def extract_yaml(text):
    return text.strip().replace("```yaml", "").replace("```", "").strip()


def find_entities(text):
    return sorted(set(re.findall(r"\b[a-z_]+\.[a-zA-Z0-9_]+\b", text)))


def validate_dashboard(yaml_text, allowed_entities):
    errors = []
    try:
        parsed = yaml.safe_load(yaml_text)
        if not isinstance(parsed, (dict, list)):
            errors.append("YAML root must be dict or list")
    except Exception as exc:
        errors.append(f"YAML parse error: {exc}")
    found = find_entities(yaml_text)
    unknown = [entity_id for entity_id in found if entity_id not in allowed_entities]
    if unknown:
        errors.append(f"Unknown entity_id: {', '.join(unknown[:50])}")
    return {"valid": not errors, "errors": errors, "found_entities": found}


def store_attempt(generated_dir, source_name, yaml_text, validation):
    (generated_dir / f"latest_{source_name}.yaml").write_text(yaml_text, encoding="utf-8")
    (generated_dir / f"latest_{source_name}_validation.json").write_text(
        json.dumps(validation, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def build_repair_prompt(bad_yaml, errors, allowed_entities, lessons, profile):
    return (
        "Repair this Home Assistant Lovelace YAML.\n"
        "Return only corrected YAML. No markdown. No explanations.\n"
        f"Current profile rules: {build_profile_rules(profile)}\n"
        f"Exact errors: {json.dumps(errors, ensure_ascii=False)}\n"
        f"Allowed entities only: {json.dumps(allowed_entities)}\n"
        f"Recent lessons: {json.dumps(lessons[-5:], ensure_ascii=False)}\n"
        f"YAML to repair:\n{bad_yaml}"
    )


def save_success(approved_dir, yaml_text):
    path = approved_dir / f"dashboard_{dt.datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    return path


def save_failure(failed_dir, yaml_text):
    path = failed_dir / f"dashboard_failed_{dt.datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.yaml"
    path.write_text(yaml_text, encoding="utf-8")
    return path


def maybe_git_commit(base_dir, log_file):
    if not (base_dir / ".git").exists():
        log(f"Skip git commit: {base_dir} is not a git repository", log_file)
        return
    subprocess.run(["git", "-C", str(base_dir), "add", "."], check=False)
    if subprocess.run(["git", "-C", str(base_dir), "diff", "--cached", "--quiet"], check=False).returncode == 0:
        log("Skip git commit: no staged changes", log_file)
        return
    subprocess.run(
        ["git", "-C", str(base_dir), "commit", "-m", "chore: store AI dashboard builder generated output"],
        check=False,
    )


def main():
    options = load_options()
    base_dir = Path(options["repo_subdir"])
    generated_dir = base_dir / "generated"
    approved_dir = base_dir / "approved"
    failed_dir = base_dir / "failed"
    knowledge_dir = base_dir / "knowledge"
    logs_dir = base_dir / "logs"

    for directory in [generated_dir, approved_dir, failed_dir, knowledge_dir, logs_dir]:
        directory.mkdir(parents=True, exist_ok=True)

    log_file = logs_dir / "run.log"
    log("Starting AI dashboard generation", log_file)

    ha_context = export_ha_context(generated_dir, log_file)
    states = ha_context.get("/api/states", []) if isinstance(ha_context.get("/api/states"), list) else []
    entity_analysis = analyze_entities(states)
    (generated_dir / "entity_analysis.json").write_text(
        json.dumps(entity_analysis, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    existing_snapshot = None
    if options.get("include_existing_dashboards_analysis", True):
        existing_snapshot = export_existing_dashboards_readonly(generated_dir, log_file)

    supervisor_context = None
    if options.get("include_supervisor_analysis", True):
        supervisor_context = export_supervisor_context(generated_dir, log_file)

    lessons = load_lessons(knowledge_dir)
    prompt = build_generation_prompt(
        entity_analysis,
        lessons,
        options.get("dashboard_profile", "mobile"),
        build_existing_dashboard_context(existing_snapshot),
        build_supervisor_context_summary(supervisor_context),
    )

    ollama_yaml = ""
    try:
        ollama_yaml = extract_yaml(call_ollama(options["ollama_url"], options["ollama_model"], prompt))
    except Exception as exc:
        log(f"Ollama generation failed: {exc}", log_file)

    allowed_entities = set(entity_analysis.get("entity_ids", []))
    validation = (
        validate_dashboard(ollama_yaml, allowed_entities)
        if ollama_yaml
        else {"valid": False, "errors": ["No YAML generated from Ollama"], "found_entities": []}
    )
    store_attempt(generated_dir, "ollama", ollama_yaml, validation)

    final_yaml = ollama_yaml
    final_validation = validation
    source = "ollama"

    if not validation["valid"] and options.get("anthropic_api_key"):
        try:
            repair_prompt = build_repair_prompt(
                ollama_yaml,
                validation["errors"],
                entity_analysis.get("entity_ids", []),
                lessons,
                options.get("dashboard_profile", "mobile"),
            )
            anthropic_yaml = extract_yaml(
                call_anthropic(options["anthropic_api_key"], options["anthropic_model_fast"], repair_prompt)
            )
            anthropic_validation = validate_dashboard(anthropic_yaml, allowed_entities)
            store_attempt(generated_dir, "anthropic", anthropic_yaml, anthropic_validation)
            final_yaml = anthropic_yaml
            final_validation = anthropic_validation
            source = "anthropic"
            append_jsonl(
                knowledge_dir / "repair_pairs.jsonl",
                {
                    "timestamp": dt.datetime.utcnow().isoformat(),
                    "before_errors": validation["errors"],
                    "after_valid": anthropic_validation["valid"],
                },
            )
        except Exception as exc:
            log(f"Anthropic fallback failed: {exc}", log_file)

    if final_validation["valid"]:
        output_path = save_success(approved_dir, final_yaml)
        log(f"Dashboard approved from {source}: {output_path}", log_file)
        append_jsonl(
            knowledge_dir / "lessons_learned.jsonl",
            {
                "timestamp": dt.datetime.utcnow().isoformat(),
                "result": "success",
                "source": source,
                "message": "Generated valid dashboard YAML",
            },
        )
    else:
        output_path = save_failure(failed_dir, final_yaml or ollama_yaml)
        log(f"Dashboard generation failed: {output_path}", log_file)
        append_jsonl(
            knowledge_dir / "lessons_learned.jsonl",
            {
                "timestamp": dt.datetime.utcnow().isoformat(),
                "result": "failure",
                "errors": final_validation["errors"],
                "message": "Validation failed",
            },
        )

    if options.get("git_auto_commit"):
        maybe_git_commit(base_dir, log_file)


if __name__ == "__main__":
    main()
