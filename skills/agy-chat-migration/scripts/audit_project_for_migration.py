#!/usr/bin/env python3
"""
audit_project_for_migration.py - Read-Only Pre-Flight Scanner & Orphan Chat Finder

Scans an existing Antigravity project before moving, relinking, or migrating it
to another folder or machine (Windows / Linux / macOS). Can also list unbound
"Outside of Project", legacy Antigravity IDE (`antigravity-ide`), and broken-path
conversations ready to be adopted via `agy_capsule_sync.py move-chat`.

Usage:
  python audit_project_for_migration.py "<ProjectNameOrPath>"
  python audit_project_for_migration.py --list-orphan-chats
"""

import argparse
import glob
import json
import os
import re
import sqlite3
import subprocess
import sys
import urllib.parse

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

STDLIB_MODULES = set(sys.stdlib_module_names) if hasattr(sys, "stdlib_module_names") else {
    "abc", "argparse", "asyncio", "base64", "collections", "concurrent", "contextlib", "copy",
    "csv", "ctypes", "dataclasses", "datetime", "decimal", "difflib", "email", "enum", "errno",
    "fnmatch", "fractions", "functools", "gc", "getpass", "glob", "gzip", "hashlib", "heapq",
    "hmac", "html", "http", "importlib", "inspect", "io", "ipaddress", "itertools", "json",
    "logging", "lzma", "math", "mimetypes", "multiprocessing", "numbers", "operator", "os",
    "pathlib", "pickle", "platform", "plistlib", "pprint", "queue", "quopri", "random", "re",
    "secrets", "select", "selectors", "shlex", "shutil", "signal", "socket", "sqlite3", "ssl",
    "stat", "statistics", "string", "struct", "subprocess", "sys", "tarfile", "tempfile",
    "textwrap", "threading", "time", "timeit", "tomllib", "traceback", "tracemalloc", "types",
    "typing", "unicodedata", "unittest", "urllib", "uuid", "venv", "warnings", "wave", "weakref",
    "winreg", "xml", "xmlrpc", "zipfile", "zlib", "__future__",
}


def decode_uri_to_path(uri: str) -> str:
    if not uri.startswith("file://"):
        return uri
    p = urllib.parse.unquote(uri[len("file://"):])
    if sys.platform == "win32" and p.startswith("/") and len(p) > 2 and p[2] == ":":
        p = p[1:]
    return os.path.normpath(p)


def load_all_registered_projects():
    gemini_cfg = os.path.expanduser("~/.gemini/config/projects")
    projects = {}
    if not os.path.exists(gemini_cfg):
        return projects
    for fp in glob.glob(os.path.join(gemini_cfg, "*.json")):
        try:
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)
            pid = data.get("id", "")
            name = data.get("name", "")
            uris = []
            for res in data.get("projectResources", {}).get("resources", []):
                u = res.get("folderUri") or res.get("gitFolder", {}).get("folderUri")
                if u:
                    uris.append(u)
            if pid:
                projects[pid] = {"file": fp, "id": pid, "name": name, "uris": uris}
        except Exception:
            continue
    return projects


def find_project_registration(query: str):
    projects = load_all_registered_projects()
    matches = []
    q_norm = os.path.normcase(os.path.abspath(query)) if os.path.exists(query) else None
    q_low = query.lower()

    for pid, pinfo in projects.items():
        name = pinfo["name"]
        uris = pinfo["uris"]
        matched = False
        if q_low in (pid.lower(), name.lower()):
            matched = True
        for u in uris:
            p = os.path.normcase(decode_uri_to_path(u))
            if q_norm and p == q_norm:
                matched = True
            elif q_low in u.lower():
                matched = True
        if matched:
            matches.append((pinfo["file"], pid, name, uris))
    return matches


def extract_pbtxt_title(pbtxt_path: str) -> str:
    if not os.path.exists(pbtxt_path):
        return ""
    try:
        with open(pbtxt_path, "r", encoding="utf-8", errors="replace") as f:
            txt = f.read()
        m = re.search(r'title:\s*"((?:[^"\\]|\\.)*)"', txt)
        return m.group(1) if m else ""
    except Exception:
        return ""


def dir_size(path: str) -> int:
    total = 0
    if os.path.exists(path):
        for r, _, files in os.walk(path):
            for fn in files:
                try:
                    total += os.path.getsize(os.path.join(r, fn))
                except OSError:
                    pass
    return total


def uri_matches_name_or_dir(ws_uris: str, project_name: str, project_dir: str) -> bool:
    if not ws_uris:
        return False
    try:
        parsed = json.loads(ws_uris)
        uris = parsed if isinstance(parsed, list) else [ws_uris]
    except Exception:
        uris = [ws_uris]
    dir_norm = os.path.normcase(os.path.abspath(project_dir))
    for u in uris:
        if not u:
            continue
        p = os.path.normcase(decode_uri_to_path(str(u)))
        if p == dir_norm:
            return True
        if os.path.basename(p).lower() == project_name.lower():
            return True
    return False


def scan_codebase(project_dir: str):
    win_refs = []
    external_imports = set()
    local_top_dirs = {
        d.split(".")[0]
        for d in os.listdir(project_dir)
        if not d.startswith(".")
    } if os.path.exists(project_dir) else set()

    win_pattern = re.compile(r'([A-Za-z]:\\[A-Za-z0-9_\\.-]+|powershell\.exe|cmd\.exe|winreg)', re.IGNORECASE)
    imp_pattern = re.compile(r'^\s*(?:import|from)\s+([a-zA-Z_][a-zA-Z0-9_]*)')

    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in {".git", ".agy-capsule", "__pycache__", "node_modules", ".venv", "venv", ".pytest_cache"}]
        for fn in files:
            if not fn.endswith((".py", ".sh", ".cmd", ".bat", ".ps1", ".yaml", ".yml")):
                continue
            fp = os.path.join(root, fn)
            rel = os.path.relpath(fp, project_dir)
            try:
                with open(fp, "r", encoding="utf-8", errors="ignore") as f:
                    for idx, line in enumerate(f, 1):
                        m = win_pattern.search(line)
                        if m and not rel.endswith(("AGENTS.md", "GEMINI.md", "README.md")):
                            win_refs.append((rel, idx, m.group(0), line.strip()[:100]))
                        if fn.endswith(".py"):
                            im = imp_pattern.match(line)
                            if im:
                                mod = im.group(1)
                                if mod not in STDLIB_MODULES and mod not in local_top_dirs:
                                    external_imports.add(mod)
            except Exception:
                pass
    return win_refs, sorted(external_imports)


def list_orphan_chats():
    gemini_root = os.path.expanduser("~/.gemini")
    projects = load_all_registered_projects()
    valid_pids = {
        pid for pid in projects
        if pid not in ("", "default-cli-project", "outside-of-project")
    }

    print("=" * 84)
    print("  ANTIGRAVITY ORPHAN / UNBOUND / LEGACY IDE CONVERSATIONS SCANNER")
    print("=" * 84)

    findings = []
    seen = set()

    for app in ("antigravity", "antigravity-ide", "antigravity-cli"):
        sum_db = os.path.join(gemini_root, app, "conversation_summaries.db")
        if not os.path.exists(sum_db):
            continue
        uri = f"file:{sum_db}?mode=ro"
        if not os.path.exists(sum_db + "-wal"):
            uri += "&immutable=1"
        conn = sqlite3.connect(uri, uri=True)
        rows = conn.execute(
            "SELECT conversation_id, title, preview, step_count, project_id, workspace_uris FROM conversation_summaries"
        ).fetchall()
        conn.close()

        for cid, title, preview, steps, pid, ws_uris in rows:
            reasons = []
            if not pid or pid in ("outside-of-project", "default-cli-project"):
                reasons.append("OUTSIDE_OF_PROJECT" if pid != "default-cli-project" else "DEFAULT_CLI_PROJECT")
            elif pid not in valid_pids:
                reasons.append("UNKNOWN_PROJECT_ID")

            parsed_uris = []
            if ws_uris:
                try:
                    u_list = json.loads(ws_uris)
                    parsed_uris = u_list if isinstance(u_list, list) else [ws_uris]
                except Exception:
                    parsed_uris = [ws_uris]
            if parsed_uris:
                if not any(os.path.exists(decode_uri_to_path(str(u))) for u in parsed_uris if str(u).startswith("file://")):
                    reasons.append("MISSING_WORKSPACE_PATH")

            if app == "antigravity-ide":
                ide_brain = os.path.join(gemini_root, "antigravity-ide", "brain", cid)
                desk_brain = os.path.join(gemini_root, "antigravity", "brain", cid)
                if os.path.exists(ide_brain) and not os.path.exists(desk_brain):
                    reasons.append("IDE_BRAIN_NOT_MIGRATED")
                elif not reasons:
                    reasons.append("LEGACY_IDE_CHAT")

            if not reasons:
                continue

            conv_db = os.path.join(gemini_root, app, "conversations", f"{cid}.db")
            pbtxt = os.path.join(gemini_root, app, "annotations", f"{cid}.pbtxt")
            db_sz = os.path.getsize(conv_db) if os.path.exists(conv_db) else 0
            eff_title = title or extract_pbtxt_title(pbtxt) or ((preview or "").splitlines()[0][:55] if preview else "<UNTITLED>")

            key = (app, cid)
            if key in seen:
                continue
            seen.add(key)
            findings.append((steps or 0, app, cid, db_sz, ",".join(reasons), eff_title, parsed_uris[0] if parsed_uris else ""))

    if not findings:
        print("No orphan, unbound, broken-path, or unmigrated IDE conversations found.")
        return

    findings.sort(reverse=True)
    print(f"Found {len(findings)} candidate conversation(s):\n")
    for steps, app, cid, db_sz, reason, title, wuri in findings:
        print(f"  * {cid} | {app:<15} | steps={steps:>5} | {db_sz/1024/1024:>6.2f} MB | {reason}")
        print(f"    Title: {title}")
        if wuri:
            print(f"    URI:   {wuri}")
    print("\nTo move/adopt a chat into a target project:")
    print('  python agy_capsule_sync.py move-chat --project-dir "/path/to/project" --chat-id "<UUID>"')


def main():
    parser = argparse.ArgumentParser(description="Read-only audit of an Antigravity project before moving or migrating")
    parser.add_argument("project", nargs="?", default="", help="Project name, UUID, or path")
    parser.add_argument("--list-orphan-chats", action="store_true", help="List Outside-of-Project, broken-path, and legacy IDE chats")
    parser.add_argument("--target-win-root", default="", help="Optional target Windows parent directory to preview target URI")
    parser.add_argument("--target-linux-root", default="", help="Optional target Linux/macOS parent directory to preview target URI")
    args = parser.parse_args()

    if args.list_orphan_chats:
        list_orphan_chats()
        if not args.project:
            return
        print()

    if not args.project:
        parser.print_help()
        sys.exit(1)

    print("=" * 74)
    print(f"  ANTIGRAVITY PROJECT MIGRATION PRE-FLIGHT AUDIT: {args.project}")
    print("=" * 74)

    matches = find_project_registration(args.project)
    project_dir = os.path.abspath(args.project) if os.path.exists(args.project) else None
    project_id = None
    project_name = None

    if matches:
        cfg_fp, project_id, project_name, uris = matches[0]
        print("[1] Project Registration Found:")
        print(f"    - Config File:  {cfg_fp}")
        print(f"    - Project Name: {project_name}")
        print(f"    - Project ID:   {project_id}")
        print(f"    - Current URIs: {uris}")
        if not project_dir and uris:
            cand = decode_uri_to_path(uris[0])
            if os.path.exists(cand):
                project_dir = cand
    else:
        print("[!] No matching ~/.gemini/config/projects/*.json found for query.")

    if not project_dir or not os.path.exists(project_dir):
        print(f"[ERROR] Project directory not found on disk: {project_dir}")
        sys.exit(1)

    if not project_name:
        project_name = os.path.basename(project_dir)

    print(f"    - Local Path:   {project_dir}")
    if args.target_win_root:
        print(f"    - Target Win:   {os.path.join(args.target_win_root, project_name)}")
    if args.target_linux_root:
        print(f"    - Target POSIX: {args.target_linux_root.rstrip('/')}/{project_name}")

    # 2. Scan Conversations
    print("\n[2] Conversations & Artifacts Audit:")
    gemini_root = os.path.expanduser("~/.gemini")
    total_db_bytes = 0
    total_brain_bytes = 0
    untitled = []
    open_wals = []
    found_chats = {}

    for app in ("antigravity", "antigravity-ide", "antigravity-cli"):
        sum_db = os.path.join(gemini_root, app, "conversation_summaries.db")
        if not os.path.exists(sum_db):
            continue
        uri = f"file:{sum_db}?mode=ro"
        if not os.path.exists(sum_db + "-wal"):
            uri += "&immutable=1"
        conn = sqlite3.connect(uri, uri=True)
        rows = conn.execute(
            "SELECT conversation_id, title, preview, step_count, project_id, workspace_uris FROM conversation_summaries"
        ).fetchall()
        conn.close()
        for cid, title, preview, steps, pid, ws_uris in rows:
            if (project_id and pid == project_id) or uri_matches_name_or_dir(ws_uris, project_name, project_dir):
                if cid in found_chats:
                    continue
                conv_db = os.path.join(gemini_root, app, "conversations", f"{cid}.db")
                pbtxt = os.path.join(gemini_root, app, "annotations", f"{cid}.pbtxt")
                brain_dir = os.path.join(gemini_root, app, "brain", cid)
                db_sz = os.path.getsize(conv_db) if os.path.exists(conv_db) else 0
                br_sz = dir_size(brain_dir)
                total_db_bytes += db_sz
                total_brain_bytes += br_sz
                pb_title = extract_pbtxt_title(pbtxt)
                eff_title = title or pb_title
                if not eff_title:
                    untitled.append((cid, steps, (preview or "").splitlines()[0][:60] if preview else ""))
                if os.path.exists(conv_db + "-wal"):
                    open_wals.append(cid)
                found_chats[cid] = (steps or 0, db_sz, br_sz, eff_title or "<UNTITLED>", app)

    print(f"    - Total Chats:      {len(found_chats)}")
    print(f"    - Total .db Size:   {total_db_bytes / 1024 / 1024:.2f} MB")
    print(f"    - Total brain Size: {total_brain_bytes / 1024 / 1024:.2f} MB")
    for cid, (steps, dbsz, brsz, t, app) in sorted(found_chats.items(), key=lambda x: x[1][0], reverse=True):
        big_flag = " [LARGE >50MB: transfer atomically before import]" if dbsz > 50 * 1024 * 1024 else ""
        print(f"      * {cid[:8]} ({app}) | steps={steps:>5} | db={dbsz/1024/1024:>6.2f} MB | brain={brsz/1024/1024:>5.2f} MB | {t}{big_flag}")

    if untitled:
        print(f"    - [WARN] {len(untitled)} chat(s) lack a short title in annotations/<cid>.pbtxt (agy_capsule_sync.py will auto-title them from preview)")
    if open_wals:
        print(f"    - [INFO] {len(open_wals)} chat(s) currently open in WAL mode (Online Backup API will be used): {open_wals}")

    # Check CLI trap folder on Windows
    cli_conv = os.path.join(gemini_root, "antigravity-cli", "conversations")
    if sys.platform == "win32" and os.path.exists(cli_conv):
        dups = [c for c in found_chats if os.path.exists(os.path.join(cli_conv, f"{c}.db"))]
        if dups:
            print(f"    - [WARN] Found {len(dups)} project .db file(s) duplicated in ~/.gemini/antigravity-cli/conversations (AdditionalConvoDir CLI trap!)")

    # 3. Git & Rules Audit
    print("\n[3] Git, Capsule & Workspace Rules Audit:")
    git_dir = os.path.join(project_dir, ".git")
    if os.path.exists(git_dir):
        rem = subprocess.run(["git", "-C", project_dir, "remote", "-v"], capture_output=True, text=True).stdout.strip()
        print(f"    - Git Repository:   YES ({'remotes configured' if rem else 'no git remote configured'})")
    else:
        print("    - Git Repository:   NO")

    gi_path = os.path.join(project_dir, ".gitignore")
    if os.path.exists(gi_path):
        gi_txt = open(gi_path, encoding="utf-8", errors="ignore").read()
        print(f"    - .gitignore has .agy-capsule: {'YES' if '.agy-capsule' in gi_txt else 'NO (add .agy-capsule/ to .gitignore or use --capsule-dir)'}")

    for rule_fn in ("AGENTS.md", "GEMINI.md"):
        rp = os.path.join(project_dir, rule_fn)
        if os.path.exists(rp):
            rtxt = open(rp, encoding="utf-8", errors="ignore").read()
            if "root" in rtxt.lower() and ("clean" in rtxt.lower() or "scripts/" in rtxt.lower()):
                print(f"    - [IMPORTANT] {rule_fn} enforces a CLEAN ROOT DIRECTORY! Invoke agy_capsule_sync.py from the skill directory with --project-dir (or use --capsule-dir outside repo).")

    # 4. Cross-Platform Code Scan
    print("\n[4] Cross-OS Portability Scan (Windows -> POSIX):")
    win_refs, ext_imports = scan_codebase(project_dir)
    print(f"    - Third-party Python imports detected: {', '.join(ext_imports) if ext_imports else 'None (Pure stdlib!)'}")
    if win_refs:
        print(f"    - Hardcoded Windows path/tool references ({len(win_refs)} found):")
        for rel, ln, tok, snippet in win_refs[:15]:
            print(f"      * {rel}:{ln} -> `{tok}` | {snippet}")
        if len(win_refs) > 15:
            print(f"      ... and {len(win_refs) - 15} more.")
    else:
        print("    - Hardcoded Windows path/tool references: None!")

    print("\n" + "=" * 74)


if __name__ == "__main__":
    main()
