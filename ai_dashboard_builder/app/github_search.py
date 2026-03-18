"""GitHub REST Search API client for HACS and integration discovery."""
import logging
from typing import Any, Dict, List, Optional

import requests

TIMEOUT = 15
GITHUB_SEARCH_API = "https://api.github.com/search/repositories"

log = logging.getLogger(__name__)


def _search_repos(
    query: str,
    token: Optional[str],
    max_results: int = 5,
) -> List[Dict[str, Any]]:
    """Call the GitHub Search Repositories API and return simplified repo info."""
    headers = {"Accept": "application/vnd.github.v3+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        resp = requests.get(
            GITHUB_SEARCH_API,
            params={
                "q": query,
                "sort": "stars",
                "order": "desc",
                "per_page": max_results,
            },
            headers=headers,
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        items = resp.json().get("items", [])
        return [
            {
                "name": item.get("full_name", ""),
                "description": (item.get("description") or "")[:200],
                "stars": item.get("stargazers_count", 0),
                "url": item.get("html_url", ""),
                "topics": item.get("topics", []),
            }
            for item in items
        ]
    except Exception as exc:
        log.warning("GitHub search failed for query %r: %s", query, exc)
        return []


def discover_hacs_resources(
    new_device_names: List[str],
    domains: List[str],
    token: Optional[str],
) -> Dict[str, Any]:
    """Search GitHub for custom components and Lovelace cards relevant to detected domains/devices.

    Returns a dict with:
      - custom_components: list of repo info dicts
      - lovelace_cards: list of repo info dicts

    Degrades gracefully when no token is provided or on API errors.
    """
    results: Dict[str, Any] = {"custom_components": [], "lovelace_cards": []}

    if not token:
        return results

    # Build domain-specific queries (cap to avoid hitting rate limits)
    interesting_domains = [d for d in domains if d not in ("unknown", "persistent_notification")][:5]

    for domain in interesting_domains:
        hits = _search_repos(
            f"home-assistant custom-component {domain} topic:hacs",
            token,
            max_results=3,
        )
        results["custom_components"].extend(hits)

    # Deduplicate by URL
    seen: set = set()
    unique: List[Dict[str, Any]] = []
    for item in results["custom_components"]:
        if item["url"] not in seen:
            seen.add(item["url"])
            unique.append(item)
    results["custom_components"] = unique[:10]

    # Lovelace cards search
    results["lovelace_cards"] = _search_repos(
        "home-assistant lovelace card custom topic:lovelace",
        token,
        max_results=5,
    )

    return results
