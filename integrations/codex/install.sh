#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo "Usage: $0 [--target SKILLS_DIR] [--copy|--symlink]" >&2
}

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
target_root="${CODEX_HOME:-${HOME}/.codex}/skills"
mode="copy"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target)
      [[ $# -ge 2 ]] || { usage; exit 2; }
      target_root="$2"
      shift 2
      ;;
    --copy)
      mode="copy"
      shift
      ;;
    --symlink)
      mode="symlink"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage
      exit 2
      ;;
  esac
done

[[ -f "$script_dir/SKILL.md" ]] || { echo "SKILL.md is missing." >&2; exit 1; }
command -v aer >/dev/null 2>&1 || {
  echo "The aer command is not installed; install the wheel before this integration." >&2
  exit 1
}

mkdir -p -- "$target_root"
target_root="$(cd -- "$target_root" && pwd -P)"
destination="$target_root/agent-efficiency-runtime"
if [[ -e "$destination" || -L "$destination" ]]; then
  echo "Refusing to overwrite existing path: $destination" >&2
  exit 3
fi

if [[ "$mode" == "symlink" ]]; then
  ln -s -- "$script_dir" "$destination"
else
  cp -a -- "$script_dir" "$destination"
fi

echo "Installed Agent Efficiency Runtime skill at $destination"

