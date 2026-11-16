#!/usr/bin/env python3
"""
agy_capsule_sync.py - Universal Antigravity Conversation & Project Capsule Sync

Synchronizes, moves, adopts, and relinks Antigravity conversations, summaries,
Protobuf workspace bindings, annotations, and brain artifacts across projects,
editions (Antigravity IDE v1 / Desktop v2 / CLI), and operating systems
(Windows / Linux / macOS) using a portable `.agy-capsule/` directory.

Key safeguards:
  1. Zero hardcoded project UUIDs: auto-discovers `project_id` & `project_name`
     from `.agy-capsule/project.json` or `~/.gemini/config/projects/*.json`
     (or explicit `--project-id`).
  2. Prevents the Windows `AdditionalConvoDir` (`antigravity-cli/conversations`)
     CLI-tagging bug (never imports into `antigravity-cli` on Windows and removes
     only conflicting duplicate `.db` files for imported conversation IDs).
  3. Updates `agyhub_summaries_proto.pb` / `jetbox_summaries_proto.pb` in-place
     (preserving `file_mod_times`, other projects, and aligning `.db` `mtime`).
  4. Preserves or generates clean short titles in `annotations/<cid>.pbtxt`.
  5. Uses `?mode=ro&immutable=1` on idle DBs (zero spurious file-watcher events)
     and SQLite Online Backup API on live open `-wal` DBs.
  6. Stores conflict-free per-chat `.agy-capsule/summaries/<cid>.json` merged by
     highest `step_count`.
  7. Pushes live zero-restart updates to running `language_server` instances
     via the `JetboxWriteSummary` Connect RPC.

Usage:
  python agy_capsule_sync.py export         --project-dir <path> [--project-id <uuid>] [--adopt-chat <cid>] [--capsule-dir <path>] [--force]
  python agy_capsule_sync.py import         --project-dir <path> [--project-id <uuid>] [--capsule-dir <path>] [--restart-service] [--force]
  python agy_capsule_sync.py sync           --project-dir <path> [--capsule-dir <path>] [--restart-service]
  python agy_capsule_sync.py move-chat      --project-dir <path> --chat-id <cid> [--chat-id <cid> ...] [--project-id <uuid>]
  python agy_capsule_sync.py relink-project --project-dir <path> [--project-id <uuid>] [--restart-service]
  python agy_capsule_sync.py status         --project-dir <path> [--capsule-dir <path>]
"""

import argparse
import datetime
import glob
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Protobuf Wire-Format Helpers (Zero external dependencies)
# ---------------------------------------------------------------------------

def encode_varint(n: int) -> bytes:
    res = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            res.append(b | 0x80)
        else:
            res.append(b)
            break
    return bytes(res)


def parse_msg(data: bytes):
    i = 0
    fields = []
    n = len(data)
    while i < n:
        key = 0
        shift = 0
        while True:
            if i >= n:
                break
            b = data[i]
            i += 1
            key |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
        if i >= n and key == 0:
            break
        fn = key >> 3
        wt = key & 7
        if wt == 0:
            val = 0
            shift = 0
            while True:
                if i >= n:
                    break
                b = data[i]
                i += 1
                val |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            fields.append((fn, wt, val))
        elif wt == 2:
            length = 0
            shift = 0
            while True:
                if i >= n:
                    break
                b = data[i]
                i += 1
                length |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            chunk = data[i:i + length]
            i += length
            fields.append((fn, wt, chunk))
        elif wt == 1:
            chunk = data[i:i + 8]
            i += 8
            fields.append((fn, wt, chunk))
        elif wt == 5:
            chunk = data[i:i + 4]
            i += 4
            fields.append((fn, wt, chunk))
        else:
            break
    return fields


def serialize_field(fn: int, wt: int, val) -> bytes:
    tag = encode_varint((fn << 3) | wt)
    if wt == 0:
        return tag + encode_varint(val)
    elif wt == 2:
        return tag + encode_varint(len(val)) + val
    elif wt in (1, 5):
        return tag + val
    return b""


def serialize_msg(fields) -> bytes:
    res = bytearray()
    for fn, wt, val in fields:
        res += serialize_field(fn, wt, val)
    return bytes(res)


def build_workspace_submsg(raw_uri: str, branch: str = "master") -> bytes:
    uri_bytes = raw_uri.encode("utf-8")
    return serialize_msg([
        (1, 2, uri_bytes),
        (2, 2, uri_bytes),
        (3, 2, b""),
        (4, 2, branch.encode("utf-8")),
    ])


def rewrite_trajectory_metadata_blob(
    blob: bytes,
    raw_uri: str,
    encoded_uri: str,
    project_id: str,
    conversation_id: str = "",
) -> bytes:
    fields = parse_msg(blob)
    ws_submsg = build_workspace_submsg(raw_uri)
    new_fields = []
    has_f1 = has_f6 = has_f7 = False

    for fn, wt, val in fields:
        if fn == 1 and wt == 2:
            if not has_f1:
                new_fields.append((1, 2, ws_submsg))
                has_f1 = True
        elif fn == 6 and wt == 2:
            new_fields.append((6, 2, (conversation_id.encode("utf-8") if conversation_id else val)))
            has_f6 = True
        elif fn == 7 and wt == 2:
            if not has_f7:
                new_fields.append((7, 2, encoded_uri.encode("utf-8")))
                has_f7 = True
        elif fn == 18:
            continue
        else:
            new_fields.append((fn, wt, val))

    if not has_f1:
        new_fields.insert(0, (1, 2, ws_submsg))
    if not has_f6 and conversation_id:
        new_fields.append((6, 2, conversation_id.encode("utf-8")))
    if not has_f7:
        new_fields.append((7, 2, encoded_uri.encode("utf-8")))

    new_fields.append((18, 2, project_id.encode("utf-8")))
    return serialize_msg(new_fields)


def extract_title_from_raw_summary(raw_summary: bytes) -> str:
    if not raw_summary:
        return ""
    f1_preview = ""
    for fn, wt, val in parse_msg(raw_summary):
        if fn == 15 and wt == 2:
            for sfn, swt, sval in parse_msg(val):
                if sfn == 1 and swt == 2 and sval:
                    return sval.decode("utf-8", errors="replace")
        elif fn == 1 and wt == 2 and not f1_preview:
            f1_preview = val.decode("utf-8", errors="replace")
    return f1_preview


def extract_preview_from_raw_summary(raw_summary: bytes) -> str:
    if not raw_summary:
        return ""
    for fn, wt, val in parse_msg(raw_summary):
        if fn == 1 and wt == 2:
            return val.decode("utf-8", errors="replace")
    return ""


def extract_mtime_sec_from_raw_summary(raw_summary: bytes) -> int:
    if not raw_summary:
        return int(time.time()) - 60
    for fn, wt, val in parse_msg(raw_summary):
        if fn == 3 and wt == 2:
            for sfn, swt, sval in parse_msg(val):
                if sfn == 1 and swt == 0 and sval > 0:
                    return int(sval)
    return int(time.time()) - 60


def extract_title_from_pbtxt(pbtxt_path: str) -> str:
    if not os.path.exists(pbtxt_path):
        return ""
    try:
        with open(pbtxt_path, "r", encoding="utf-8", errors="replace") as f:
            txt = f.read()
        m = re.search(r'title:\s*"((?:[^"\\]|\\.)*)"', txt)
        if m:
            raw_str = m.group(1)
            if "\\3" in raw_str or "\\2" in raw_str:
                return raw_str.encode("utf-8").decode("unicode_escape").encode("latin1").decode("utf-8", errors="replace")
            return raw_str.replace('\\"', '"')
    except Exception:
        pass
    return ""


def make_short_fallback_title(preview: str, cid: str) -> str:
    """Derives a clean <=55-char sidebar title from the first meaningful line of preview when untitled."""
    if preview:
        for line in preview.splitlines():
            cleaned = re.sub(r"<[^>]+>", "", line).strip()
            cleaned = re.sub(r"^@\[[^\]]+\]\s*", "", cleaned).strip()
            if cleaned:
                if len(cleaned) > 55:
                    return cleaned[:52].rstrip() + "..."
                return cleaned
    return f"Chat {cid[:8]}"


def write_annotation_pbtxt(pbtxt_path: str, title: str, mtime_sec: int = 0):
    os.makedirs(os.path.dirname(os.path.abspath(pbtxt_path)), exist_ok=True)
    sec = mtime_sec or int(time.time())
    nanos = 0
    if os.path.exists(pbtxt_path):
        try:
            with open(pbtxt_path, "r", encoding="utf-8", errors="replace") as f:
                old = f.read()
            ms = re.search(r"seconds:\s*(\d+)", old)
            mn = re.search(r"nanos:\s*(\d+)", old)
            if ms:
                sec = int(ms.group(1))
            if mn:
                nanos = int(mn.group(1))
        except Exception:
            pass
    escaped_title = title.replace("\\", "\\\\").replace('"', '\\"')
    content = f'title:"{escaped_title}" last_user_view_time:{{seconds:{sec} nanos:{nanos}}}'
    with open(pbtxt_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


def rewrite_raw_summary_blob(
    raw_summary: bytes,
    raw_uri: str,
    encoded_uri: str,
    project_id: str,
    conversation_id: str,
    title: str = "",
    preview: str = "",
    step_count: int = 0,
    meta_blob: bytes = b"",
) -> bytes:
    ws_submsg = build_workspace_submsg(raw_uri)
    if not raw_summary:
        mtime_sec = int(time.time()) - 60
        eff_summary = preview or title or f"Chat {conversation_id[:8]}"
        eff_title = title or eff_summary
        f17 = rewrite_trajectory_metadata_blob(meta_blob, raw_uri, encoded_uri, project_id, conversation_id)
        f15 = serialize_msg([
            (1, 2, eff_title.encode("utf-8")),
            (7, 2, serialize_msg([(1, 0, mtime_sec)])),
        ])
        synth = [
            (1, 2, eff_summary.encode("utf-8")),
            (2, 0, max(0, int(step_count))),
            (3, 2, serialize_msg([(1, 0, mtime_sec)])),
            (4, 2, conversation_id.encode("utf-8")),
            (5, 0, 1),
            (7, 2, serialize_msg([(1, 0, mtime_sec)])),
            (9, 2, ws_submsg),
            (15, 2, f15),
            (17, 2, f17),
            (22, 0, 4),
            (23, 0, 0),
            (28, 0, 4),
        ]
        return serialize_msg(synth)
    fields = parse_msg(raw_summary)
    mtime_sec = extract_mtime_sec_from_raw_summary(raw_summary)
    new_fields = []
    has_f1 = has_f9 = has_f15 = False

    effective_summary = preview or extract_preview_from_raw_summary(raw_summary) or title

    for fn, wt, val in fields:
        if fn == 4 and wt == 0:
            new_fields.append((4, 2, conversation_id.encode("utf-8")))
            if not any(f == 5 and w == 0 for f, w, _ in fields):
                new_fields.append((5, 0, 1))
        elif fn == 1 and wt == 2:
            if not has_f1:
                new_fields.append((1, 2, effective_summary.encode("utf-8") if effective_summary else val))
                has_f1 = True
        elif fn == 9 and wt == 2:
            if not has_f9:
                new_fields.append((9, 2, ws_submsg))
                has_f9 = True
        elif fn == 15 and wt == 2:
            f15_sub = [(sfn, swt, sval) for (sfn, swt, sval) in parse_msg(val) if sfn != 1]
            if title:
                f15_sub.insert(0, (1, 2, title.encode("utf-8")))
            if not any(sfn == 7 for sfn, _, _ in f15_sub):
                f15_sub.append((7, 2, serialize_msg([(1, 0, mtime_sec)])))
            new_fields.append((15, 2, serialize_msg(f15_sub)))
            has_f15 = True
        elif fn == 17 and wt == 2:
            rewritten_f17 = rewrite_trajectory_metadata_blob(
                val, raw_uri, encoded_uri, project_id, conversation_id
            )
            new_fields.append((17, 2, rewritten_f17))
        elif fn == 20:
            # Strip CORTEX_TRAJECTORY_SOURCE_CLI tag so Desktop UI does not hide the chat
            continue
        else:
            new_fields.append((fn, wt, val))

    if not has_f1 and effective_summary:
        new_fields.insert(0, (1, 2, effective_summary.encode("utf-8")))
    if not has_f9:
        new_fields.append((9, 2, ws_submsg))
    if not has_f15 and title:
        f15_sub = [
            (1, 2, title.encode("utf-8")),
            (7, 2, serialize_msg([(1, 0, mtime_sec)])),
        ]
        new_fields.append((15, 2, serialize_msg(f15_sub)))

    return serialize_msg(new_fields)


def update_summaries_proto_cache(pb_path: str, updated_summaries: dict, updated_mod_times: dict):
    existing_summaries = {}
    existing_mod_times = {}
    other_fields = []

    if os.path.exists(pb_path):
        try:
            with open(pb_path, "rb") as f:
                data = f.read()
            for fn, wt, val in parse_msg(data):
                if fn == 1 and wt == 2:
                    sub = parse_msg(val)
                    k = next((v.decode("utf-8", errors="replace") for sfn, swt, v in sub if sfn == 1 and swt == 2), "")
                    v = next((v for sfn, swt, v in sub if sfn == 2 and swt == 2), b"")
                    if k:
                        existing_summaries[k] = v
                elif fn == 2 and wt == 2:
                    sub = parse_msg(val)
                    k = next((v.decode("utf-8", errors="replace") for sfn, swt, v in sub if sfn == 1 and swt == 2), "")
                    v = next((v for sfn, swt, v in sub if sfn == 2 and swt == 0), 0)
                    if k:
                        existing_mod_times[k] = v
                else:
                    other_fields.append((fn, wt, val))
        except Exception:
            pass

    existing_summaries.update(updated_summaries)
    existing_mod_times.update(updated_mod_times)

    out_fields = []
    for cid, raw_sum in existing_summaries.items():
        entry = serialize_msg([(1, 2, cid.encode("utf-8")), (2, 2, raw_sum)])
        out_fields.append((1, 2, entry))
    for cid, mtime in existing_mod_times.items():
        if mtime > 0:
            entry = serialize_msg([(1, 2, cid.encode("utf-8")), (2, 0, int(mtime))])
            out_fields.append((2, 2, entry))
    out_fields.extend(other_fields)

    with open(pb_path, "wb") as f:
        f.write(serialize_msg(out_fields))


def push_live_summaries_to_daemon(agy_dir: str, updated_summaries: dict) -> int:
    daemon_dir = os.path.join(agy_dir, "daemon")
    if not os.path.exists(daemon_dir):
        return 0

    pushed = 0
    for df in glob.glob(os.path.join(daemon_dir, "*.json")):
        try:
            with open(df, "r", encoding="utf-8") as f:
                info = json.load(f)
            http_port = info.get("httpPort")
            csrf_token = info.get("csrfToken", "")
            if not http_port or not csrf_token:
                continue

            url = f"http://127.0.0.1:{http_port}/exa.language_server_pb.LanguageServerService/JetboxWriteSummary"
            daemon_ok = False
            for cid, summary_bytes in updated_summaries.items():
                req_bytes = serialize_msg([
                    (1, 2, cid.encode("utf-8")),
                    (2, 2, summary_bytes),
                ])
                req = urllib.request.Request(
                    url,
                    data=req_bytes,
                    headers={
                        "Content-Type": "application/proto",
                        "Connect-Protocol-Version": "1",
                        "x-codeium-csrf-token": csrf_token,
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=1.5) as resp:
                    if resp.status == 200:
                        daemon_ok = True
            if daemon_ok:
                pushed += 1
        except Exception:
            continue
    return pushed


# ---------------------------------------------------------------------------
# Project Auto-Discovery & Environment Helpers
# ---------------------------------------------------------------------------

def get_project_paths(project_dir: str):
    project_dir = os.path.abspath(project_dir)
    if sys.platform == "win32":
        drive, rest = os.path.splitdrive(project_dir)
        drive_letter = drive.rstrip(":").lower()
        rest_posix = urllib.parse.quote(rest.replace("\\", "/"), safe="/")
        raw_rest = rest.replace("\\", "/")
        raw_uri = f"file:///{drive_letter}:{raw_rest}"
        encoded_uri = f"file:///{drive_letter}%3A{rest_posix}"
    else:
        posix_path = project_dir.replace("\\", "/")
        raw_uri = f"file://{posix_path}"
        encoded_uri = f"file://{urllib.parse.quote(posix_path, safe='/')}"
    return project_dir, raw_uri, encoded_uri


def uri_matches_project(ws_uris: str, raw_uri: str, encoded_uri: str, project_name: str) -> bool:
    if not ws_uris:
        return False
    uris = []
    try:
        parsed = json.loads(ws_uris)
        if isinstance(parsed, list):
            uris = [str(u) for u in parsed if u]
    except Exception:
        uris = [ws_uris]

    raw_low = raw_uri.rstrip("/").lower()
    enc_low = encoded_uri.rstrip("/").lower()
    pname_low = project_name.lower()

    for u in uris:
        u_clean = u.rstrip("/")
        if u_clean.lower() in (raw_low, enc_low):
            return True
        decoded = urllib.parse.unquote(u_clean).rstrip("/")
        base = os.path.basename(decoded)
        if base and base.lower() == pname_low:
            return True
    return False


def discover_project_identity(project_dir: str, capsule_dir: str, explicit_id: str = "", explicit_name: str = "") -> dict:
    """Resolves project_id and project_name using `.agy-capsule/project_id`, `project.json`, or `~/.gemini/config/projects/*.json`."""
    proj_id_file = os.path.join(capsule_dir, "project_id")
    proj_meta_file = os.path.join(capsule_dir, "project.json")
    base_name = explicit_name or os.path.basename(project_dir)

    file_pid = ""
    if os.path.exists(proj_id_file):
        try:
            with open(proj_id_file, "r", encoding="utf-8") as f:
                file_pid = f.read().strip()
        except Exception:
            pass

    json_meta = {}
    if os.path.exists(proj_meta_file):
        try:
            with open(proj_meta_file, "r", encoding="utf-8") as f:
                json_meta = json.load(f)
        except Exception:
            pass

    resolved_pid = explicit_id or file_pid or json_meta.get("project_id", "")
    resolved_name = explicit_name or json_meta.get("project_name", "") or base_name

    if not resolved_pid:
        _, raw_uri, encoded_uri = get_project_paths(project_dir)
        cfg_dir = os.path.expanduser("~/.gemini/config/projects")
        uri_match = None
        name_match = None
        if os.path.exists(cfg_dir):
            for fp in glob.glob(os.path.join(cfg_dir, "*.json")):
                try:
                    with open(fp, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    pid = data.get("id", "")
                    pname = data.get("name", "")
                    if pid in ("default-cli-project", "outside-of-project", ""):
                        continue
                    for res in data.get("projectResources", {}).get("resources", []):
                        u = res.get("folderUri") or res.get("gitFolder", {}).get("folderUri") or ""
                        if u.rstrip("/").lower() in (raw_uri.rstrip("/").lower(), encoded_uri.rstrip("/").lower()):
                            uri_match = (pid, explicit_name or pname or base_name)
                            break
                    if pname.lower() == base_name.lower():
                        name_match = (pid, explicit_name or pname)
                except Exception:
                    continue
        chosen = uri_match or name_match
        if not chosen:
            print(
                f"[ERROR] Could not auto-discover project_id for '{project_dir}'.\n"
                f"        Open the folder once in Antigravity, create {proj_id_file}, or pass --project-id <UUID>."
            )
            sys.exit(1)
        resolved_pid, resolved_name = chosen
        print(f"[OK] Auto-discovered project identity: {resolved_name} ({resolved_pid})")

    meta = {"project_id": resolved_pid, "project_name": resolved_name}
    if file_pid != resolved_pid:
        with open(proj_id_file, "w", encoding="utf-8", newline="\n") as f:
            f.write(resolved_pid + "\n")
    if json_meta.get("project_id") != resolved_pid or json_meta.get("project_name") != resolved_name:
        with open(proj_meta_file, "w", encoding="utf-8", newline="\n") as f:
            json.dump(meta, f, indent=2)
    return meta


def get_agy_dirs(include_cli_read_only: bool = False):
    gemini_root = os.path.expanduser("~/.gemini")
    candidates = []
    if sys.platform == "win32":
        # CRITICAL: Never import into antigravity-cli on Windows Desktop!
        names = ["antigravity", "antigravity-ide"]
        if include_cli_read_only:
            names.append("antigravity-cli")
        for name in names:
            p = os.path.join(gemini_root, name)
            if os.path.exists(p):
                candidates.append((name, p))
        if not candidates:
            candidates.append(("antigravity", os.path.join(gemini_root, "antigravity")))
    else:
        for name in ["antigravity-cli", "antigravity", "antigravity-ide"]:
            p = os.path.join(gemini_root, name)
            if os.path.exists(p):
                candidates.append((name, p))
        if not candidates:
            candidates.append(("antigravity-cli", os.path.join(gemini_root, "antigravity-cli")))
    return gemini_root, candidates


def safe_sqlite_copy(src_db: str, dst_db: str):
    os.makedirs(os.path.dirname(os.path.abspath(dst_db)), exist_ok=True)
    if os.path.exists(dst_db):
        try:
            os.remove(dst_db)
        except OSError:
            pass
    try:
        if not os.path.exists(src_db + "-wal"):
            shutil.copy2(src_db, dst_db)
            return
        src_conn = sqlite3.connect(f"file:{src_db}?mode=ro", uri=True, timeout=30)
        dst_conn = sqlite3.connect(dst_db, timeout=30)
        with dst_conn:
            src_conn.backup(dst_conn)
        dst_conn.close()
        src_conn.close()
    except Exception as e:
        print(f"  [WARN] Could not copy DB {src_db} -> {dst_db}: {e}")


def get_db_step_count(db_path: str) -> int:
    if not os.path.exists(db_path):
        return -1
    try:
        uri = f"file:{db_path}?mode=ro"
        if not os.path.exists(db_path + "-wal"):
            uri += "&immutable=1"
        conn = sqlite3.connect(uri, uri=True, timeout=10)
        cnt = conn.execute("SELECT COUNT(*) FROM steps").fetchone()[0]
        conn.close()
        return cnt
    except Exception:
        return -1


# ---------------------------------------------------------------------------
# Capsule Metadata Load / Save
# ---------------------------------------------------------------------------

def load_all_capsule_summaries(capsule_dir: str) -> dict:
    merged = {}
    summaries_dir = os.path.join(capsule_dir, "summaries")
    os.makedirs(summaries_dir, exist_ok=True)

    for fn in os.listdir(capsule_dir):
        if fn.startswith("summaries") and fn.endswith(".json"):
            fp = os.path.join(capsule_dir, fn)
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    for cid, item in data.items():
                        if isinstance(item, dict) and item.get("step_count", -1) >= merged.get(cid, {}).get("step_count", -1):
                            merged[cid] = item
            except Exception:
                pass
        if ".sync-conflict-" in fn:
            try:
                os.remove(os.path.join(capsule_dir, fn))
            except Exception:
                pass

    for fn in os.listdir(summaries_dir):
        if fn.endswith(".json") and ".sync-conflict-" not in fn:
            fp = os.path.join(summaries_dir, fn)
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    item = json.load(f)
                cid = item.get("conversation_id")
                if cid and item.get("step_count", -1) >= merged.get(cid, {}).get("step_count", -1):
                    merged[cid] = item
            except Exception:
                pass
        elif ".sync-conflict-" in fn:
            try:
                os.remove(os.path.join(summaries_dir, fn))
            except Exception:
                pass

    return merged


def save_all_capsule_summaries(capsule_dir: str, summaries: dict):
    summaries_dir = os.path.join(capsule_dir, "summaries")
    os.makedirs(summaries_dir, exist_ok=True)
    for cid, item in summaries.items():
        per_file = os.path.join(summaries_dir, f"{cid}.json")
        with open(per_file, "w", encoding="utf-8", newline="\n") as f:
            json.dump(item, f, indent=2, ensure_ascii=False)
    summaries_path = os.path.join(capsule_dir, "summaries.json")
    with open(summaries_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(summaries, f, indent=2, ensure_ascii=False)


def is_clean_short_title(t: str, preview: str) -> bool:
    if not t:
        return False
    if "\n" in t or len(t) > 90:
        return False
    if preview and t == preview and len(t) > 60:
        return False
    return True


# ---------------------------------------------------------------------------
# Export / Import / Status Commands
# ---------------------------------------------------------------------------

def cmd_export(args, project_dir: str, capsule_dir: str, meta: dict, only_ids=None):
    project_id = meta["project_id"]
    project_name = meta["project_name"]
    _, raw_uri, encoded_uri = get_project_paths(project_dir)
    adopt_ids = {c.strip().lower() for c in (getattr(args, "adopt_chat", None) or []) if c.strip()}
    adopt_ids |= {c.strip().lower() for c in (getattr(args, "chat_id", None) or []) if c.strip()}
    if only_ids:
        only_ids = {c.strip().lower() for c in only_ids}
        adopt_ids |= only_ids
    gemini_root, agy_dirs = get_agy_dirs(include_cli_read_only=bool(adopt_ids))

    for sub in ("conversations", "summaries", "annotations", "brain"):
        os.makedirs(os.path.join(capsule_dir, sub), exist_ok=True)

    existing_summaries = load_all_capsule_summaries(capsule_dir)
    exported_count = 0

    for app_name, agy_dir in agy_dirs:
        sum_db = os.path.join(agy_dir, "conversation_summaries.db")
        pb_cache = os.path.join(agy_dir, "agyhub_summaries_proto.pb")
        has_conv_dir = os.path.exists(os.path.join(agy_dir, "conversations"))
        if not os.path.exists(sum_db) and not os.path.exists(pb_cache) and not (adopt_ids and has_conv_dir):
            continue

        row_map = {}
        if os.path.exists(sum_db):
            with tempfile.TemporaryDirectory() as tmpdir:
                tmp_sum = os.path.join(tmpdir, "conversation_summaries.db")
                safe_sqlite_copy(sum_db, tmp_sum)
                conn = sqlite3.connect(tmp_sum)
                conn.row_factory = sqlite3.Row
                for r in conn.execute("SELECT * FROM conversation_summaries").fetchall():
                    rd = dict(r)
                    row_map[rd["conversation_id"]] = rd
                conn.close()

        # Also merge entries from agyhub_summaries_proto.pb (Desktop v2 primary summary cache)
        if os.path.exists(pb_cache):
            try:
                with open(pb_cache, "rb") as f:
                    pb_data = f.read()
                for fn, wt, val in parse_msg(pb_data):
                    if fn == 1 and wt == 2:
                        sub = parse_msg(val)
                        cid_k = next((v.decode("utf-8", errors="replace") for sfn, swt, v in sub if sfn == 1 and swt == 2), "")
                        raw_v = next((v for sfn, swt, v in sub if sfn == 2 and swt == 2), b"")
                        if cid_k and raw_v and cid_k not in row_map:
                            pb_pid = ""
                            pb_ws = ""
                            pb_steps = 0
                            for rfn, rwt, rval in parse_msg(raw_v):
                                if rfn == 2 and rwt == 0:
                                    pb_steps = int(rval)
                                elif rfn == 17 and rwt == 2:
                                    for mfn, mwt, mval in parse_msg(rval):
                                        if mfn == 18 and mwt == 2:
                                            pb_pid = mval.decode("utf-8", errors="replace")
                                        elif mfn == 7 and mwt == 2:
                                            pb_ws = json.dumps([mval.decode("utf-8", errors="replace")])
                            mtime_s = extract_mtime_sec_from_raw_summary(raw_v)
                            iso_ts = datetime.datetime.fromtimestamp(mtime_s, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                            row_map[cid_k] = {
                                "conversation_id": cid_k,
                                "title": extract_title_from_raw_summary(raw_v),
                                "preview": extract_preview_from_raw_summary(raw_v),
                                "step_count": pb_steps,
                                "last_modified_time": iso_ts,
                                "status": "CASCADE_RUN_STATUS_IDLE",
                                "source": "",
                                "project_id": pb_pid,
                                "workspace_uris": pb_ws,
                                "raw_summary": raw_v,
                            }
            except Exception as e:
                print(f"  [WARN] Failed reading {pb_cache}: {e}")

        # Synthesize entries for any explicitly adopted chat present as conversations/<cid>.db
        for acid in adopt_ids:
            if any(k.lower() == acid for k in row_map):
                continue
            cand_db = os.path.join(agy_dir, "conversations", f"{acid}.db")
            if os.path.exists(cand_db):
                steps_n = max(0, get_db_step_count(cand_db))
                m_blob = b""
                try:
                    uri_m = f"file:{cand_db}?mode=ro"
                    if not os.path.exists(cand_db + "-wal"):
                        uri_m += "&immutable=1"
                    c0 = sqlite3.connect(uri_m, uri=True)
                    r0 = c0.execute("SELECT data FROM trajectory_metadata_blob WHERE id='main'").fetchone()
                    if r0 and r0[0]:
                        m_blob = r0[0] if isinstance(r0[0], bytes) else b""
                    c0.close()
                except Exception:
                    pass
                pbtxt_p = os.path.join(agy_dir, "annotations", f"{acid}.pbtxt")
                t0 = extract_title_from_pbtxt(pbtxt_p) or f"Chat {acid[:8]}"
                mt_s = int(os.path.getmtime(cand_db))
                iso_t = datetime.datetime.fromtimestamp(mt_s, tz=datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                synth_raw = rewrite_raw_summary_blob(
                    b"", raw_uri, encoded_uri, project_id, acid, t0, t0, step_count=steps_n, meta_blob=m_blob
                )
                row_map[acid] = {
                    "conversation_id": acid,
                    "title": t0,
                    "preview": t0,
                    "step_count": steps_n,
                    "last_modified_time": iso_t,
                    "status": "CASCADE_RUN_STATUS_IDLE",
                    "source": "",
                    "project_id": project_id,
                    "workspace_uris": json.dumps([encoded_uri]),
                    "raw_summary": synth_raw,
                }

        for d in row_map.values():
            cid = d["conversation_id"]
            if only_ids is not None and cid.lower() not in only_ids:
                continue
            pid = d.get("project_id", "")
            ws_uris = d.get("workspace_uris", "")

            is_match = (
                pid == project_id
                or cid.lower() in adopt_ids
                or cid in existing_summaries
                or uri_matches_project(ws_uris, raw_uri, encoded_uri, project_name)
                or (getattr(args, "include_all_local", False) and pid == "default-cli-project")
            )
            if not is_match:
                continue

            src_conv_db = os.path.join(agy_dir, "conversations", f"{cid}.db")
            dst_conv_db = os.path.join(capsule_dir, "conversations", f"{cid}.db")

            local_steps = d.get("step_count", 0)
            if os.path.exists(src_conv_db):
                actual_steps = get_db_step_count(src_conv_db)
                if actual_steps > local_steps:
                    local_steps = actual_steps

            capsule_steps = get_db_step_count(dst_conv_db)
            prev = existing_summaries.get(cid, {})
            prev_steps = prev.get("step_count", -1)
            force_export = getattr(args, "force", False) or (cid.lower() in adopt_ids)

            needs_export = (
                force_export
                or not os.path.exists(dst_conv_db)
                or local_steps > capsule_steps
                or local_steps > prev_steps
            )
            if not needs_export:
                continue

            if os.path.exists(src_conv_db):
                safe_sqlite_copy(src_conv_db, dst_conv_db)

            dst_brain = os.path.join(capsule_dir, "brain", cid)
            brain_sources = []
            ide_brain = os.path.join(gemini_root, "antigravity-ide", "brain", cid)
            cur_brain = os.path.join(agy_dir, "brain", cid)
            if os.path.exists(ide_brain) and os.path.normcase(ide_brain) != os.path.normcase(cur_brain):
                brain_sources.append(ide_brain)
            if os.path.exists(cur_brain):
                brain_sources.append(cur_brain)

            for src_brain in brain_sources:
                for root, _, files in os.walk(src_brain):
                    rel = os.path.relpath(root, src_brain)
                    target_root = os.path.join(dst_brain, rel) if rel != "." else dst_brain
                    os.makedirs(target_root, exist_ok=True)
                    for fn in files:
                        s_file = os.path.join(root, fn)
                        d_file = os.path.join(target_root, fn)
                        try:
                            if not os.path.exists(d_file) or os.path.getmtime(s_file) > os.path.getmtime(d_file):
                                shutil.copy2(s_file, d_file)
                        except Exception:
                            pass

            raw_bytes = d["raw_summary"] if isinstance(d.get("raw_summary"), (bytes, bytearray)) else b""
            pb_title = extract_title_from_raw_summary(raw_bytes)
            pb_preview = extract_preview_from_raw_summary(raw_bytes)
            local_pbtxt = os.path.join(agy_dir, "annotations", f"{cid}.pbtxt")
            capsule_pbtxt = os.path.join(capsule_dir, "annotations", f"{cid}.pbtxt")
            pbtxt_title = extract_title_from_pbtxt(local_pbtxt) or extract_title_from_pbtxt(capsule_pbtxt)

            prev_title = prev.get("title", "")
            preview_str = d.get("preview") or pb_preview or prev.get("preview", "")

            candidates = [
                d.get("title", "") if is_clean_short_title(d.get("title", ""), preview_str) else "",
                pbtxt_title if is_clean_short_title(pbtxt_title, preview_str) else "",
                prev_title if is_clean_short_title(prev_title, preview_str) else "",
                pb_title if is_clean_short_title(pb_title, preview_str) else "",
                make_short_fallback_title(preview_str, cid),
            ]
            resolved_title = next((c for c in candidates if c), f"Chat {cid[:8]}")

            if raw_bytes:
                raw_bytes = rewrite_raw_summary_blob(
                    raw_bytes, raw_uri, encoded_uri, project_id, cid, resolved_title, preview_str
                )
            raw_hex = raw_bytes.hex() if raw_bytes else prev.get("raw_summary_hex", "")
            mtime_sec = extract_mtime_sec_from_raw_summary(raw_bytes)
            write_annotation_pbtxt(capsule_pbtxt, resolved_title, mtime_sec)

            existing_summaries[cid] = {
                "conversation_id": cid,
                "title": resolved_title,
                "preview": preview_str or resolved_title,
                "step_count": max(local_steps, d.get("step_count", 0)),
                "last_modified_time": d.get("last_modified_time", ""),
                "status": d.get("status", "CASCADE_RUN_STATUS_IDLE"),
                "source": "",
                "project_id": project_id,
                "agent_name": d.get("agent_name", ""),
                "parent_conversation_id": d.get("parent_conversation_id", ""),
                "nesting_depth": d.get("nesting_depth", 0),
                "battle_id": d.get("battle_id", ""),
                "winning_conversation_id": d.get("winning_conversation_id", ""),
                "not_fully_idle": d.get("not_fully_idle", 0),
                "killed": d.get("killed", 0),
                "last_user_input_time": d.get("last_user_input_time", ""),
                "last_user_input_step_index": d.get("last_user_input_step_index", -1),
                "group_id": d.get("group_id", ""),
                "raw_summary_hex": raw_hex,
                "exported_from_host": platform.node(),
                "exported_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }
            exported_count += 1

    if exported_count > 0 or getattr(args, "force", False):
        save_all_capsule_summaries(capsule_dir, existing_summaries)
        manifest = {
            "project_id": project_id,
            "project_name": project_name,
            "last_export_host": platform.node(),
            "last_export_time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "conversation_count": len(existing_summaries),
            "conversations": [
                {
                    "id": k,
                    "title": v.get("title", ""),
                    "step_count": v.get("step_count", 0),
                    "last_modified_time": v.get("last_modified_time", ""),
                    "exported_from_host": v.get("exported_from_host", ""),
                }
                for k, v in sorted(
                    existing_summaries.items(),
                    key=lambda x: x[1].get("last_modified_time", ""),
                    reverse=True,
                )
            ],
        }
        with open(os.path.join(capsule_dir, "manifest.json"), "w", encoding="utf-8", newline="\n") as f:
            json.dump(manifest, f, indent=2, ensure_ascii=False)

    print(f"[OK] [{project_name}] Exported {exported_count} updated chat(s) into {capsule_dir} (total: {len(existing_summaries)})")


def ensure_project_binding(gemini_root: str, agy_dirs, project_id: str, project_name: str, project_dir: str, encoded_uri: str):
    proj_cfg_dir = os.path.join(gemini_root, "config", "projects")
    os.makedirs(proj_cfg_dir, exist_ok=True)
    proj_cfg_file = os.path.join(proj_cfg_dir, f"{project_id}.json")

    is_git = os.path.exists(os.path.join(project_dir, ".git"))
    res_entry = {"gitFolder": {"folderUri": encoded_uri}} if is_git else {"folderUri": encoded_uri}

    cfg = {
        "id": project_id,
        "name": project_name,
        "projectResources": {"resources": [res_entry]},
        "permissionGrants": {"v2Migrated": True},
        "settings": {},
        "isWorkspaceOnly": False,
    }
    if os.path.exists(proj_cfg_file):
        try:
            with open(proj_cfg_file, "r", encoding="utf-8") as f:
                existing = json.load(f)
            existing["name"] = project_name
            existing["projectResources"] = {"resources": [res_entry]}
            cfg = existing
        except Exception:
            pass

    with open(proj_cfg_file, "w", encoding="utf-8", newline="\n") as f:
        json.dump(cfg, f, indent=2)
    print(f"[OK] Registered project definition: {proj_cfg_file} -> {encoded_uri}")

    for _, agy_dir in agy_dirs:
        settings_path = os.path.join(agy_dir, "settings.json")
        if os.path.exists(settings_path):
            try:
                with open(settings_path, "r", encoding="utf-8") as f:
                    sdata = json.load(f)
                tw = sdata.get("trustedWorkspaces", [])
                parent_dir = os.path.dirname(project_dir)
                for p in (project_dir, parent_dir):
                    if p not in tw:
                        tw.append(p)
                sdata["trustedWorkspaces"] = tw
                with open(settings_path, "w", encoding="utf-8", newline="\n") as f:
                    json.dump(sdata, f, indent=2)
            except Exception:
                pass


def cmd_import(args, project_dir: str, capsule_dir: str, meta: dict, only_ids=None):
    project_id = meta["project_id"]
    project_name = meta["project_name"]
    project_dir, raw_uri, encoded_uri = get_project_paths(project_dir)
    gemini_root, agy_dirs = get_agy_dirs()

    if only_ids is not None:
        only_ids = {c.strip().lower() for c in only_ids}

    capsule_summaries = load_all_capsule_summaries(capsule_dir)
    if not capsule_summaries:
        print(f"[WARN] [{project_name}] No capsule summaries found in {capsule_dir}. Skipping import.")
        return

    service_stopped = False
    if getattr(args, "restart_service", False) and sys.platform.startswith("linux") and shutil.which("systemctl"):
        try:
            subprocess.run(["systemctl", "--user", "stop", "antigravity-cli-daemon.service"], check=False)
            service_stopped = True
            print("[OK] Stopped antigravity-cli-daemon.service for clean SQLite import")
        except Exception as e:
            print(f"[WARN] Could not stop antigravity-cli-daemon.service: {e}")

    try:
        ensure_project_binding(gemini_root, agy_dirs, project_id, project_name, project_dir, encoded_uri)

        target_sum_dbs = 0
        for app_name, agy_dir in agy_dirs:
            sum_db = os.path.join(agy_dir, "conversation_summaries.db")
            if not os.path.exists(sum_db):
                continue
            target_sum_dbs += 1

            for sub in ("conversations", "annotations", "brain"):
                os.makedirs(os.path.join(agy_dir, sub), exist_ok=True)

            conn_sum = sqlite3.connect(sum_db, timeout=30)
            cur_sum = conn_sum.cursor()
            has_fts = bool(
                cur_sum.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='conversations_fts'"
                ).fetchone()
            )

            imported_count = 0
            updated_proto_summaries = {}
            updated_proto_mtimes = {}

            for cid, sinfo in capsule_summaries.items():
                if only_ids is not None and cid.lower() not in only_ids:
                    continue

                src_conv_db = os.path.join(capsule_dir, "conversations", f"{cid}.db")
                dst_conv_db = os.path.join(agy_dir, "conversations", f"{cid}.db")

                # Guard against incomplete transfer (missing .db or temporary sync file)
                if not os.path.exists(src_conv_db):
                    print(f"  [SKIP] {cid[:8]}: conversations/{cid}.db not yet present in capsule (transfer incomplete?)")
                    continue

                capsule_steps = get_db_step_count(src_conv_db)
                local_steps = get_db_step_count(dst_conv_db)
                force_import = getattr(args, "force", False) or (only_ids is not None)

                needs_import = (
                    force_import
                    or not os.path.exists(dst_conv_db)
                    or capsule_steps > local_steps
                )
                if not needs_import:
                    continue

                if not os.path.exists(dst_conv_db) or capsule_steps > local_steps:
                    safe_sqlite_copy(src_conv_db, dst_conv_db)

                # On Windows Desktop, remove any duplicate of this cid in antigravity-cli/conversations
                # so Desktop language_server does not mark it CORTEX_TRAJECTORY_SOURCE_CLI and hide it.
                if sys.platform == "win32":
                    cli_conv_dir = os.path.join(gemini_root, "antigravity-cli", "conversations")
                    if os.path.exists(cli_conv_dir):
                        for f in glob.glob(os.path.join(cli_conv_dir, f"{cid}.db*")):
                            try:
                                os.remove(f)
                            except OSError:
                                pass

                local_pbtxt = os.path.join(agy_dir, "annotations", f"{cid}.pbtxt")
                capsule_pbtxt = os.path.join(capsule_dir, "annotations", f"{cid}.pbtxt")
                pbtxt_title = extract_title_from_pbtxt(local_pbtxt) or extract_title_from_pbtxt(capsule_pbtxt)
                preview_str = sinfo.get("preview", "")
                sinfo_title = sinfo.get("title", "")

                if is_clean_short_title(sinfo_title, preview_str):
                    title = sinfo_title
                elif is_clean_short_title(pbtxt_title, preview_str):
                    title = pbtxt_title
                else:
                    title = make_short_fallback_title(preview_str, cid)

                raw_bytes = bytes.fromhex(sinfo["raw_summary_hex"]) if sinfo.get("raw_summary_hex") else b""
                if raw_bytes:
                    raw_bytes = rewrite_raw_summary_blob(
                        raw_bytes, raw_uri, encoded_uri, project_id, cid, title, preview_str
                    )
                mtime_sec = extract_mtime_sec_from_raw_summary(raw_bytes)

                write_annotation_pbtxt(local_pbtxt, title, mtime_sec)
                write_annotation_pbtxt(capsule_pbtxt, title, mtime_sec)

                if os.path.exists(dst_conv_db):
                    try:
                        cconn = sqlite3.connect(dst_conv_db, timeout=20)
                        row = cconn.execute("SELECT data FROM trajectory_metadata_blob WHERE id='main'").fetchone()
                        if row and row[0]:
                            new_blob = rewrite_trajectory_metadata_blob(
                                row[0], raw_uri, encoded_uri, project_id, cid
                            )
                            cconn.execute("UPDATE trajectory_metadata_blob SET data = ? WHERE id='main'", (new_blob,))
                            cconn.commit()
                            cconn.execute("PRAGMA wal_checkpoint(FULL);")
                        cconn.close()
                        if mtime_sec > 0:
                            os.utime(dst_conv_db, (mtime_sec, mtime_sec))
                        updated_proto_mtimes[cid] = int(os.path.getmtime(dst_conv_db))
                    except Exception as e:
                        print(f"  [WARN] Failed to update trajectory_metadata_blob for {cid}: {e}")

                if raw_bytes:
                    updated_proto_summaries[cid] = raw_bytes

                src_brain = os.path.join(capsule_dir, "brain", cid)
                dst_brain = os.path.join(agy_dir, "brain", cid)
                if os.path.exists(src_brain):
                    for root, _, files in os.walk(src_brain):
                        rel = os.path.relpath(root, src_brain)
                        target_root = os.path.join(dst_brain, rel) if rel != "." else dst_brain
                        os.makedirs(target_root, exist_ok=True)
                        for fn in files:
                            s_file = os.path.join(root, fn)
                            d_file = os.path.join(target_root, fn)
                            try:
                                if not os.path.exists(d_file) or os.path.getmtime(s_file) > os.path.getmtime(d_file):
                                    shutil.copy2(s_file, d_file)
                            except Exception:
                                pass

                ws_json = json.dumps([encoded_uri])
                cur_sum.execute(
                    """
                    INSERT INTO conversation_summaries (
                        conversation_id, title, preview, step_count, last_modified_time,
                        workspace_uris, status, source, project_id, agent_name,
                        parent_conversation_id, nesting_depth, battle_id, winning_conversation_id,
                        not_fully_idle, killed, last_user_input_time, last_user_input_step_index,
                        app_data_dir, raw_summary, group_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(conversation_id) DO UPDATE SET
                        title = excluded.title,
                        preview = excluded.preview,
                        step_count = MAX(conversation_summaries.step_count, excluded.step_count),
                        last_modified_time = MAX(conversation_summaries.last_modified_time, excluded.last_modified_time),
                        workspace_uris = excluded.workspace_uris,
                        status = excluded.status,
                        source = '',
                        project_id = excluded.project_id,
                        app_data_dir = excluded.app_data_dir,
                        raw_summary = excluded.raw_summary
                    """,
                    (
                        cid,
                        title,
                        preview_str or title,
                        max(capsule_steps, sinfo.get("step_count", 0)),
                        sinfo.get("last_modified_time", ""),
                        ws_json,
                        sinfo.get("status", "CASCADE_RUN_STATUS_IDLE"),
                        "",
                        project_id,
                        sinfo.get("agent_name", ""),
                        sinfo.get("parent_conversation_id", ""),
                        sinfo.get("nesting_depth", 0),
                        sinfo.get("battle_id", ""),
                        sinfo.get("winning_conversation_id", ""),
                        sinfo.get("not_fully_idle", 0),
                        sinfo.get("killed", 0),
                        sinfo.get("last_user_input_time", sinfo.get("last_modified_time", "")),
                        sinfo.get("last_user_input_step_index", -1),
                        app_name,
                        raw_bytes,
                        sinfo.get("group_id", ""),
                    ),
                )

                if has_fts:
                    exists_fts = cur_sum.execute(
                        "SELECT 1 FROM conversations_fts WHERE conversation_id = ?", (cid,)
                    ).fetchone()
                    if exists_fts:
                        cur_sum.execute(
                            "UPDATE conversations_fts SET project = ?, workspace = ?, title = ? WHERE conversation_id = ?",
                            (project_name, encoded_uri, title, cid),
                        )
                    else:
                        cur_sum.execute(
                            """
                            INSERT INTO conversations_fts (
                                conversation_id, chunk_index, chunk_first_step, chunk_last_step,
                                last_modified_time, step_count, last_step_created_at, archived,
                                user_step_byte_offsets, agent_step_byte_offsets,
                                title, workspace, project, user_body, agent_body
                            ) VALUES (?, 0, 0, ?, ?, ?, ?, 0, '', '', ?, ?, ?, '', '')
                            """,
                            (
                                cid,
                                max(capsule_steps, sinfo.get("step_count", 0)),
                                sinfo.get("last_modified_time", ""),
                                max(capsule_steps, sinfo.get("step_count", 0)),
                                sinfo.get("last_modified_time", ""),
                                title,
                                encoded_uri,
                                project_name,
                            ),
                        )

                imported_count += 1

            conn_sum.commit()
            cur_sum.execute("PRAGMA wal_checkpoint(FULL);")
            conn_sum.close()

            if imported_count > 0 or getattr(args, "force", False):
                for cache_name in ("agyhub_summaries_proto.pb", "jetbox_summaries_proto.pb"):
                    cpath = os.path.join(agy_dir, cache_name)
                    if os.path.exists(cpath) or cache_name == "agyhub_summaries_proto.pb":
                        update_summaries_proto_cache(cpath, updated_proto_summaries, updated_proto_mtimes)

                pushed_daemons = push_live_summaries_to_daemon(agy_dir, updated_proto_summaries)
                if pushed_daemons:
                    time.sleep(0.5)
                    pb_path = os.path.join(agy_dir, "agyhub_summaries_proto.pb")
                    if os.path.exists(pb_path):
                        try:
                            with open(pb_path, "rb") as f:
                                pb_fields = parse_msg(f.read())
                            for fn, wt, val in pb_fields:
                                if fn == 2 and wt == 2:
                                    mf = parse_msg(val)
                                    k = next((v.decode("utf-8", errors="ignore") for f, w, v in mf if f == 1), "")
                                    v = next((v for f, w, v in mf if f == 2), 0)
                                    dbp = os.path.join(agy_dir, "conversations", f"{k}.db")
                                    if k and v > 0 and os.path.exists(dbp) and not os.path.exists(dbp + "-wal"):
                                        os.utime(dbp, (v, v))
                        except Exception:
                            pass
                    print(f"[OK] Pushed live summary updates to {pushed_daemons} running language_server daemon(s)")

            print(f"[OK] [{project_name}] Imported & relinked {imported_count} updated chat(s) into {agy_dir} ({encoded_uri})")

        if target_sum_dbs == 0:
            print(
                "[WARN] No initialized conversation_summaries.db found in ~/.gemini/<app>/.\n"
                "       Launch Antigravity once on this machine first so it creates its local database, then re-run import."
            )

    finally:
        if service_stopped:
            subprocess.run(["systemctl", "--user", "start", "antigravity-cli-daemon.service"], check=False)
            print("[OK] Restarted antigravity-cli-daemon.service")


def cmd_status(project_dir: str, capsule_dir: str, meta: dict):
    _, raw_uri, encoded_uri = get_project_paths(project_dir)
    print(f"Project:       {meta['project_name']} ({meta['project_id']})")
    print(f"Directory:     {project_dir}")
    print(f"Capsule Dir:   {capsule_dir}")
    print(f"Raw URI:       {raw_uri}")
    print(f"Encoded URI:   {encoded_uri}")
    manifest_path = os.path.join(capsule_dir, "manifest.json")
    if os.path.exists(manifest_path):
        with open(manifest_path, "r", encoding="utf-8") as f:
            m = json.load(f)
        print(f"Capsule Sync:  {m.get('conversation_count', 0)} chats (last exported from {m.get('last_export_host')} at {m.get('last_export_time')})")
        for c in m.get("conversations", []):
            print(f"  - {c['id'][:8]} | steps: {str(c['step_count']):>5} | {c['title']} ({c.get('exported_from_host', '')})")
    else:
        print("Capsule Sync:  Not exported yet")


def main():
    parser = argparse.ArgumentParser(description="Universal Antigravity Conversation & Project Capsule Sync")
    parser.add_argument(
        "action",
        choices=["export", "import", "sync", "move-chat", "relink-project", "status"],
        help="Action to run",
    )
    parser.add_argument("--project-dir", required=False, default=os.getcwd(), help="Target project root directory")
    parser.add_argument("--capsule-dir", default="", help="Optional capsule directory (defaults to <project-dir>/.agy-capsule)")
    parser.add_argument("--project-id", default="", help="Explicit Project UUID (auto-discovered if omitted)")
    parser.add_argument("--project-name", default="", help="Explicit Project Name (auto-discovered if omitted)")
    parser.add_argument("--adopt-chat", action="append", default=[], help="Conversation UUID(s) to adopt into this project during export (repeatable)")
    parser.add_argument("--chat-id", action="append", default=[], help="Conversation UUID(s) to move/adopt for move-chat (repeatable)")
    parser.add_argument("--include-all-local", action="store_true", help="Include default-cli-project chats during export")
    parser.add_argument("--restart-service", action="store_true", help="Stop/start antigravity-cli-daemon.service during import on Linux")
    parser.add_argument("--force", action="store_true", help="Force overwrite during export/import")
    args = parser.parse_args()

    project_dir = os.path.abspath(args.project_dir)
    if not os.path.exists(project_dir):
        print(f"[ERROR] Project directory does not exist: {project_dir}")
        sys.exit(1)

    capsule_dir = os.path.abspath(args.capsule_dir) if args.capsule_dir else os.path.join(project_dir, ".agy-capsule")
    os.makedirs(capsule_dir, exist_ok=True)

    meta = discover_project_identity(project_dir, capsule_dir, args.project_id, args.project_name)

    if args.action == "export":
        cmd_export(args, project_dir, capsule_dir, meta)
    elif args.action == "import":
        cmd_import(args, project_dir, capsule_dir, meta)
    elif args.action == "sync":
        cmd_export(args, project_dir, capsule_dir, meta)
        cmd_import(args, project_dir, capsule_dir, meta)
    elif args.action == "move-chat":
        target_cids = [c.strip() for c in (args.chat_id + args.adopt_chat) if c.strip()]
        if not target_cids:
            print("[ERROR] move-chat requires at least one --chat-id <CONVERSATION_UUID>.")
            sys.exit(1)
        args.force = True
        cmd_export(args, project_dir, capsule_dir, meta, only_ids=target_cids)
        cmd_import(args, project_dir, capsule_dir, meta, only_ids=target_cids)
    elif args.action == "relink-project":
        args.force = True
        cmd_export(args, project_dir, capsule_dir, meta)
        cmd_import(args, project_dir, capsule_dir, meta)
    elif args.action == "status":
        cmd_status(project_dir, capsule_dir, meta)


if __name__ == "__main__":
    main()
