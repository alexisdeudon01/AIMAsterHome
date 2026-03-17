# AI Dashboard Builder Add-on

This Home Assistant OS add-on generates Lovelace YAML dashboards from local Home Assistant context.

- Uses Ollama first.
- Uses Anthropic only as fallback when configured.
- Never modifies existing dashboards.
- Writes all outputs under `/share/ai_dashboard_builder`.
