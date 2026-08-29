# nexus_heartbeat_archive

Reversible, evidence-gated archival of **terminal, one-shot** heartbeat job
records out of the live `~/Vaults/loupe-vault/heartbeats/` directory, so the
Jobs panel doesn't accumulate stale done cards forever.

Standalone stdlib script. Not imported by the Nexus app (`app/*`), no HTTP/API
route — running it never requires a service restart.

## What it does

1. Scans `heartbeats/*.json` **top-level only** (never recursive — matches the
   live Jobs panel's own scan in `app/work.py`, so it never sees or touches
   `heartbeats/archive/` or `heartbeats/inline/`).
2. A record is only a candidate if its job id is an **exact entry** in
   `archive_allowlist.json`. Nothing is inferred from filename or shape — an
   unlisted record is always skipped, full stop.
3. Every candidate must still pass, independent of the allowlist:
   - shape check: has `job` + `state` + `run_id` keys (a real job record, not a
     host/thermal/gallery sidecar that slipped into the allowlist by accident);
   - `state` is exactly `"done"` (hard-denied outright if `failed`, `error`,
     `blocked`, `running`, `ended`, or `stalled` — denylist wins over allowlist,
     always);
   - `kind` is absent, or exactly `"job"` (any other value, e.g. `"watch"` /
     `"guard"`, is refused);
   - file mtime age is at least 24 hours;
   - the job id does not contain `host-`, `thermal`, `gallery`, `nas3`, or
     `temple` (case-insensitive) — a hard safety net independent of the
     allowlist file, so an allowlist typo can never archive a live sidecar or
     a diagnostic (`temple-file-catalog`) record.
4. **Dry-run by default.** Prints the full decision report (every file, moved
   or not, with its reason) to stdout. Zero filesystem writes.
5. `--apply` is required to actually move anything. Immediately before each
   move, the file is re-stat'd / re-read / re-hashed and re-evaluated from
   scratch; any change since the initial scan (a race — e.g. a relaunch under
   the same id) aborts that file's move.
6. Moves only, never deletes: `os.replace(src, dst)`, same filesystem, into
   one timestamped `heartbeats/archive/nexus7-terminal-archive-<UTC>/`
   directory per invocation. If the destination already exists, the move is
   refused (`collision`) rather than overwriting.
7. Every `--apply` run writes `manifest.json` in that archive directory,
   listing every file the run considered — moved (with hash/size/mtime/state/
   kind/evidence/moved_at and a literal `restore_cmd`) and every
   preserved/skipped record with its reason.

## Usage

```
# Dry run (default) — report only, touches nothing
python3 tools/heartbeat-archive/nexus_heartbeat_archive.py

# Apply — actually move eligible files
python3 tools/heartbeat-archive/nexus_heartbeat_archive.py --apply

# Point at a different heartbeats dir / allowlist (e.g. for tests)
python3 tools/heartbeat-archive/nexus_heartbeat_archive.py \
    --heartbeats-dir /path/to/scratch/heartbeats \
    --allowlist /path/to/scratch/allowlist.json
```

## Restore a moved record

Every manifest entry carries a literal restore command. Equivalent by hand:

```
cp -p heartbeats/archive/nexus7-terminal-archive-<UTC>/<job>.json heartbeats/<job>.json
sha256sum heartbeats/<job>.json   # compare against manifest.json's pre-move sha256
```

## Editing the allowlist

`archive_allowlist.json` is a human-maintained config, not inferred. Add a job
id only once you have concrete evidence it's terminal and has no live
producer (see the `evidence` field on each existing entry for the expected
level of detail). Adding an entry is necessary but not sufficient — the tool
still re-checks state/kind/age/shape/hard-denied-id-substrings at scan time
and again immediately before every move.
