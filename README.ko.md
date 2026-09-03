# Agent Efficiency Runtime

Agent Efficiency Runtime(AER)은 AI Agent가 판단할 정보만 노출하고 대용량 데이터, 생성 코드,
로그, 결과물은 모델 컨텍스트 밖에서 결정론적으로 처리하는 로컬 실행 계층이다.

## 줄이는 토큰 낭비

AER은 반복되는 Office·차트 Python 코드를 versioned semantic spec으로, 전체 파일 읽기를
selector 기반 조회로, 전체 재생성을 atomic patch로, 긴 로그를 compact diagnostics로 바꾼다.
큰 원문과 결과는 SHA-256 object store에 보존하고 `aer://sha256/...` 참조만 반환한다. 장기 작업
상태와 caller가 제공한 사용량 기록도 로컬에 남긴다. 런타임 내부에는 LLM 클라이언트나 모델 API
호출이 없다.

## 설치

공식 첫 지원 환경은 Linux이며 Python 3.11 이상이 필요하다.

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install .
source .venv/bin/activate
export AER_HOME="$HOME/.aer"       # 선택 사항이며 이미 기본값이다
aer --version
aer doctor
```

기본 설치에 Office, PDF, image, chart 라이브러리가 포함된다. v0.1 query 입력은 Parquet를
지원하지 않는다. LibreOffice, Pandoc, `pdftoppm`은 선택적 외부 프로그램이다. 필요한 프로그램이
없으면 해당 명령은 dependency와 capability를 포함한 `DEPENDENCY_MISSING`을 반환한다.

## 5분 quick start

저장소 루트에서 그대로 실행한다.

```bash
mkdir -p example-output
aer discover "build and patch ppt"
aer schema presentation.build --compact --example
aer build examples/presentation.yaml --validate -o example-output/deck.pptx
aer inspect example-output/deck.pptx --selector "slide:id=metrics/shape:id=token-reduction-value"
aer patch example-output/deck.pptx --spec examples/patches/presentation.yaml --backup --validate
aer archive create example-output -o example-output.zip
aer archive verify example-output.zip
```

기본 출력은 한 개의 compact JSON object다. 사람용 표시는 global `--pretty` 또는 `--human`, 내부
traceback은 global `--debug`에서만 활성화한다.

## 명령 요약

| 명령 | 역할 |
|---|---|
| `aer discover`, `aer schema` | 로컬 검색 후 선택된 capability 계약만 공개 |
| `aer store` | content dedup, 검증, 조회, pin, GC |
| `aer inspect` | text/data/repository/Office/PDF의 제한된 부분 조회 |
| `aer run -- ...` | shell 없이 argv 실행, compact 오류와 redacted raw log ref 반환 |
| `aer data query` | 로컬 filter/select/sort/dedup/aggregate |
| `aer build`, `aer patch`, `aer validate` | semantic artifact 생성, 부분 수정, 검증 |
| `aer convert`, `aer image`, `aer pdf`, `aer archive` | 결정론적 변환과 delivery 작업 |
| `aer state`, `aer recipe` | 장기 상태와 신뢰된 workflow 저장·실행 |
| `aer profile`, `aer benchmark` | caller가 입력한 사용량 집계와 로컬 비교 측정 |
| `aer doctor` | core 상태와 선택 dependency 확인 |

## 대표 사용 예

```bash
aer run --timeout 300 -- pytest -q
aer data query orders.xlsx --sheet Raw \
  --where "status == pending" --where "total >= 30000" \
  --select id,customer,total --sort total --descending --limit 100 -o pending.csv
aer state init release-01 --goal "검증된 결과물 전달"
aer state update release-01 --complete "PPTX 생성" --remaining "사람 시각 검토"
```

query preview는 기본 20행 이하이며 전체 결과는 output 또는 object ref로 회수할 수 있다. 명령의
저장 로그에도 검출된 secret은 redaction된다.

runner는 출력을 메모리가 아닌 임시 파일로 spool한다. stdout과 stderr 합계가 256 MiB를 넘으면
프로세스를 종료하고, 그때까지 캡처한 textual prefix를 UTF-8 정규화, stdout/stderr section 구분,
ANSI 제거, secret redaction 후 저장하며 `output_limit_exceeded`를 반환한다. 이 prefix를 완전한 실행
로그라고 표시하지 않는다.

## Codex 연동

wheel 설치 뒤 다음 스크립트를 실행한다.

```bash
./integrations/codex/install.sh --copy
```

스크립트는 source checkout과 source distribution에 포함되며 `$CODEX_HOME` 또는 `~/.codex`를
탐지하고 `--target`도 받는다. 기존 destination이나 Codex 설정은 덮어쓰지 않는다. checkout을
계속 유지하는 개발 환경에서만 `--symlink`를 사용한다. 프로젝트별 snippet은
`integrations/codex/AGENTS-snippet.md`다.

## 검증과 benchmark

```bash
aer validate example-output/deck.pptx
aer benchmark run --scenario log-compaction
aer benchmark report
aer profile compare --task ppt-generation
```

benchmark의 byte, wall-clock time, hash, validity는 로컬 실행 실측이다. token 값은 실측 UTF-8
byte에 `ceil(bytes / 4)`를 적용한 추정치이며 provider 청구 token이 아니다. Office render
validation은 LibreOffice를 사용한다. PDF render validation은 `pdftoppm`으로 첫 3페이지까지만
rasterize하고 PNG preview를 ref로 저장한다. 어느 검사도 사람 수준 시각 품질 통과를 주장하지
않는다.

## 보안

safe YAML, `shell=False` argv, process-group timeout, 저장 전 secret redaction, atomic replace,
SHA-256 precondition, archive traversal 차단, symlink 정책, image/ZIP/data/output 제한, capability-only
recipe allowlist를 적용한다. raw recipe command는 trust와 명시적 allow가 모두 있어야 한다. URL
fetch, 사용자 코드 평가, macro 및 embedded executable 실행은 하지 않는다. semantic spec은 4 MiB,
patch 대상은 256 MiB로 제한한다. ZIP/OOXML은 최대 10,000 entry, 전체 압축 해제 크기 512 MiB,
entry당 256 MiB이며 1 MiB 이상 entry의 압축률은 최대 200:1이다. Office 변환과 render validation은
외부 relationship, macro, executable part를 거부한다. 정규식은 위험한 구조를 제한하고 실행
timeout도 적용한다. CSV/TSV의 수식처럼 보이는 값은 XLSX query/변환 출력에서 수식이 아닌 quoted
text로 기록한다.

## 실제 한계

Linux만 현재 공식 검증됐다. Office는 문서화한 semantic block을 지원하며 모든 native feature의
완전한 round-trip은 아니다. Excel 수식은 보존하지만 계산하지 않는다. Office→PDF/render에는
LibreOffice, markup 변환에는 Pandoc, PDF raster preview에는 `pdftoppm`이 필요하다. 자동 검증은
사람 시각 검토를 대체하지 않는다. v0.1은 Parquet query, 외부 URL, macro-enabled format 보존,
GUI, provider token 자동 수집을 지원하지 않는다. profile 값은 caller가 제공하므로 v0.1은
실측값과 추정값 provenance를 검증하거나 분류하지 않는다. 가능하면 provider usage 값을 사용하고
출처는 `notes`에 기록해야 한다.
