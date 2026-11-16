# `agy-chat-migration` — Universal Antigravity Chat & Project Migration Toolkit

[Русская версия (README.ru.md)](./README.ru.md)

A **zero-dependency, transport-agnostic** Python 3 toolkit and Antigravity skill/plugin for migrating, relinking, adopting, splitting, and synchronizing Antigravity conversations (`conversations/*.db`, `annotations/*.pbtxt`, `brain/*`, `agyhub_summaries_proto.pb`) across:

- **Projects** (move chats from Project A to Project B, or adopt *"Outside of Project"* chats)
- **Directories** (relink all chats after moving or renaming a project folder)
- **Antigravity Editions** (legacy `antigravity-ide` v1 ➔ Desktop v2 `antigravity` ➔ headless `antigravity-cli` / Remote Control)
- **Operating Systems & Machines** (`Windows` ⇄ `Linux` ⇄ `macOS` via `scp`, `rsync`, `.tar.gz`/`.zip`, Git, cloud drives, or any file sync tool)

---

## Why This Exists

Antigravity does **not** store conversations inside your project folder. Chat history, sidebar titles, search indexes, artifacts (`brain/`), and startup binary caches live across **four synchronized layers** under `~/.gemini/`:

1. `conversations/<cid>.db` (SQLite + Protobuf `TrajectoryMetadata` with OS-specific URI & `project_id`)
2. `annotations/<cid>.pbtxt` (sidebar short title + view timestamp)
3. `conversation_summaries.db` (SQL columns + `CascadeTrajectorySummary` Protobuf `raw_summary` + `conversations_fts`)
4. `agyhub_summaries_proto.pb` / `jetbox_summaries_proto.pb` (binary startup cache tracking `.db` modification times)

Copying `.db` files manually breaks project associations, hides chats behind the Windows `AdditionalConvoDir` CLI trap (`Field 20 = 17`), wipes out titles, or misses active SQLite `-wal` pages. This toolkit automates all four layers safely and pushes live updates to running `language_server` instances.

---

## Installation

> **Requirements:** Python 3.9+ (pure standard library — no `pip install` needed).

### Option 1: Install as an Antigravity Plugin (Recommended)
Clone directly into your Antigravity plugins directory so Antigravity loads the skill automatically across all projects:

```bash
git clone https://github.com/MixasikM/agy-chat-migration.git ~/.gemini/config/plugins/agy-chat-migration
```

On Windows PowerShell:
```powershell
git clone https://github.com/MixasikM/agy-chat-migration.git "$env:USERPROFILE\.gemini\config\plugins\agy-chat-migration"
```

### Option 2: Install Only the Global Skill
```bash
git clone https://github.com/MixasikM/agy-chat-migration.git
cd agy-chat-migration
python skills/agy-chat-migration/scripts/install_skill.py
```

### Option 3: Install Into a Single Workspace
```bash
python skills/agy-chat-migration/scripts/install_skill.py --workspace /path/to/your/project
```

### Option 4: Deploy to a Remote Linux/macOS Host (with Optional Systemd Timer)
```bash
python skills/agy-chat-migration/scripts/install_skill.py --remote user@devserver --setup-systemd-timer --projects-root ~/projects
```

---

## Quickstart Recipes

> Replace `scripts/` below with the installed path (or run directly from `skills/agy-chat-migration/scripts/`).

### 1. Find Orphan, *"Outside of Project"*, or Legacy IDE Chats
```bash
python scripts/audit_project_for_migration.py --list-orphan-chats
```

### 2. Move or Adopt Chat(s) Into a Project
```bash
python scripts/agy_capsule_sync.py move-chat \
  --project-dir "/path/to/target-project" \
  --chat-id "<CONVERSATION_UUID>"
```
You can repeat `--chat-id` multiple times. Artifacts from `~/.gemini/antigravity-ide/brain/<cid>` are automatically merged if present.

### 3. Relink a Project After Moving or Renaming Its Folder
```bash
python scripts/agy_capsule_sync.py relink-project --project-dir "/new/path/to/my-app"
```
*(If the folder was also renamed and `project_id` cannot be auto-discovered, pass `--project-id "<UUID>"` found via `audit_project_for_migration.py` before moving.)*

### 4. Migrate a Project & All Its Chats to Another Machine / OS (`Windows` ⇄ `Linux` ⇄ `macOS`)

**On the source machine:**
```bash
# 1. Pre-flight read-only audit (checks sizes, untitled chats, -wal locks, Windows-only paths)
python scripts/audit_project_for_migration.py "/source/path/to/my-app"

# 2. Export portable .agy-capsule/ inside the project (or pass --capsule-dir /tmp/my-capsule)
python scripts/agy_capsule_sync.py export --project-dir "/source/path/to/my-app"
```

**Transfer the project folder (including `.agy-capsule/`)** using `rsync`, `scp`, `.tar.gz`/`.zip`, USB, or your preferred sync tool. *(Add `.agy-capsule/` to `.gitignore` if you use Git so binary chat DBs stay out of commits.)*

**On the target machine:**
```bash
# 3. Import, rewrite OS URIs, update all 4 layers, and relink
python3 scripts/agy_capsule_sync.py import --project-dir "/target/path/to/my-app" --force

# Add --restart-service on Linux servers running antigravity-cli-daemon.service:
python3 scripts/agy_capsule_sync.py import --project-dir "/target/path/to/my-app" --restart-service --force

# 4. Verify
python3 scripts/agy_capsule_sync.py status --project-dir "/target/path/to/my-app"
```

### 5. Two-Way Ongoing Sync Across Machines
Once `.agy-capsule/` is shared between machines via any file-sync tool:
```bash
python scripts/agy_capsule_sync.py sync --project-dir "/path/to/my-app"
```
Per-chat JSON files (`.agy-capsule/summaries/<cid>.json`) prevent merge conflicts and always pick the copy with the higher `step_count`.

---

## Documentation & Deep Dives

- **[Skill Runbook (SKILL.md)](./skills/agy-chat-migration/SKILL.md)** — Agent instructions and step-by-step checklists.
- **[4-Layer Architecture & Top 10 Pitfalls](./skills/agy-chat-migration/references/architecture-and-pitfalls.md)** — Why chats vanish after import (`AdditionalConvoDir` trap, `agyhub_summaries_proto.pb` `mtime` mismatch), how live RPC updates work, SQLite `-wal` handling, Windows `%3A` URI encoding, and CRLF issues.
- **[Conversation Splitting & Truncating Guide](./skills/agy-chat-migration/references/conversation-splitting-guide.md)** — Internal SQLite tables, `CHECKPOINT` (`Field 30`) Protobuf layout, reactivating archived steps, `conversations_fts` chunks, and filtering `brain/<cid>` transcripts.

---

## License

[MIT](./LICENSE)
