# AIMAsterHome

Home Assistant OS add-on repository — **AI Dashboard Builder**.

## Add-on: AI Dashboard Builder

Automatically generates Lovelace dashboards by analysing your real HA entities and calling a local Ollama LLM. Falls back to Anthropic API if Ollama output is invalid.

### Features

- Reads live entity state from Home Assistant via Supervisor API
- Generates mobile-first Lovelace YAML (no invented entity_ids)
- Validates YAML structure and entity existence before saving
- Anthropic API repair fallback (optional)
- Stores all artefacts under `/share/ai_dashboard_builder/`
- Never overwrites existing dashboards
- Optional local git commit (no push)

### Installation

1. In Home Assistant go to **Settings → Add-ons → Add-on Store**
2. Click the three-dot menu → **Repositories** → add this repo URL
3. Install **AI Dashboard Builder**
4. Configure the options (Ollama URL, model, optional Anthropic key)
5. Start the add-on

### Configuration

| Option | Description |
|---|---|
| `ollama_url` | Base URL of your Ollama instance (e.g. `http://192.168.1.x:11434`) |
| `ollama_model` | Model to use (e.g. `mistral`, `llama3`) |
| `anthropic_api_key` | *(Optional)* Anthropic key for fallback repair |
| `anthropic_model_fast` | Anthropic model (default `claude-haiku-20240307`) |
| `repo_subdir` | *(Optional)* Subdirectory inside `/share/ai_dashboard_builder` for git |
| `git_auto_commit` | Commit generated files locally (no push) |

### Output structure

```
/share/ai_dashboard_builder/
├── generated/          # Raw LLM outputs + validation results
├── approved/           # Validated dashboards (ready to import)
├── failed/             # Failed attempts
├── knowledge/          # lessons_learned.jsonl, repair_pairs.jsonl
└── logs/               # run.log
```

### Applying a dashboard

After the add-on runs, copy the content of an `approved/dashboard_*.yaml` file
and paste it into **Settings → Dashboards → Edit Dashboard → Raw configuration editor**.
