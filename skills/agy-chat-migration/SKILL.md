---
name: agy-chat-migration
description: >-
  Universal toolkit and runbook for migrating, relinking, adopting, splitting, and synchronizing
  Antigravity conversations (`conversations/*.db`, `annotations/*.pbtxt`, `brain/*`,
  `agyhub_summaries_proto.pb`) and projects across folders, projects, editions
  (Antigravity IDE v1 / Desktop v2 / CLI), and operating systems (Windows / Linux / macOS).
  Use this skill whenever the user asks to move chats between projects, adopt "Outside of Project"
  chats, relink conversations after renaming/moving a directory, migrate a project to another PC
  or devserver, recover disappeared chats, or split/truncate oversized conversation databases.
---

# Antigravity Chat & Project Migration Guide

This skill provides a **transport-agnostic**, zero-dependency Python 3 stdlib toolkit for moving and synchronizing Antigravity conversations and project associations across workspaces, editions, and operating systems without losing history, titles, or `brain/` artifacts.

For deep Protobuf wire-format schemas and the full catalog of pitfalls ("подводные камни"), read:
- [architecture-and-pitfalls.md](./references/architecture-and-pitfalls.md) — The 4-Layer Antigravity Storage Model and 10 Universal Pitfalls.
- [conversation-splitting-guide.md](./references/conversation-splitting-guide.md) — How to safely truncate, clone, or split oversized conversation `.db` files and `CHECKPOINT` steps.

---

## Golden Guardrails (Never Violate)

1. **NEVER put `.db` files into `~/.gemini/antigravity-cli/conversations` on Windows Desktop!**
   - Desktop `language_server` scans `antigravity-cli/conversations` as an `AdditionalConvoDir` with `isCLIAppDataDir = true` on startup and overwrites any matching chat with `source = CORTEX_TRAJECTORY_SOURCE_CLI` (`Field 20 = 17`), hiding it from the Desktop UI.
2. **NEVER delete `agyhub_summaries_proto.pb` (or `jetbox_summaries_proto.pb`)!**
   - Always update `Field 1` (`summaries`) and `Field 2` (`file_mod_times`) **in-place**, strip `Field 20` on Desktop, and align each `.db` file's filesystem `mtime` (`os.utime`) with `Field 3` (`last_modified_time`) and `file_mod_times`.
3. **NEVER leave `annotations/<cid>.pbtxt` without `title:"..."`!**
   - When `language_server` runs `recomputeAndStore` or `[Summaries] reconcile`, it reads `annotations/<cid>.pbtxt`. Missing titles become blank or fall back to raw multi-line prompts.
4. **NEVER run naive file copies on active `-wal` SQLite DBs or plain `sqlite3.connect()` on idle DBs:**
   - If `<cid>.db-wal` exists, use SQLite's Online Backup API (`sqlite3.connect(...).backup(...)`) to avoid losing uncheckpointed pages.
   - If `<cid>.db-wal` does not exist and you only read, open with `?mode=ro&immutable=1` so SQLite does not trigger file-watcher events or change `mtime`.
5. **NEVER hardcode another project's `project_id` UUID or copy multi-line unit files via PowerShell `ssh` inline strings:**
   - Always discover `project_id` from `~/.gemini/config/projects/*.json` or `.agy-capsule/project_id`, and transfer Linux scripts/systemd units via `scp` with LF (`\n`) line endings.
6. **Respect project root cleanliness (`AGENTS.md` / `GEMINI.md`):**
   - Run the scripts from the skill directory (using `--project-dir <path>`), or store capsules outside the repository using `--capsule-dir <path>`. If storing `.agy-capsule/` inside a repo, add `.agy-capsule/` to `.gitignore`.

---

## Script Path Resolution

All commands below reference the scripts bundled alongside this `SKILL.md` in `./scripts/`:
- [audit_project_for_migration.py](./scripts/audit_project_for_migration.py) — Read-only pre-flight audit and orphan chat finder.
- [agy_capsule_sync.py](./scripts/agy_capsule_sync.py) — Universal capsule exporter, importer, chat mover, and project relinker.
- [install_skill.py](./scripts/install_skill.py) — Global/workspace/plugin/remote installer.

---

## Workflow A: Move Chats Between Projects or Adopt Orphan ("Outside of Project") Chats

Use this workflow when chats appear under *Outside of Project*, belong to a legacy `antigravity-ide` workspace, or need to be reassigned from Project A to Project B on the same machine.

1. **Discover orphan / unbound / legacy IDE chats (read-only):**
   ```bash
   python scripts/audit_project_for_migration.py --list-orphan-chats
   ```
   Or audit a specific project to inspect its current UUID, path, and associated chats:
   ```bash
   python scripts/audit_project_for_migration.py "/path/to/target-project"
   ```
2. **Move or adopt one or more chats into the target project:**
   ```bash
   python scripts/agy_capsule_sync.py move-chat \
     --project-dir "/path/to/target-project" \
     --chat-id "<CONVERSATION_UUID_1>" \
     --chat-id "<CONVERSATION_UUID_2>"
   ```
   * What this does automatically:
     * Discovers (or accepts `--project-id <UUID>`) for the target project.
     * Exports the chat `.db`, merges `brain/<cid>` artifacts (including legacy `~/.gemini/antigravity-ide/brain/<cid>`), and preserves or generates a clean `annotations/<cid>.pbtxt` title.
     * Rewrites `trajectory_metadata_blob` (`Field 1`, `Field 7`, `Field 18`), `conversation_summaries.db`, `conversations_fts`, and `agyhub_summaries_proto.pb` in-place.
     * Pushes a live `JetboxWriteSummary` RPC update to any running `language_server` daemon and aligns `.db` `mtime`.
3. **Verify:**
   ```bash
   python scripts/agy_capsule_sync.py status --project-dir "/path/to/target-project"
   ```
   Check in Antigravity Desktop / IDE that the moved chat(s) now appear under the target project with intact titles and artifacts.

---

## Workflow B: Relink a Project After Moving or Renaming Its Folder (Same Machine)

When you rename or move a project folder (e.g., `C:\old\my-app` -> `D:\dev\my-app` or `~/tmp/app` -> `~/projects/app`), Antigravity binds chats to the old URI (`Field 7` / `folderUri`), causing chats to disappear from the workspace sidebar.

1. **Move or copy the project folder** to its new path (or create an OS symlink / Windows NTFS Junction `mklink /J` if legacy external scripts still point to the old path).
2. **Relink all existing conversations and project registration to the new path:**
   ```bash
   python scripts/agy_capsule_sync.py relink-project --project-dir "/new/path/to/my-app"
   ```
   *(If the project UUID cannot be auto-discovered by folder name, pass `--project-id "<PROJECT_UUID>"` found via `audit_project_for_migration.py` before moving.)*
3. **Verify:**
   - Check `~/.gemini/config/projects/<PROJECT_UUID>.json` now points to the new URI.
   - Restart Antigravity if the old folder was actively open in the UI, or verify live via `status`.

---

## Workflow C: Migrate or Sync a Project & Chats Across Machines / OSes (`Windows` ⇄ `Linux` ⇄ `macOS`)

This workflow works over **any** transfer mechanism (`scp`, `rsync`, `.zip`/`.tar.gz`, Git LFS/private repo, USB drive, cloud storage, or P2P file sync).

### Phase 1: Pre-Flight Audit on Source Machine (Read-Only)
1. Run the readiness scanner:
   ```bash
   python scripts/audit_project_for_migration.py "<ProjectNameOrPath>"
   ```
2. Review the output:
   - **Project Identity:** Record `project_id` and `project_name`.
   - **Conversations & Sizes:** Note large `.db` files (`> 50 MB`), active `-wal` sessions, and any untitled chats.
   - **Git & Clean-Root Rules:** Add `.agy-capsule/` to `.gitignore` if keeping the capsule inside the project directory, or use `--capsule-dir /tmp/my-capsule` to keep the project directory untouched.
   - **Cross-OS Portability:** Review hardcoded Windows drive paths (`C:\...`, `.bat`, `.ps1`) and external Python packages needed on the target machine.

### Phase 2: Export Portable Capsule on Source Machine
```bash
python scripts/agy_capsule_sync.py export --project-dir "/source/path/to/my-app"
```
*(Optional: add `--capsule-dir "/path/to/portable-capsule"` to export outside the project tree, or `--adopt-chat <UUID>` to bundle orphan chats at the same time.)*

### Phase 3: Transfer Project & Capsule to Target Machine
Use whichever transport fits your environment:
- **Direct SSH / `scp` / `rsync`:**
  ```bash
  rsync -avz --exclude "__pycache__" --exclude ".pytest_cache" "/source/path/to/my-app/" "user@target-host:/target/path/to/my-app/"
  ```
- **Archive (`tar` / `zip`):** Pack the project folder (including `.agy-capsule/`) and extract on the target host.
- **Continuous file-sync tool (Syncthing, Resilio, cloud drive, etc.):** Ensure `.agy-capsule/` is **not** excluded by ignore rules (such as overly broad `.*` ignore patterns) and wait until no temporary partial sync files (e.g., `.syncthing.*.tmp`) remain before importing.

### Phase 4: Import & Rebind on Target Machine
On the target machine (Windows, Linux, or macOS):
- If the target runs a headless CLI / Remote Control daemon (`antigravity-cli-daemon.service` on Linux), stop/restart it cleanly during import with `--restart-service`:
  ```bash
  python3 scripts/agy_capsule_sync.py import \
    --project-dir "/target/path/to/my-app" \
    --restart-service \
    --force
  ```
- On Windows Desktop or macOS Desktop (where the target project is not currently open in an active chat), omit `--restart-service`:
  ```powershell
  python scripts\agy_capsule_sync.py import --project-dir "D:\Projects\my-app" --force
  ```

### Phase 5: Verify on Target Machine
1. Run:
   ```bash
   python scripts/agy_capsule_sync.py status --project-dir "/target/path/to/my-app"
   ```
2. Verify `~/.gemini/config/projects/<PROJECT_UUID>.json` contains the target OS URI (`file:///d%3A/...` on Windows, `file:///home/...` on Linux/macOS).
3. On Windows, verify `~/.gemini/antigravity-cli/conversations` has **zero** `.db` files.
4. Open the project in Antigravity (or Remote Control) and restart once to verify zero chats vanish on startup.

### Optional: Continuous Two-Way Sync Between Machines
Once both machines share a synced directory containing `.agy-capsule/`:
- Run `agy_capsule_sync.py sync --project-dir "<path>"` periodically (e.g., via cron, `systemd --user` timer templates in `./scripts/systemd/`, or Windows Task Scheduler).
- Conflict resolution is automatic per-chat (`.agy-capsule/summaries/<cid>.json`), keeping whichever side has the higher `step_count`.

---

## Workflow D: Split or Truncate Oversized Conversations

When a single conversation grows too large (hundreds of megabytes, thousands of steps, multiple topics), read [conversation-splitting-guide.md](./references/conversation-splitting-guide.md) before modifying `.db` internals. Always back up `conversations/<cid>.db`, `annotations/<cid>.pbtxt`, `brain/<cid>/`, `conversation_summaries.db`, and `agyhub_summaries_proto.pb` first.
