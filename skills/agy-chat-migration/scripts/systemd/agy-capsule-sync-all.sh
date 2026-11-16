#!/usr/bin/env bash
set -euo pipefail

# Syncs all Antigravity projects under AGY_PROJECTS_ROOT that contain .agy-capsule/project_id (or project.json)
PROJECTS_ROOT="${AGY_PROJECTS_ROOT:-${HOME}/projects}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYNC_TOOL="${AGY_SYNC_TOOL:-${SCRIPT_DIR}/agy_capsule_sync.py}"

if [ ! -d "${PROJECTS_ROOT}" ]; then
    echo "[WARN] AGY_PROJECTS_ROOT does not exist: ${PROJECTS_ROOT}"
    exit 0
fi

for proj_dir in "${PROJECTS_ROOT}"/*; do
    [ -d "${proj_dir}" ] || continue
    # Skip symlinks so aliased directories are not synced twice
    [ ! -L "${proj_dir}" ] || continue
    if [ ! -f "${proj_dir}/.agy-capsule/project_id" ] && [ ! -f "${proj_dir}/.agy-capsule/project.json" ]; then
        continue
    fi

    if [ -f "${SYNC_TOOL}" ]; then
        python3 "${SYNC_TOOL}" sync --project-dir "${proj_dir}"
    elif [ -f "${proj_dir}/scripts/agy_capsule_sync.py" ]; then
        python3 "${proj_dir}/scripts/agy_capsule_sync.py" sync --project-dir "${proj_dir}"
    fi
done
