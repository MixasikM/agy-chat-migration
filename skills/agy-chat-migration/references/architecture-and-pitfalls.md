# Antigravity Conversation Storage Architecture & Universal Pitfalls

This document explains how Antigravity stores projects, conversations, titles, and artifacts internally, and documents the **10 universal pitfalls ("подводные камни")** to avoid when moving chats between projects, migrating across operating systems, or syncing across machines.

---

## 1. The 4-Layer Antigravity Conversation Storage Model

Conversations do **not** live inside your project folder. By default, Antigravity stores them under `~/.gemini/<app>/`:
- **Windows / macOS Desktop v2:** `~/.gemini/antigravity/`
- **Legacy Antigravity IDE v1:** `~/.gemini/antigravity-ide/`
- **Linux / macOS CLI & Remote Control Daemon:** `~/.gemini/antigravity-cli/`
- **Shared Project Definitions:** `~/.gemini/config/projects/<project_id>.json`

To move, adopt, or sync conversations cleanly, **four synchronized layers** plus project definitions must stay consistent:

### Layer 0: Project Definitions (`~/.gemini/config/projects/<project_id>.json`)
Maps a `project_id` UUID to its workspace directory URI (`folderUri` or `gitFolder.folderUri`):
- **Windows:** URL-encoded drive colon (`file:///c%3A/Projects/my-app`, lowercase drive letter)
- **Linux / macOS:** POSIX URI (`file:///home/dev/projects/my-app` or `file:///Users/dev/projects/my-app`)

### Layer 1: Individual Conversation SQLite DB (`~/.gemini/<app>/conversations/<cid>.db`)
Stores the full conversation steps, tool calls, and metadata.
- Table `trajectory_metadata_blob` (`id = 'main'`) holds a serialized Protobuf `TrajectoryMetadata`:
  - **Field 1 (`wt=2`)**: `WorkspaceInfo` submessage:
    - `Subfield 1 (wt=2)`: raw URI (`file:///c:/Projects/my-app` or `file:///home/dev/projects/my-app`)
    - `Subfield 2 (wt=2)`: raw URI (matching Subfield 1)
    - `Subfield 3 (wt=2)`: `""`
    - `Subfield 4 (wt=2)`: git branch (e.g., `"master"` / `"main"`)
  - **Field 6 (`wt=2`)**: `conversation_id` UUID string (`<cid>`)
  - **Field 7 (`wt=2`)**: URL-encoded workspace URI (`file:///c%3A/...` on Windows, `file:///home/...` on Linux/macOS)
  - **Field 18 (`wt=2`)**: `project_id` UUID string. **If Field 18 is missing or empty, Antigravity places the conversation in *Outside of Project*.**

### Layer 2: Annotation Textproto (`~/.gemini/<app>/annotations/<cid>.pbtxt`)
Plain-text protobuf storing the user-visible sidebar title and view timestamp:
```text
title:"My Concise Chat Title" last_user_view_time:{seconds:1794000000 nanos:0}
```
Whenever `language_server` runs `recomputeAndStore` or `[Summaries] reconcile`, it reads `annotations/<cid>.pbtxt` to populate the sidebar title.

### Layer 3: Global Summaries SQLite DB (`~/.gemini/<app>/conversation_summaries.db`)
- Table `conversation_summaries`: holds searchable columns (`conversation_id`, `title`, `preview`, `step_count`, `last_modified_time`, `workspace_uris`, `source`, `project_id`, `app_data_dir`) and `raw_summary` (`CascadeTrajectorySummary` Protobuf):
  - **Field 1 (`wt=2`)**: `summary` — the **preview** / first user message (NOT the sidebar title!).
  - **Field 2 (`wt=0`)**: `step_count` (varint).
  - **Field 3 (`wt=2`)**: `last_modified_time` (`google.protobuf.Timestamp`: `Subfield 1` = Unix seconds).
  - **Field 4 (`wt=2`)**: `trajectory_id` (or legacy string/ID).
  - **Field 9 (`wt=2`)**: `workspaces` (`WorkspaceInfo` submessage matching Layer 1).
  - **Field 15 (`wt=2`)**: `annotations` (`TrajectoryAnnotations`: **`Subfield 1 (wt=2)` is the actual sidebar `title`**, `Subfield 7 (wt=2)` is `last_user_view_time`).
  - **Field 17 (`wt=2`)**: `trajectory_metadata` (`TrajectoryMetadata` matching Layer 1).
  - **Field 20 (`wt=0`)**: `source` (`17` = `CORTEX_TRAJECTORY_SOURCE_CLI`). **On Desktop UI, Field 20 MUST be omitted (`source = ""`), otherwise Desktop hides the conversation as a headless CLI session!**
- Table `conversations_fts`: Full-text search index storing `conversation_id`, `title`, `workspace`, `project`, and chunked step text (`chunk_index = -1` for metadata row, `0..N` for message chunks).

### Layer 4: Binary Summaries Cache (`agyhub_summaries_proto.pb` / `jetbox_summaries_proto.pb`) & Live RPC
- Binary serialized `SummariesState` read on startup by `language_server`:
  - **Field 1 (`wt=2`)**: `map<string, CascadeTrajectorySummary> summaries` (`Subfield 1` = `<cid>`, `Subfield 2` = `raw_summary` bytes).
  - **Field 2 (`wt=2`)**: `map<string, int64> file_mod_times` (`Subfield 1` = `<cid>`, `Subfield 2` = `.db` file Unix `mtime` in whole seconds).
- **Live Zero-Restart Update:** Active `language_server` instances publish their port and CSRF token in `~/.gemini/<app>/daemon/*.json` (`httpPort`, `csrfToken`). Posting a `JetboxWriteSummaryRequest` (`Field 1` = `<cid>`, `Field 2` = `raw_summary`) to:
  ```text
  POST http://127.0.0.1:<httpPort>/exa.language_server_pb.LanguageServerService/JetboxWriteSummary
  Header: Content-Type: application/proto
  Header: Connect-Protocol-Version: 1
  Header: x-codeium-csrf-token: <csrfToken>
  ```
  pushes summary and project-binding updates live without restarting Desktop.

---

## 2. The 10 Universal Pitfalls ("Подводные камни") & How to Avoid Them

### Pitfall 1: The `AdditionalConvoDir` CLI-Tagging Trap on Windows Desktop
- **Symptom:** Imported or migrated conversations appear briefly and then **completely vanish** on the next Windows Desktop restart.
- **Root Cause:** Desktop `language_server` scans `~/.gemini/antigravity-cli/conversations/` as an `AdditionalConvoDir` with `isCLIAppDataDir = true` *after* scanning `~/.gemini/antigravity/conversations/`. Any conversation ID found in `antigravity-cli/conversations` gets tagged with `source = CORTEX_TRAJECTORY_SOURCE_CLI` (`Field 20 = 17`), hiding it from the Desktop sidebar!
- **Solution:** On Windows Desktop, **never** import `.db` files into `~/.gemini/antigravity-cli/conversations/`. Delete duplicate `.db` files there if any exist.

### Pitfall 2: Deleting `agyhub_summaries_proto.pb` or Desyncing `.db` File `mtime`
- **Symptom:** Deleting `agyhub_summaries_proto.pb` to "force a cache rebuild" (or touching `.db` files without updating `file_mod_times`) causes chats to disappear or lose project links on startup.
- **Root Cause:** On startup, `scanAndRefresh` compares each `.db` file's filesystem `mtime` against `Field 2` (`file_mod_times`) in `agyhub_summaries_proto.pb` and `Field 3` (`last_modified_time`). If the cache is missing or the `.db` `mtime` is newer than `last_modified_time` and absent from `file_mod_times`, `language_server` assumes an external CLI modified the `.db`, runs `recomputeAndStore`, and marks the chat `CLI` (`Field 20 = 17`).
- **Solution:** Always patch `agyhub_summaries_proto.pb` **in-place** (preserving all other chats), strip `Field 20`, and call `os.utime(db_path, (mtime_sec, mtime_sec))` so the `.db` `mtime` matches both `Field 3` and `file_mod_times`.

### Pitfall 3: Missing `annotations/<cid>.pbtxt` Titles Causing Raw Multi-Line Prompts in Sidebar
- **Symptom:** After migrating or restarting the CLI daemon, chat titles turn blank or display the entire multi-paragraph first user prompt instead of a short title.
- **Root Cause:** `Field 1` of `raw_summary` holds the first user message (*preview*), whereas `Field 15.1` and `annotations/<cid>.pbtxt` hold the short *title*. Many older chats have `last_user_view_time:{...}` in `.pbtxt` without a `title:"..."` field. When `[Summaries] reconcile` runs, missing `.pbtxt` titles cause fallback to the raw prompt.
- **Solution:** Always verify `title:"..."` in `annotations/<cid>.pbtxt`, generate a concise fallback title (`<= 55` chars) if missing, and write both `Field 15.1` and `annotations/<cid>.pbtxt`.

### Pitfall 4: SQLite `-wal` / `-shm` Side Effects & Incomplete File Copies
- **Symptom:**
  1. Copying an active `.db` file with plain `cp`/`shutil.copy2` produces a corrupt or stale copy missing recent messages stored in `<cid>.db-wal`.
  2. Opening an idle `.db` with standard `sqlite3.connect()` briefly creates `-wal`/`-shm` files, firing `language_server` file watchers and modifying timestamps.
- **Solution:** Check `os.path.exists(db_path + "-wal")`:
  - If `-wal` **exists** (open DB): copy using SQLite's Online Backup API (`src_conn.backup(dst_conn)`).
  - If `-wal` does **not** exist (idle DB): read with `?mode=ro&immutable=1` and copy using `shutil.copy2` (followed by `os.utime` alignment after patching).

### Pitfall 5: Antigravity IDE v1 (`antigravity-ide`) Empty `brain/<cid>` & Unbound Chats
- **Symptom:** Legacy Antigravity IDE conversations migrated to Desktop v2 have chat text (`conversations/<cid>.db`) but are missing artifacts, plans, screenshots, and `scratch/` scripts (`brain/<cid>`), or remain stuck in *Outside of Project*.
- **Root Cause:** Desktop v2 auto-migration copied `conversations/<cid>.db` from `~/.gemini/antigravity-ide/` without always copying `~/.gemini/antigravity-ide/brain/<cid>/` or assigning a `project_id` (`Field 18`).
- **Solution:** Merge `~/.gemini/antigravity-ide/brain/<cid>` before `~/.gemini/antigravity/brain/<cid>` during export/move, and stamp `Field 18` (`project_id`) using `move-chat` or `export --adopt-chat <cid>`.

### Pitfall 6: Windows URI Encoding Mismatches (`C:` vs `c%3A` vs `c:`)
- **Symptom:** Project exists in `~/.gemini/config/projects/`, yet chats do not match the workspace.
- **Root Cause:** Antigravity writes lowercase drive letters with URL-encoded colons in `folderUri`, `workspace_uris`, and `TrajectoryMetadata.Field 7` (`file:///c%3A/Projects/my-app`), while `WorkspaceInfo.Field 1/2` uses the unencoded drive colon (`file:///c:/Projects/my-app`). On Linux/macOS, both use `file:///path/to/dir`.
- **Solution:** Always normalize both `raw_uri` and `encoded_uri` per target OS when rewriting `TrajectoryMetadata`, `CascadeTrajectorySummary`, `conversation_summaries`, `conversations_fts`, and `config/projects/<pid>.json`.

### Pitfall 7: File-Sync Ignore Rules (`.*`) & Monolithic Metadata Conflicts
- **Symptom:**
  1. Cloud/NAS sync tools that ignore hidden dot-folders (`.*` in `.stignore` / `.gitignore`) silently skip `.git/` and `.agy-capsule/`.
  2. Storing all chat metadata in a single JSON file produces `sync-conflict` files when two machines update different chats.
- **Solution:**
  1. Ensure your transfer/sync tool includes `.agy-capsule/` (add `.agy-capsule/` to `.gitignore` if you don't want binary `.db` files in Git, while keeping it included in file-level sync).
  2. Store per-chat metadata in `.agy-capsule/summaries/<cid>.json` and merge strictly by highest `step_count`.

### Pitfall 8: Importing Partially Transferred Large `.db` Files
- **Symptom:** Importing while a `50–300 MB` `.db` file is still transferring yields truncated SQLite errors or `0` step counts.
- **Solution:** Ensure the transfer completes (and no `.tmp` / `.part` / `.syncthing.*.tmp` files exist in `.agy-capsule/conversations/`) before running `import`. For initial migration of huge histories, direct `scp`/`rsync` or archive copy is faster and atomic.

### Pitfall 9: PowerShell Inline SSH Escaping (`\r\n` / Stripped `\n`) Corrupting Linux Scripts & Systemd Units
- **Symptom:** Creating Linux `.sh` scripts or `.service`/`.timer` units via inline `ssh user@host "echo ..."` from Windows PowerShell strips backslashes or injects `\r`, producing broken units (`[Unit]nDescription=...` or `Bad message`).
- **Solution:** Write all Linux files locally with `newline="\n"` (LF) and transfer them via `scp` or SFTP. Never generate multi-line Linux config files via PowerShell `ssh` inline strings.

### Pitfall 10: Project Root Cleanliness Policies (`AGENTS.md` / `GEMINI.md`)
- **Symptom:** Dropping helper scripts (`*.py`, `*.cmd`) or `.agy-capsule/` directly into a project root violates repository cleanliness rules or pollutes `git status`.
- **Solution:** Keep migration scripts globally in `~/.gemini/config/skills/agy-chat-migration/scripts/` and invoke them with `--project-dir <path>`. Use `--capsule-dir <external-path>` if `.agy-capsule/` cannot reside inside the workspace, or add `.agy-capsule/` to `.gitignore`.
