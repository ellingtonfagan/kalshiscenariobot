"""Source registry and readiness checks for autonomous research inputs.

This module is deliberately secret-safe: reports include only whether required
environment variables are present, never their values.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

import requests
import yaml

from ..config import CONFIG_DIR, REPO_ROOT

SOURCE_CONFIG_PATH = CONFIG_DIR / "sources.yaml"


def load_source_config(path: Path | None = None) -> dict[str, Any]:
    """Load the external source registry."""
    source_path = path or SOURCE_CONFIG_PATH
    return yaml.safe_load(source_path.read_text())


def _is_truthy(value: str | None) -> bool:
    return value not in (None, "", "0", "false", "False", "no", "No")


def _resolve_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = (REPO_ROOT / path).resolve()
    return path


def _env_status(required_env: list[str], env: Mapping[str, str]) -> list[dict[str, Any]]:
    status = []
    for name in required_env:
        value = env.get(name, "")
        item: dict[str, Any] = {"name": name, "present": bool(value)}
        if value and name.endswith("_PATH"):
            item["file_exists"] = _resolve_path(value).exists()
        status.append(item)
    return status


def _configured(required_env: list[str], env: Mapping[str, str]) -> bool:
    if not required_env:
        return True
    for item in _env_status(required_env, env):
        if not item["present"]:
            return False
        if "file_exists" in item and not item["file_exists"]:
            return False
    return True


def _probe_provider(provider: dict[str, Any], env: Mapping[str, str]) -> dict[str, Any]:
    probe = provider.get("probe") or {}
    base_url = str(provider.get("base_url") or env.get(provider.get("base_url_env", ""), "")).rstrip("/")
    path = str(probe.get("path") or "")
    if not base_url or not path:
        return {"attempted": False, "ok": None, "reason": "no-probe-configured"}

    params = dict(probe.get("params") or {})
    headers: dict[str, str] = {}
    auth = probe.get("auth") or {}
    auth_env = auth.get("env", "")
    if auth.get("query_param") and auth_env:
        params[auth["query_param"]] = env.get(auth_env, "")
    if auth.get("header") and auth_env:
        headers[auth["header"]] = env.get(auth_env, "")

    try:
        response = requests.request(
            str(probe.get("method") or "GET"),
            base_url + path,
            params=params,
            headers=headers,
            timeout=8,
        )
    except requests.RequestException as exc:
        return {
            "attempted": True,
            "ok": False,
            "reason": exc.__class__.__name__,
        }
    return {
        "attempted": True,
        "ok": response.ok,
        "status_code": response.status_code,
        "reason": "ok" if response.ok else "http-error",
    }


def build_source_report(
    *,
    env: Mapping[str, str] | None = None,
    network: bool | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    """Return provider readiness without leaking configured secrets."""
    env_map = env or os.environ
    network_enabled = _is_truthy(env_map.get("NBABOT_SOURCE_CHECK_NETWORK")) if network is None else network
    config = load_source_config(path)
    providers = []
    for key, provider in sorted(
        config.get("providers", {}).items(),
        key=lambda item: (int(item[1].get("tier", 99)), item[0]),
    ):
        required_env = list(provider.get("required_env") or [])
        configured = _configured(required_env, env_map)
        probe = {"attempted": False, "ok": None, "reason": "network-disabled"}
        if network_enabled and configured:
            probe = _probe_provider(provider, env_map)
        providers.append({
            "key": key,
            "label": provider.get("label", key),
            "tier": provider.get("tier"),
            "role": provider.get("role"),
            "configured": configured,
            "required_env": _env_status(required_env, env_map),
            "allowed_for": list(provider.get("allowed_for") or []),
            "live_execution_allowed": bool(provider.get("live_execution_allowed")),
            "confidence": provider.get("confidence"),
            "docs_url": provider.get("docs_url"),
            "probe": probe,
        })
    return {
        "version": config.get("version"),
        "network_enabled": network_enabled,
        "policy": config.get("policy", {}),
        "providers": providers,
        "sports": config.get("sports", {}),
        "agent_contracts": config.get("agent_contracts", {}),
    }


def format_source_report(report: dict[str, Any]) -> str:
    providers = report.get("providers", [])
    configured = sum(1 for provider in providers if provider.get("configured"))
    missing = len(providers) - configured
    lines = [
        (
            f"[source-check] configured={configured}/{len(providers)} "
            f"missing={missing} network={'on' if report.get('network_enabled') else 'off'}"
        )
    ]
    for provider in providers:
        state = "configured" if provider.get("configured") else "missing"
        probe = provider.get("probe") or {}
        probe_state = ""
        if probe.get("attempted"):
            probe_state = f" probe={'ok' if probe.get('ok') else probe.get('reason', 'failed')}"
        lines.append(
            f"  {provider['key']} tier={provider.get('tier')} "
            f"role={provider.get('role')} {state}{probe_state}"
        )
    if not report.get("policy", {}).get("social_may_trigger_execution", False):
        lines.append("  policy: social/opinion sources are context-only, never execution triggers")
    return "\n".join(lines)
