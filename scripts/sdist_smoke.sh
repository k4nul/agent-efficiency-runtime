#!/usr/bin/env bash
set -euo pipefail

python_bin="${PYTHON:-python3.11}"
smoke_root="${AER_SDIST_SMOKE_ROOT:-/tmp/aer-sdist-test}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
project_root="$(cd -- "$script_dir/.." && pwd -P)"
project_version="$(
  "$python_bin" -c \
    'import pathlib, sys, tomllib; print(tomllib.loads(pathlib.Path(sys.argv[1]).read_text())["project"]["version"])' \
    "$project_root/pyproject.toml"
)"

case "$smoke_root" in
  ""|/|"${HOME}"|"${CODEX_HOME:-/__aer_unset__}"|"$project_root")
    echo "Refusing broad smoke path: $smoke_root" >&2
    exit 2
    ;;
esac
[[ ! -e "$smoke_root" && ! -L "$smoke_root" ]] || {
  echo "Refusing to overwrite existing path: $smoke_root" >&2
  exit 2
}

sdist_path="${AER_SDIST_PATH:-$project_root/dist/agent_efficiency_runtime-$project_version.tar.gz}"
[[ -f "$sdist_path" && ! -L "$sdist_path" ]] || {
  echo "Source distribution is missing or unsafe: $sdist_path" >&2
  exit 2
}

mkdir -m 700 -- "$smoke_root"
smoke_root="$(cd -- "$smoke_root" && pwd -P)"
install_marker="$smoke_root/.aer-sdist-smoke"
touch -- "$install_marker"
cleanup() {
  [[ -f "$install_marker" ]] && rm -rf -- "$smoke_root"
}
trap cleanup EXIT

venv_dir="$smoke_root/venv"
extract_dir="$smoke_root/source"
work_dir="$smoke_root/work"
aer_home="$smoke_root/aer-home"
mkdir -m 700 -- "$extract_dir" "$work_dir"
"$python_bin" -c \
  'import sys, tarfile; archive = tarfile.open(sys.argv[1]); archive.extractall(sys.argv[2], filter="data"); archive.close()' \
  "$sdist_path" "$extract_dir"
source_root="$extract_dir/agent_efficiency_runtime-$project_version"

required_source_files=(
  "AGENTS.md"
  "CHANGELOG.md"
  "LICENSE"
  "README.md"
  "README.ko.md"
  "THIRD_PARTY_NOTICES.md"
  "docs/architecture.md"
  "docs/protocol.md"
  "docs/capabilities.md"
  "docs/artifact-spec.md"
  "docs/patch-spec.md"
  "docs/recipes.md"
  "docs/security.md"
  "docs/token-measurement.md"
  "docs/codex-integration.md"
  "docs/limitations.md"
  "docs/development.md"
  "examples/presentation.yaml"
  "examples/document.yaml"
  "examples/workbook.yaml"
  "examples/chart.yaml"
  "examples/patches/presentation.yaml"
  "examples/patches/document.yaml"
  "examples/patches/workbook.yaml"
  "examples/patches/json.yaml"
  "examples/recipes/office-delivery.yaml"
  "examples/data/metrics.csv"
  "integrations/codex/SKILL.md"
  "integrations/codex/AGENTS-snippet.md"
  "integrations/codex/install.sh"
  "recipes/office-delivery.yaml"
  "schemas/artifact-v1.schema.json"
  "schemas/patch-v1.schema.json"
  "schemas/recipe-v1.schema.json"
  "scripts/sdist_smoke.sh"
  "scripts/wheel_smoke.sh"
  "templates/business-clean.json"
)
for relative_path in "${required_source_files[@]}"; do
  [[ -f "$source_root/$relative_path" ]] || {
    echo "Source distribution is missing: $relative_path" >&2
    exit 1
  }
done
[[ -x "$source_root/integrations/codex/install.sh" ]] || {
  echo "Codex installer is not executable in the source distribution." >&2
  exit 1
}

"$python_bin" -m venv "$venv_dir"
(
  cd -- "$work_dir"
  PIP_CACHE_DIR="$smoke_root/pip-cache" PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONPATH= \
    "$venv_dir/bin/python" -m pip install "$sdist_path"
  PYTHONPATH= "$venv_dir/bin/python" -c \
    'import pathlib, sys, aer; assert not pathlib.Path(aer.__file__).resolve().is_relative_to(pathlib.Path(sys.argv[1]).resolve())' \
    "$project_root"
  PYTHONPATH= "$venv_dir/bin/python" -c \
    'from importlib.resources import files; root=files("aer"); assert root.joinpath("resources/fonts/NanumGothic.ttf").is_file(); assert root.joinpath("resources/licenses/NanumGothic-OFL-1.1.txt").is_file()'

  AER_HOME="$aer_home" PYTHONPATH= "$venv_dir/bin/aer" --version
  AER_HOME="$aer_home" PYTHONPATH= "$venv_dir/bin/aer" doctor
  AER_HOME="$aer_home" PYTHONPATH= "$venv_dir/bin/aer" discover "ppt patch"
  AER_HOME="$aer_home" PYTHONPATH= "$venv_dir/bin/aer" schema presentation.patch --compact
  AER_HOME="$aer_home" PYTHONPATH= "$venv_dir/bin/aer" recipe show office-delivery

  output_dir="$work_dir/example-output"
  mkdir -m 700 -- "$output_dir"
  for kind in presentation document workbook chart; do
    case "$kind" in
      presentation) suffix="pptx" ;;
      document) suffix="docx" ;;
      workbook) suffix="xlsx" ;;
      chart) suffix="png" ;;
    esac
    AER_HOME="$aer_home" PYTHONPATH= "$venv_dir/bin/aer" build \
      "$source_root/examples/$kind.yaml" -o "$output_dir/$kind.$suffix"
    AER_HOME="$aer_home" PYTHONPATH= "$venv_dir/bin/aer" validate \
      "$output_dir/$kind.$suffix"
  done

  AER_HOME="$aer_home" PYTHONPATH= "$venv_dir/bin/aer" patch \
    "$output_dir/presentation.pptx" \
    --spec "$source_root/examples/patches/presentation.yaml" --backup --validate
  [[ -f "$output_dir/presentation.pptx.bak" ]]

  PATH="$venv_dir/bin:$PATH" "$source_root/integrations/codex/install.sh" \
    --copy --target "$smoke_root/codex-skills"
  [[ -f "$smoke_root/codex-skills/agent-efficiency-runtime/SKILL.md" ]]
)
