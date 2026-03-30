# AIMAsterHome

Home Assistant OS add-on repository containing `ai_dashboard_builder` – a Claude-powered **HA Analyst + Dashboard & Integration Recommender** that proposes Lovelace dashboards, core integrations, Supervisor add-ons, and HACS repos from your real HA context, with an approval-gated execution plan.

## Streamlit import troubleshooting

If you run a Streamlit app and see:

`ModuleNotFoundError: No module named 'dashboard'`

it usually means Python is starting from the `dashboard/` folder instead of the repository root, so absolute imports like `from dashboard.pages...` cannot resolve.

Typical fixes:

1. Run from the project root:
   - `streamlit run dashboard/app.py`
2. Ensure the package root is importable:
   - `export PYTHONPATH="$(pwd):$PYTHONPATH"`
3. If needed, switch imports in `dashboard/app.py` from absolute package imports to local imports that match your runtime layout.
