#!/usr/bin/env python3
"""
install_skill.py - Universal Installer for `agy-chat-migration`

Installs the `agy-chat-migration` skill (or full plugin bundle) globally into
`~/.gemini/config/`, into a specific workspace `.agents/skills/`, or onto a
remote Linux/macOS host via SSH/SCP (optionally deploying the LF-clean
multi-project systemd sync timer).

Usage:
  python install_skill.py
  python install_skill.py --as-plugin
  python install_skill.py --workspace /path/to/project
  python install_skill.py --remote user@devserver [--setup-systemd-timer] [--projects-root ~/projects]
"""

import argparse
import os
import shlex
import shutil
import subprocess
import sys

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


TEXT_EXTENSIONS = {
    ".md", ".py", ".sh", ".service", ".timer", ".json", ".txt", ".yml", ".yaml",
}


def copy_tree_lf(src_root: str, dst_root: str, exclude_dirs=None):
    exclude_dirs = set(exclude_dirs or ()) | {"__pycache__", ".git"}
    if os.path.exists(dst_root):
        shutil.rmtree(dst_root)
    for root, dirs, files in os.walk(src_root):
        dirs[:] = [d for d in dirs if d not in exclude_dirs]
        rel = os.path.relpath(root, src_root)
        target_dir = os.path.join(dst_root, rel) if rel != "." else dst_root
        os.makedirs(target_dir, exist_ok=True)
        for fn in files:
            if fn.endswith(".pyc"):
                continue
            s_fp = os.path.join(root, fn)
            d_fp = os.path.join(target_dir, fn)
            ext = os.path.splitext(fn)[1].lower()
            if ext in TEXT_EXTENSIONS:
                with open(s_fp, "rb") as f:
                    data = f.read().replace(b"\r\n", b"\n")
                with open(d_fp, "wb") as f:
                    f.write(data)
            else:
                shutil.copy2(s_fp, d_fp)


def main():
    parser = argparse.ArgumentParser(description="Install agy-chat-migration skill or plugin")
    parser.add_argument("--as-plugin", action="store_true", help="Install the full plugin bundle to ~/.gemini/config/plugins/agy-chat-migration")
    parser.add_argument("--workspace", default="", help="Install skill into <workspace>/.agents/skills/agy-chat-migration")
    parser.add_argument("--remote", default="", help="Optional SSH target (e.g., user@host) to deploy skill/tools remotely")
    parser.add_argument("--remote-tools-dir", default="~/projects/.tools", help="Remote directory for shared sync tools (default: ~/projects/.tools)")
    parser.add_argument("--setup-systemd-timer", action="store_true", help="With --remote, also deploy and enable systemd user sync timer on Linux")
    parser.add_argument("--projects-root", default="~/projects", help="Projects root used by remote systemd sync timer (default: ~/projects)")
    args = parser.parse_args()

    # scripts/ -> skills/agy-chat-migration/ -> skills/ -> repo_root/
    skill_src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    repo_root = os.path.dirname(os.path.dirname(skill_src))

    if args.workspace:
        dst = os.path.join(os.path.abspath(args.workspace), ".agents", "skills", "agy-chat-migration")
        copy_tree_lf(skill_src, dst)
        print(f"[OK] Installed workspace skill: {dst}")
        return

    if args.as_plugin:
        local_dst = os.path.expanduser("~/.gemini/config/plugins/agy-chat-migration")
        copy_tree_lf(repo_root, local_dst, exclude_dirs={".agy-capsule"})
        print(f"[OK] Installed global plugin: {local_dst}")
        remote_parent = "~/.gemini/config/plugins"
        remote_name = "agy-chat-migration"
    else:
        local_dst = os.path.expanduser("~/.gemini/config/skills/agy-chat-migration")
        copy_tree_lf(skill_src, local_dst)
        print(f"[OK] Installed global skill: {local_dst}")
        remote_parent = "~/.gemini/config/skills"
        remote_name = "agy-chat-migration"

    if args.remote:
        remote = args.remote
        remote_parent_exp = remote_parent.replace("~", "$HOME")
        remote_tools_exp = args.remote_tools_dir.replace("~", "$HOME")

        subprocess.run([
            "ssh", remote,
            f"mkdir -p {remote_parent_exp} && rm -rf {remote_parent_exp}/{remote_name}",
        ], check=True)
        subprocess.run([
            "scp", "-r", local_dst, f"{remote}:{remote_parent}/",
        ], check=True)
        print(f"[OK] Deployed {remote_name} to {remote}:{remote_parent}/{remote_name}")

        if args.setup_systemd_timer:
            scripts_dir = os.path.join(skill_src, "scripts")
            systemd_dir = os.path.join(scripts_dir, "systemd")

            subprocess.run([
                "ssh", remote,
                f"mkdir -p {remote_tools_exp} $HOME/.config/systemd/user",
            ], check=True)

            # Upload LF-normalized scripts
            subprocess.run([
                "scp",
                os.path.join(scripts_dir, "agy_capsule_sync.py"),
                os.path.join(scripts_dir, "audit_project_for_migration.py"),
                os.path.join(systemd_dir, "agy-capsule-sync-all.sh"),
                f"{remote}:{args.remote_tools_dir}/",
            ], check=True)

            # Render service/timer cleanly with LF endings into a temp staging copy if needed
            import tempfile
            with tempfile.TemporaryDirectory() as tmpdir:
                svc_src = open(os.path.join(systemd_dir, "agy-capsule-export.service"), "r", encoding="utf-8").read()
                tools_for_systemd = args.remote_tools_dir.replace("~", "%h")
                svc_rendered = svc_src.replace("%h/projects/.tools", tools_for_systemd)
                svc_rendered = svc_rendered.replace("AGY_PROJECTS_ROOT=%h/projects", f"AGY_PROJECTS_ROOT={args.projects_root.replace('~', '%h')}")
                svc_tmp = os.path.join(tmpdir, "agy-capsule-export.service")
                with open(svc_tmp, "w", encoding="utf-8", newline="\n") as f:
                    f.write(svc_rendered)

                tmr_src = open(os.path.join(systemd_dir, "agy-capsule-export.timer"), "r", encoding="utf-8").read()
                tmr_tmp = os.path.join(tmpdir, "agy-capsule-export.timer")
                with open(tmr_tmp, "w", encoding="utf-8", newline="\n") as f:
                    f.write(tmr_src)

                subprocess.run([
                    "scp", svc_tmp, tmr_tmp, f"{remote}:~/.config/systemd/user/",
                ], check=True)

            subprocess.run([
                "ssh", remote,
                f"chmod +x {remote_tools_exp}/agy-capsule-sync-all.sh {remote_tools_exp}/agy_capsule_sync.py && "
                "systemctl --user reset-failed agy-capsule-export.timer agy-capsule-export.service 2>/dev/null || true && "
                "systemctl --user daemon-reload && "
                "systemctl --user enable --now agy-capsule-export.timer && "
                "systemctl --user list-timers agy-capsule-export.timer --no-pager",
            ], check=True)
            print(f"[OK] Configured and enabled systemd user timer on {remote} (projects root: {shlex.quote(args.projects_root)})")


if __name__ == "__main__":
    main()
