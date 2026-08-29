#!/usr/bin/env python3
"""Build the bounded, read-only Nexus control-plane cache.

Only the five allowlisted central indexes and their deterministic validators are
read.  No request handler invokes this module and no Markdown is rendered as
HTML.  The resulting JSON is an evidence projection, never the policy source.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path("/home/david/nexus")
VAULT_ROOT = Path("/home/david/Vaults")
HOMELAB = VAULT_ROOT / "homelab-vault"
STATE_ROOT = Path(os.environ.get("NEXUS_STATE_DIR")
                  or os.environ.get("PANEL_STATE_DIR")
                  or os.environ.get("FLEET_NEXUS_STATE_DIR")
                  or Path.home() / ".local" / "state" / "nexus")
CACHE = STATE_ROOT / "generated" / "control-plane.json"
INDEX_NAMES = (
    "fleet-index.md",
    "roadmap-index.md",
    "conventions-index.md",
    "instructions-index.md",
    "automation-index.md",
)
MAX_OUTPUT = 2_000_000


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def bounded(value: object, limit: int = 300) -> str:
    return " ".join(str(value).split())[:limit]


def _run_json(argv: list[str], timeout: int = 45) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            argv, check=False, capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"overall": "error", "error": bounded(exc)}
    raw = proc.stdout[:MAX_OUTPUT]
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {
            "overall": "error",
            "error": bounded(proc.stderr or raw or f"validator exit {proc.returncode}"),
        }
    if not isinstance(data, dict):
        return {"overall": "error", "error": "validator returned a non-object"}
    return data


def _section(text: str, heading: str) -> str:
    marker = f"## {heading}"
    if marker not in text:
        return ""
    body = text.split(marker, 1)[1]
    return body.split("\n## ", 1)[0]


def _plain(value: str) -> str:
    value = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", value)
    return value.replace("**", "").replace("`", "").strip()


def _table(text: str, heading: str) -> list[dict[str, str]]:
    lines = [line.strip() for line in _section(text, heading).splitlines()]
    rows = [line for line in lines if line.startswith("|") and line.endswith("|")]
    if len(rows) < 2:
        return []
    headers = [_plain(cell).lower().replace(" / ", "_").replace(" ", "_")
               for cell in rows[0].strip("|").split("|")]
    result: list[dict[str, str]] = []
    for line in rows[2:]:
        cells = [_plain(cell) for cell in line.strip("|").split("|")]
        if len(cells) == len(headers):
            result.append(dict(zip(headers, cells)))
    return result


def _revision(name: str, text: str) -> str:
    patterns = {
        "fleet-index.md": r"Fleet Index version:\s*([0-9.\-]+)",
        "roadmap-index.md": r"Roadmap index revision\s+([0-9.\-]+)",
        "conventions-index.md": r"Index revision\s+([0-9.\-]+)",
        "instructions-index.md": r"Parity revision:\**\s*`([^`]+)`",
        "automation-index.md": r"Registry revision:\**\s*`([^`]+)`",
    }
    match = re.search(patterns[name], text, re.IGNORECASE)
    return match.group(1) if match else "unversioned"


def _registry(text: str, begin: str, end: str) -> dict[str, Any]:
    try:
        body = text.split(begin, 1)[1].split(end, 1)[0]
        return json.loads(body.split("```json", 1)[1].split("```", 1)[0])
    except (IndexError, json.JSONDecodeError):
        return {}


def _conventions_status(lint_state: str, convention_check: dict[str, Any]) -> str:
    """error beats unknown beats ok. An unreachable linter is a probe failure
    (unknown), not a definite drift finding (error)."""
    if lint_state == "error" or _state(convention_check["overall"]) == "error":
        return "error"
    if lint_state == "unknown":
        return "unknown"
    return "ok"


def _project_stamp_summary(stamps: dict[str, Any]) -> str:
    projects = stamps.get("projects", [])
    if not isinstance(projects, list):
        projects = []
    current = sum(
        1 for project in projects
        if isinstance(project, dict) and project.get("status") == "ok"
    )
    return f"{current}/{len(projects)} project stamps"


def _lint_leg(lint: dict[str, Any]) -> tuple[str, str]:
    """Render the vault-lint leg of the conventions card.

    Two payload shapes reach here and they must not be confused:
      * vault_lint.py itself emits `ok`/`error_count`/`issues` and NO `overall`.
      * _run_json's failure envelope emits `overall` and NO `ok`.

    Keying on `overall` alone inverted them -- a real lint failure rendered as
    the probe-failure word "unknown" and never named the offending file, while
    an unreachable linter rendered as a definite "error". Key on `ok` first.
    """
    if "ok" in lint:
        if lint.get("ok"):
            return "ok", "lint ok"
        issues = lint.get("issues") or []
        count = int(lint.get("error_count") or len(issues))
        first = issues[0] if issues else {}
        detail = first.get("detail") or first.get("code") or "unspecified"
        text = f"lint {count} error{'s' if count != 1 else ''}: {detail}"
        if first.get("path"):
            text = f"{text} ({first['path']})"
        if len(issues) > 1:
            text = f"{text} +{len(issues) - 1} more"
        return "error", text
    return "unknown", f"lint unavailable ({lint.get('error') or lint.get('overall') or 'no result'})"


def _state(overall: object) -> str:
    value = str(overall or "unknown").lower()
    if value == "ok":
        return "ok"
    if value in {"warn", "warning", "unknown"}:
        return "warning"
    return "error"


def _roadmap_routes(rows: list[dict[str, str]]) -> dict[str, Any]:
    results = []
    for row in rows:
        vault = row.get("owning_vault", "")
        project = row.get("project", "unknown")
        roadmap = VAULT_ROOT / vault / "ROADMAP.md"
        handoff = VAULT_ROOT / vault / "HANDOFF.md"
        ok = bool(vault) and roadmap.is_file() and handoff.is_file()
        results.append({
            "project": project,
            "vault": vault,
            "status": "current" if ok else "missing",
            "roadmap": f"{vault}/ROADMAP.md",
            "handoff": f"{vault}/HANDOFF.md",
        })
    current = sum(row["status"] == "current" for row in results)
    return {"overall": "ok" if rows and current == len(rows) else "error",
            "counts": {"current": current, "declared": len(rows)}, "results": results}


def _convention_links(rows: list[dict[str, str]]) -> dict[str, Any]:
    results = []
    for row in rows:
        rel = row.get("canonical_document", "")
        # The Markdown table stores plain relative paths after link stripping.
        path = HOMELAB / rel
        ok = bool(rel) and path.is_file()
        results.append({"area": row.get("area", "unknown"), "path": rel,
                        "status": "current" if ok else "missing"})
    current = sum(row["status"] == "current" for row in results)
    return {"overall": "ok" if rows and current == len(rows) else "error",
            "counts": {"current": current, "declared": len(rows)}, "results": results}


def collect() -> dict[str, Any]:
    texts: dict[str, str] = {}
    index_meta: dict[str, dict[str, Any]] = {}
    for name in INDEX_NAMES:
        path = HOMELAB / name
        text = path.read_text(encoding="utf-8") if path.is_file() else ""
        texts[name] = text
        index_meta[name] = {
            "id": name.removesuffix("-index.md"),
            "file": name,
            "revision": _revision(name, text) if text else "missing",
            "exists": bool(text),
        }

    fleet_hosts = _table(texts["fleet-index.md"], "Physical fleet")
    roadmaps = _table(texts["roadmap-index.md"], "Routing contract")
    if not roadmaps:
        # The project table is before the Routing contract heading.
        raw = texts["roadmap-index.md"].split("## Routing contract", 1)[0]
        rows = [line.strip() for line in raw.splitlines() if line.strip().startswith("|")]
        if len(rows) >= 2:
            headers = [_plain(c).lower().replace(" / ", "_").replace(" ", "_")
                       for c in rows[0].strip("|").split("|")]
            roadmaps = [dict(zip(headers, [_plain(c) for c in row.strip("|").split("|")]))
                        for row in rows[2:] if len(row.strip("|").split("|")) == len(headers)]
    conventions = _table(texts["conventions-index.md"], "Canonical documents")
    instruction_registry = _registry(
        texts["instructions-index.md"],
        "<!-- INSTRUCTION-REGISTRY-V1-BEGIN -->",
        "<!-- INSTRUCTION-REGISTRY-V1-END -->",
    )
    automation_registry = _registry(
        texts["automation-index.md"],
        "<!-- AUTOMATION-REGISTRY-V1-BEGIN -->",
        "<!-- AUTOMATION-REGISTRY-V1-END -->",
    )

    instruction = _run_json([
        "python3", str(HOMELAB / "tools/bin/instruction_check.py"),
        "--vault-root", str(VAULT_ROOT), "--remote", "--json",
    ])
    automation = _run_json([
        "python3", str(HOMELAB / "tools/bin/automation_check.py"),
        "--vault-root", str(VAULT_ROOT), "--remote", "--json",
    ])
    stamps = _run_json([
        "python3", str(HOMELAB / "tools/bin/stamp_check.py"),
        "--root", str(VAULT_ROOT), "--check-only", "--json",
    ])
    lint = _run_json([
        "python3", str(HOMELAB / "tools/bin/vault_lint.py"),
        "--root", str(VAULT_ROOT), "--no-write", "--strict", "--json",
    ])
    roadmap_check = _roadmap_routes(roadmaps)
    convention_check = _convention_links(conventions)
    lint_state, lint_text = _lint_leg(lint)

    cards = [
        {**index_meta["fleet-index.md"], "title": "fleet", "status": _state(stamps.get("overall")),
         "summary": f"{len(fleet_hosts)} physical hosts · {_project_stamp_summary(stamps)}"},
        {**index_meta["roadmap-index.md"], "title": "roadmaps", "status": _state(roadmap_check["overall"]),
         "summary": f"{roadmap_check['counts']['current']}/{roadmap_check['counts']['declared']} project routes"},
        {**index_meta["conventions-index.md"], "title": "conventions",
         "status": _conventions_status(lint_state, convention_check),
         "summary": f"{convention_check['counts']['current']}/{convention_check['counts']['declared']} canonical links · {lint_text}"},
        {**index_meta["instructions-index.md"], "title": "instructions", "status": _state(instruction.get("overall")),
         "summary": f"{instruction.get('counts', {}).get('current', 0)} current · {instruction.get('counts', {}).get('manual_current', 0)} manual"},
        {**index_meta["automation-index.md"], "title": "automations", "status": _state(automation.get("overall")),
         "summary": f"{automation.get('counts', {}).get('current', 0)}/{len(automation_registry.get('entries', []))} current"},
    ]
    for card in cards:
        if not card["exists"] or card["revision"] in {"missing", "unversioned"}:
            card["status"] = "error"

    states = [card["status"] for card in cards]
    overall = "error" if "error" in states else ("warning" if "warning" in states else "ok")
    return {
        "version": 1,
        "generated_at": now_utc(),
        "overall": overall,
        "cards": cards,
        "fleet": {"hosts": fleet_hosts, "stamp_check": stamps},
        "roadmaps": {"entries": roadmaps, "check": roadmap_check},
        "conventions": {"entries": conventions, "check": convention_check, "vault_lint": lint},
        "instructions": {"registry_revision": instruction_registry.get("registry_revision"),
                         "surfaces": instruction_registry.get("surfaces", []), "check": instruction},
        "automations": {"registry_revision": automation_registry.get("registry_revision"),
                        "entries": automation_registry.get("entries", []), "check": automation},
    }


def collect_and_write(path: Path = CACHE) -> dict[str, Any]:
    started = datetime.now(timezone.utc)
    try:
        payload = collect()
        payload["collector_error"] = None
    except Exception as exc:
        payload = {"version": 1, "generated_at": now_utc(), "overall": "error",
                   "cards": [], "collector_error": bounded(exc)}
    payload["duration_seconds"] = round((datetime.now(timezone.utc) - started).total_seconds(), 3)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    os.replace(temporary, path)
    return payload


if __name__ == "__main__":
    result = collect_and_write()
    print(json.dumps(result, indent=2, sort_keys=True))
    raise SystemExit(0 if result.get("overall") != "error" else 1)
