#!/usr/bin/env bash
set -euo pipefail

python_bin="${PYTHON:-python3.11}"
venv_dir="${AER_SMOKE_VENV:-/tmp/aer-wheel-test}"
smoke_home="${AER_SMOKE_HOME:-/tmp/aer-wheel-home}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
project_root="$(cd -- "$script_dir/.." && pwd -P)"

reject_broad_path() {
  local value="$1"
  case "$value" in
    ""|/|"${HOME}"|"${CODEX_HOME:-/__aer_unset__}")
      echo "Refusing broad smoke path: $value" >&2
      exit 2
      ;;
  esac
}

reject_broad_path "$venv_dir"
reject_broad_path "$smoke_home"
[[ ! -e "$venv_dir" && ! -L "$venv_dir" ]] || {
  echo "Refusing to overwrite existing path: $venv_dir" >&2
  exit 2
}
[[ ! -e "$smoke_home" && ! -L "$smoke_home" ]] || {
  echo "Refusing to overwrite existing path: $smoke_home" >&2
  exit 2
}

mkdir -m 700 -- "$venv_dir" "$smoke_home"
touch -- "$venv_dir/.aer-wheel-smoke" "$smoke_home/.aer-wheel-smoke"

cleanup() {
  [[ -f "$venv_dir/.aer-wheel-smoke" ]] && rm -rf -- "$venv_dir"
  [[ -f "$smoke_home/.aer-wheel-smoke" ]] && rm -rf -- "$smoke_home"
}
trap cleanup EXIT

"$python_bin" -m venv "$venv_dir"
"$venv_dir/bin/python" -m pip install "$project_root"/dist/*.whl
AER_HOME="$smoke_home" "$venv_dir/bin/aer" --version
AER_HOME="$smoke_home" "$venv_dir/bin/aer" doctor
AER_HOME="$smoke_home" "$venv_dir/bin/aer" discover "ppt patch"
AER_HOME="$smoke_home" "$venv_dir/bin/aer" schema presentation.patch --compact

output_dir="$smoke_home/example-output"
mkdir -m 700 -- "$output_dir"
for kind in presentation document workbook chart; do
  case "$kind" in
    presentation) suffix="pptx" ;;
    document) suffix="docx" ;;
    workbook) suffix="xlsx" ;;
    chart) suffix="png" ;;
  esac
  AER_HOME="$smoke_home" "$venv_dir/bin/aer" build \
    "$project_root/examples/$kind.yaml" -o "$output_dir/$kind.$suffix"
  AER_HOME="$smoke_home" "$venv_dir/bin/aer" validate "$output_dir/$kind.$suffix"
done

AER_HOME="$smoke_home" "$venv_dir/bin/aer" patch "$output_dir/presentation.pptx" \
  --spec "$project_root/examples/patches/presentation.yaml" --validate
AER_HOME="$smoke_home" "$venv_dir/bin/aer" patch "$output_dir/document.docx" \
  --spec "$project_root/examples/patches/document.yaml" --validate
AER_HOME="$smoke_home" "$venv_dir/bin/aer" patch "$output_dir/workbook.xlsx" \
  --spec "$project_root/examples/patches/workbook.yaml" --validate
AER_HOME="$smoke_home" "$venv_dir/bin/aer" archive create "$output_dir" \
  -o "$smoke_home/examples.zip"
AER_HOME="$smoke_home" "$venv_dir/bin/aer" archive verify "$smoke_home/examples.zip"
