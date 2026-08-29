#!/usr/bin/env python3
"""Offline, bounded fleet activity collector. Never invoked by HTTP handlers."""
from __future__ import annotations

import base64, gzip, hashlib, json, os, subprocess, sys, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path("/home/david/nexus")
STATE_ROOT = Path(os.environ.get("NEXUS_STATE_DIR")
                  or os.environ.get("PANEL_STATE_DIR")
                  or os.environ.get("FLEET_NEXUS_STATE_DIR")
                  or Path.home() / ".local" / "state" / "nexus")
CACHE = STATE_ROOT / "generated" / "activity.json"
VAULTS = Path("/home/david/Vaults")
HOSTS = ("alpha", "charlie", "delta", "macbook")
MAX_COMMITS_PER_REPO = 1000
MAX_EVIDENCE_FILES = 20000
MAX_EVENTS = 3000
MAX_ASSISTANT_TURNS = 100000

def run(argv: list[str], timeout: int = 20) -> tuple[str, str | None]:
    try:
        item = subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=True)
        return item.stdout, None
    except (OSError, subprocess.SubprocessError) as exc:
        return "", str(exc)[:300]

def collect_host(host: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    script = """import json,subprocess,glob,os
with open(os.path.expanduser('~/.config/fleet/fleet-git-repos.json')) as h: repos=json.load(h).get('repositories',[])
out={'commits':[],'receipts':[]}
for r in repos:
 try:
  fmt='%H%x1f%h%x1f%aI%x1f%s'
  q=subprocess.run(['git','-C',r['path'],'log','--all','--date=iso-strict','--pretty=format:'+fmt,'-n','1000'],text=True,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=12)
  for line in q.stdout.splitlines():
   p=line.split('\\x1f',3)
   if len(p)==4: out['commits'].append({'full_hash':p[0],'short_hash':p[1],'timestamp':p[2],'subject':p[3],'repository':r.get('name',r['path']),'path':r['path'],'branch':r.get('branch')})
 except Exception: pass
for f in glob.glob(os.path.expanduser('~/.local/state/fleet/fleet-git-push/receipts/*.json'))[-500:]:
 try:
  with open(f) as h: out['receipts'].append(json.load(h))
 except Exception: pass
print(json.dumps(out))"""
    if host == "alpha":
        argv = [sys.executable, "-c", script]
    else:
        encoded = base64.b64encode(script.encode()).decode()
        remote = f"python3 -c \"import base64;exec(compile(base64.b64decode('{encoded}'),'activity','exec'))\""
        argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, remote]
    # Fixed script and fixed host allowlist; no HTTP-derived interpolation.
    try:
        proc = subprocess.run(argv, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=75, check=True)
        data = json.loads(proc.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        return [], [], f"snapshot failed: {str(exc)[:300]}"
    commits = [{**x, "host": host} for x in data.get("commits", [])][:MAX_EVENTS]
    return commits, successful_pushes(data.get("receipts", []), host), None

def successful_pushes(receipts: list[dict[str, Any]], host: str) -> list[dict[str, Any]]:
    """Only an actual pushed receipt row is an activity push event."""
    pushes = []
    for receipt in receipts:
        finished = receipt.get("finished_at") or receipt.get("started_at")
        for repo in receipt.get("repositories", []):
            if repo.get("status") == "pushed":
                pushes.append({"event": "push", "host": host, "repository": repo.get("name"), "branch": repo.get("branch"), "status": "pushed", "finished_at": finished})
    return pushes

def provider(surface: str) -> str | None:
    lower = surface.lower()
    if "claude" in lower: return "Claude"
    if "codex" in lower or "openai" in lower: return "OpenAI"
    if "gemini" in lower or "gemini" in lower: return "Google"
    return None

def collect_evidence() -> tuple[list[dict[str, Any]], int, int, bool, dict[str, dict[str, int]]]:
    turns, seen, unknown = [], set(), 0
    coverage = {name: {"records": 0, "assistant_records": 0, "unknown_records": 0} for name in ("Claude", "OpenAI", "Google")}
    all_paths = sorted(VAULTS.glob("*/files/conversations/evidence/**/*.jsonl.gz"))
    paths = all_paths[:MAX_EVIDENCE_FILES]
    truncated = len(all_paths) > MAX_EVIDENCE_FILES
    for path in paths:
        try:
            with gzip.open(path, "rt", encoding="utf-8", errors="replace") as handle:
                for n, line in enumerate(handle):
                    if n >= 20000: break
                    try: row = json.loads(line)
                    except json.JSONDecodeError: continue
                    conv, surface, host = row.get("conversation_id"), str(row.get("surface", "")), row.get("host")
                    mapped = provider(surface)
                    if mapped:
                        coverage[mapped]["records"] += 1
                        if row.get("role") == "assistant": coverage[mapped]["assistant_records"] += 1
                        if row.get("role") == "unknown": coverage[mapped]["unknown_records"] += 1
                    if row.get("role") != "assistant": continue
                    key = (surface, host, conv, row.get("ordinal"))
                    if not conv or key in seen: continue
                    seen.add(key)
                    if not mapped: unknown += 1; continue
                    turns.append({"timestamp": row.get("timestamp"), "provider": mapped, "conversation_id": conv, "surface": surface, "host": host})
                    if len(turns) >= MAX_ASSISTANT_TURNS:
                        return turns, unknown, len(paths), True, coverage
        except (OSError, gzip.BadGzipFile):
            continue
    return turns, unknown, len(paths), truncated, coverage

def main() -> int:
    started = time.monotonic(); commits=[]; pushes=[]; errors={}
    for host in HOSTS:
        c,p,error = collect_host(host); commits.extend(c); pushes.extend(p)
        if error: errors[host] = error
    turns, unknown, evidence_files_scanned, turns_truncated, provider_coverage = collect_evidence()
    payload = {"version": 1, "generated_at": datetime.now(timezone.utc).isoformat().replace('+00:00','Z'), "collector_duration_seconds": round(time.monotonic()-started, 3), "host_errors": errors, "metric_definitions": {"primary_unit": "redacted assistant turns", "sessions": "unique conversation IDs", "push": "receipt repository row with status=pushed; up_to_date is excluded"}, "commits": sorted(commits, key=lambda x: str(x.get('timestamp','')), reverse=True)[:MAX_EVENTS], "pushes": sorted(pushes, key=lambda x: str(x.get('finished_at','')), reverse=True)[:MAX_EVENTS], "assistant_turns": turns, "assistant_turns_limit": MAX_ASSISTANT_TURNS, "assistant_turns_truncated": turns_truncated, "evidence_files_scanned": evidence_files_scanned, "provider_coverage": provider_coverage, "unknown_surface_assistant_turns": unknown}
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    temporary = CACHE.with_suffix('.json.tmp')
    temporary.write_text(json.dumps(payload, separators=(',',':')), encoding='utf-8')
    os.replace(temporary, CACHE)
    return 0
if __name__ == '__main__': raise SystemExit(main())
