# Guide: Splitting, Cloning, and Truncating Oversized Antigravity Conversations

When a conversation grows to thousands of steps or hundreds of megabytes — or combines two unrelated topics in a single thread — you can cleanly **truncate**, **clone**, or **split** it into separate Antigravity conversations without losing history or breaking context compaction.

> **WARNING:** Always create a full backup before modifying any `.db` or summary files:
> - `~/.gemini/<app>/conversations/<cid>.db` (including `-wal`/`-shm` after checkpointing or closing the active chat)
> - `~/.gemini/<app>/annotations/<cid>.pbtxt`
> - `~/.gemini/<app>/brain/<cid>/`
> - `~/.gemini/<app>/conversation_summaries.db`
> - `~/.gemini/<app>/agyhub_summaries_proto.pb`

---

## 1. Internal Schema of `<cid>.db`

An Antigravity conversation SQLite database contains 6 core tables:

| Table | Primary Key / Index | Purpose |
| :--- | :--- | :--- |
| `trajectory_meta` | `trajectory_id`, `cascade_id` | Links the SQLite file to a specific `cascade_id` (`<cid>`) and `trajectory_id`. |
| `trajectory_metadata_blob` | `id = 'main'` | Protobuf `TrajectoryMetadata` (`Field 1` `WorkspaceInfo`, `Field 6` `conversation_id`, `Field 7` `encoded_uri`, `Field 18` `project_id`). |
| `parent_references` | `idx` | Tracks conversation span metadata (`idx=1` Protobuf `Field 2` holds `last_step_index`). |
| `steps` | `idx` (`0..N`) | Sequential conversation steps (`status`, `metadata` blob, `step_payload` blob). |
| `gen_metadata` | `idx` | LLM generation records referenced by step ranges. |
| `executor_metadata` | `idx` | Tool/executor execution records referenced by step ranges. |

---

## 2. How Context Compaction & `CHECKPOINT` Steps Work

Long conversations contain periodic `CHECKPOINT` steps (when Antigravity summarizes earlier turns to free context window). Understanding their structure is essential before truncating or splitting:

1. **Archived vs. Active Steps:**
   - Steps before the latest checkpoint are often stored in an archived/compressed state (`steps.status` != `3`, and `step_payload` `Field 4` != `3`).
   - If you truncate a conversation **backward** to end at an earlier step (e.g., splitting Topic A out of steps `0..1201`), you must reactivate the last turn (`steps.status = 3` and `step_payload` `Field 4 = 3`) so the UI renders those steps as live, visible messages rather than hidden pre-compaction history.
2. **Checkpoint Payload (`step_payload` `Field 30`):**
   Inside a `CHECKPOINT` step, `Field 30` is a nested Protobuf containing:
   - **`Field 1` (`wt=0`)**: Checkpoint sequence/version marker.
   - **`Field 5` (`wt=2`)**: The `<summary>...</summary>` text injected after compaction.
   - **`Field 11` / `Field 12` (`wt=0`)**: Start/end step indices covered by this checkpoint.
   - **`Field 14` (`wt=2`, repeated)**: Active artifact entries (`Subfield 1` = artifact name).
   - **`Field 20` (`wt=2`, repeated)**: Active subagent references (`Subfield 5 -> Subfield 1` = subagent `conversationId`).
   - **`Field 27` (`wt=0`)**: Total count of referenced subagents (`len(Field 20)`).
   - **`Field 30` (`wt=2`, repeated)**: Numbered `# User Requests` preserved across compaction (`Subfield 1` = request string).

---

## 3. Clean Split / Clone / Truncate Checklist

Follow this exact sequence when splitting one `<OLD_CID>` into `<NEW_CID_A>` (earlier segment) and `<NEW_CID_B>` (later segment), or cloning with a new UUID:

### Step 1: Close the Target Chat & Checkpoint SQLite
Ensure the chat is closed in Antigravity (no `-wal`/`-shm` locks) or run SQLite Online Backup API + `PRAGMA wal_checkpoint(FULL);` on a working copy so `<OLD_CID>.db` is self-contained.

### Step 2: Assign Fresh UUIDs
Generate new UUIDs for every derived conversation:
- New `conversation_id` (`<NEW_CID>`)
- New `trajectory_id` (distinct UUID for `trajectory_meta.trajectory_id` and `raw_summary` `Field 4`)

### Step 3: Rewrite All Internal UUID References in `<NEW_CID>.db`
1. `UPDATE trajectory_meta SET trajectory_id=?, cascade_id=?`
2. Rewrite `trajectory_metadata_blob` (`id='main'`): update `Field 6` (`<NEW_CID>`) and `Field 18` (`<PROJECT_ID>`), and replace `<OLD_CID>` bytes anywhere inside metadata/payloads where artifacts or transcript paths reference the old brain directory (`brain/<OLD_CID>` -> `brain/<NEW_CID>`).
3. Update `parent_references`, `gen_metadata`, `executor_metadata`, and `steps`:
   - If truncating tail steps (`idx > CUTOFF_STEP`): delete tail rows from `steps`, delete corresponding trailing `gen_metadata`/`executor_metadata` rows, and update `parent_references` (`idx=1`, `Field 2 = CUTOFF_STEP`).
   - If stripping early steps (`idx < CUTOFF_STEP`) while preserving step indices: strip large controller/tool blobs from early `steps` rows and clean `CHECKPOINT` `Field 30` (`Field 5`, `Field 14`, `Field 20`, `Field 27`, `Field 30`) so Topic A artifacts/subagents are no longer injected into Topic B's prompt.
4. Run `VACUUM;` on the resulting `.db` to reclaim disk space from deleted blobs.

### Step 4: Filter `brain/<NEW_CID>/` and Transcript Logs
Copy only matching artifacts into `~/.gemini/<app>/brain/<NEW_CID>/`:
- Artifacts (root files, `.user_uploaded/`, `scratch/`, `.system_generated/logs/chunks/`, `.system_generated/logs/media/`).
- Filter `.system_generated/logs/transcript.jsonl` and `transcript_full.jsonl` by `step_index`, replacing `<OLD_CID>` with `<NEW_CID>` in paths/URIs and removing dropped `[ARTIFACT: ...]` / `# Subagents` blocks from compaction lines.

### Step 5: Synchronize All 4 Summary & Cache Layers
1. **`annotations/<NEW_CID>.pbtxt`**: Write `title:"<New Title>" last_user_view_time:{seconds:<sec> nanos:<nanos>}`.
2. **`conversation_summaries.db` (`conversation_summaries`)**: Insert/update the row with updated `conversation_id`, `title`, `preview`, `step_count`, `last_modified_time`, `source = ''`, and rewritten `raw_summary` (`Field 2` step_count, `Field 3` `last_modified_time`, `Field 4` `trajectory_id`, `Field 15.1` `title`, `Field 17` `TrajectoryMetadata`, strip `Field 20`).
3. **`conversation_summaries.db` (`conversations_fts`)**: Copy/filter matching `chunk_index` rows (`-1` metadata row + relevant `0..N` chunks) under `<NEW_CID>` with the new `title` and `step_count`.
4. **`agyhub_summaries_proto.pb` & `.db` `mtime`**:
   - Align `os.utime("<NEW_CID>.db", (mod_sec, mod_sec))` with `raw_summary` `Field 3`.
   - Patch `agyhub_summaries_proto.pb` in-place (`Field 1` `summaries[<NEW_CID>]` and `Field 2` `file_mod_times[<NEW_CID>]`).
   - Optionally push `JetboxWriteSummary` to the running `language_server` daemon.

---

## 4. Inspecting Steps & Checkpoints Read-Only

Use `?mode=ro&immutable=1` to inspect an idle conversation safely without disturbing file locks:

```python
import sqlite3

db_path = "/path/to/conversations/<cid>.db"
conn = sqlite3.connect(f"file:{db_path}?mode=ro&immutable=1", uri=True)
print("Total steps:", conn.execute("SELECT COUNT(*) FROM steps").fetchone()[0])
print("Meta:", conn.execute("SELECT * FROM trajectory_meta").fetchall())
conn.close()
```
