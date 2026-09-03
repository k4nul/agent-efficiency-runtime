# Agent Efficiency Runtime

[English](README.md)

[![CI](https://github.com/k4nul/agent-efficiency-runtime/actions/workflows/ci.yml/badge.svg)](https://github.com/k4nul/agent-efficiency-runtime/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB.svg)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Agent Efficiency Runtime(AER)은 AI 에이전트가 판단에 필요한 정보만 받도록 하고, 대용량 데이터,
생성 코드, 긴 로그와 결과물은 모델 컨텍스트 밖에서 결정론적으로 처리하는 로컬 실행 계층이다.

AER 내부에는 LLM 클라이언트가 없으며 모델 API도 호출하지 않는다. AI 에이전트, 스크립트 또는
사람이 제한된 JSON 중심 CLI를 통해 호출하는 로컬 도구 런타임이다.

## 만든 이유

AI 에이전트 작업은 다음과 같은 방식으로 컨텍스트와 실행 비용을 반복해서 낭비하기 쉽다.

- Office, PDF, 이미지와 차트를 만들 때마다 Python 보일러플레이트를 다시 생성
- 한 부분만 필요해도 파일 전체를 읽음
- 작은 요소를 수정하기 위해 결과물 전체를 재생성
- 긴 명령 로그나 바이너리 데이터를 모델 컨텍스트에 복사
- 대화 압축 이후 장기 작업 상태를 다시 복원

AER은 이를 versioned semantic spec, selector 기반 제한 조회, atomic patch, compact diagnostics,
영속 작업 상태와 `aer://sha256/...` 콘텐츠 주소 참조로 대체한다.

## 주요 기능

| 명령 계열 | 역할 |
|---|---|
| `aer discover`, `aer schema` | 로컬 capability를 찾고 선택된 compact 계약만 공개 |
| `aer store` | 콘텐츠 중복 제거, 검증, 조회, pin, 목록과 GC |
| `aer inspect` | 텍스트, 데이터, 저장소, Office 또는 PDF의 제한된 부분 조회 |
| `aer run -- ...` | 셸 없이 argv를 실행하고 compact 진단과 redacted log ref 반환 |
| `aer data query` | 로컬 표 데이터 filter, select, sort, dedup과 aggregate |
| `aer build`, `aer patch`, `aer validate` | semantic artifact 생성, 부분 수정과 결과 검증 |
| `aer convert`, `aer image`, `aer pdf`, `aer archive` | 결정론적 변환, 미디어, PDF와 전달 작업 |
| `aer state`, `aer recipe` | 장기 작업 상태 저장과 제한된 신뢰 workflow 실행 |
| `aer profile`, `aer benchmark` | caller 제공 사용량 집계와 로컬 비교 측정 |
| `aer doctor` | core 상태와 선택 capability 사용 가능 여부 확인 |

핵심 artifact workflow는 DOCX, PPTX, XLSX, 차트, 이미지, PDF, ZIP과 로컬 구조화·표 데이터를
다룬다.

## 설치

공식 첫 지원 환경은 Linux이며 Python 3.11 이상이 필요하다.

```bash
git clone https://github.com/k4nul/agent-efficiency-runtime.git
cd agent-efficiency-runtime
python3.11 -m venv .venv
.venv/bin/python -m pip install .
source .venv/bin/activate
export AER_HOME="$HOME/.aer"  # 선택 사항이며 이미 기본값이다
aer --version
aer doctor
```

기본 Python 설치에 Office, PDF, 이미지와 차트 workflow에 필요한 라이브러리가 포함된다.
LibreOffice, Pandoc과 `pdftoppm`은 선택적 외부 프로그램이다. 필요한 프로그램이 없으면 해당
명령은 dependency와 capability를 포함한 `DEPENDENCY_MISSING`을 반환한다.

## 5분 quick start

저장소 루트에서 실행한다.

```bash
mkdir -p example-output
aer discover "build and patch ppt"
aer schema presentation.build --compact --example
aer build examples/presentation.yaml --validate -o example-output/deck.pptx
aer inspect example-output/deck.pptx \
  --selector "slide:id=metrics/shape:id=token-reduction-value"
aer patch example-output/deck.pptx \
  --spec examples/patches/presentation.yaml --backup --validate
aer archive create example-output -o example-output.zip
aer archive verify example-output.zip
```

기본 응답은 한 개의 compact JSON object다. 사람이 읽는 형식은 global `--pretty` 또는
`--human`을 사용하고 내부 traceback은 global `--debug`에서만 활성화한다.

### 제한된 명령 출력

```bash
aer run --timeout 300 -- pytest -q
aer store cat aer://sha256/REPLACE_WITH_RETURNED_DIGEST \
  --start-line 10 --end-line 30
```

runner는 출력을 메모리가 아니라 임시 파일로 spool한다. stdout과 stderr 합계가 256 MiB를 넘으면
프로세스를 종료하고, 그때까지 캡처한 textual prefix를 UTF-8 정규화, stdout/stderr 구분, ANSI
제거와 secret redaction 후 저장한다. 응답에는 `output_limit_exceeded`를 표시하며 해당 prefix를
완전한 로그라고 주장하지 않는다.

### 로컬 데이터 조회

큰 데이터는 로컬에 유지하고 preview는 기본 20행 이하로 제한한다.

```bash
aer data query orders.xlsx --sheet Raw \
  --where "status == pending" --where "total >= 30000" \
  --select id,customer,total --sort total --descending --limit 100 \
  -o pending.csv
```

### 영속 작업 상태

```bash
aer state init release-01 --goal "검증된 결과물 준비"
aer state update release-01 \
  --complete "PPTX 생성" --remaining "사람 시각 검토"
aer state update release-01 \
  --decision provider=TradingView --artifact deck=example-output/deck.pptx
aer state checkpoint release-01
```

## Codex 연동

wheel 설치 후 기존 Codex 설정을 덮어쓰지 않고 포함된 skill을 설치한다.

```bash
./integrations/codex/install.sh --copy
```

installer는 `$CODEX_HOME`을 탐지하고 없으면 `~/.codex`를 사용한다. 명시적 `--target`을 지원하며
기존 destination은 덮어쓰지 않는다. checkout을 유지하는 개발 환경에서만 `--symlink`를 사용한다.
프로젝트별 지침은 `integrations/codex/AGENTS-snippet.md`에 있다.

## 검증과 benchmark

```bash
aer validate example-output/deck.pptx
aer benchmark run --scenario log-compaction
aer benchmark report
aer profile compare --task ppt-generation
```

benchmark의 byte, wall-clock time, validity와 hash는 실제 로컬 workload 측정값이다. token 값은
`ceil(UTF-8 bytes / 4)`로 계산한 추정치이며 provider 청구 token이 아니다. Office render
validation은 LibreOffice를 사용하고, PDF render validation은 `pdftoppm`으로 처음 3페이지까지만
rasterize한 뒤 PNG preview를 ref로 저장한다. 어느 검사도 사람 수준의 시각 품질 승인을 주장하지
않는다.

## 보안 모델

AER은 safe YAML loading, `shell=False` argv subprocess, process-group timeout, 저장 전 secret
redaction, atomic replacement, SHA-256 precondition, archive traversal 차단, symlink 제한, 정규식
실행 제한과 명시적인 image, ZIP, data, spec, patch, output 크기 제한을 적용한다.

raw recipe command는 trust와 raw-command permission이 모두 명시된 경우에만 허용한다. URL fetch,
사용자 코드 평가, macro와 embedded Office payload 실행은 하지 않는다. Office 변환과 render
validation은 외부 relationship, macro와 executable part를 거부한다. CSV 또는 TSV에서 수식처럼
보이는 값은 XLSX 출력 시 수식이 아니라 quoted text로 기록한다.

위협 모델은 [docs/security.md](docs/security.md), 비공개 취약점 신고 절차는
[SECURITY.md](SECURITY.md)를 참고한다.

## 현재 한계

- Linux가 현재 지속적으로 검증되는 첫 플랫폼이다.
- Office round-trip은 문서화된 semantic block을 대상으로 하며 모든 native feature를 지원하지
  않는다.
- 수식 문자열은 보존하지만 계산하지 않는다.
- Office→PDF와 Office render 검사는 LibreOffice가 필요하다.
- markup 변환은 Pandoc이 필요하다.
- PDF raster preview는 `pdftoppm`이 필요하다.
- 자동 검증은 사람의 시각 검토를 대체하지 않는다.
- v0.1은 Parquet query, 외부 URL fetch, macro-enabled format 보존, GUI와 provider token 자동
  측정을 지원하지 않는다.
- profile 값은 caller 제공값이며 v0.1은 실측값과 추정값 provenance를 검증하지 않는다.

protocol, spec, 개발 절차와 세부 한계는 [docs/](docs/)에서 확인할 수 있다.

## 기여

큰 변경을 제출하기 전에 [CONTRIBUTING.md](CONTRIBUTING.md),
[CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)와 [AGENTS.md](AGENTS.md)를 확인해야 한다. 보안
취약점은 공개 issue로 신고하면 안 된다.

## 라이선스

AER 소스 코드는 [MIT License](LICENSE)로 배포한다. 번들된 제3자 자산은 각자의 라이선스를
유지하며 자세한 내용은 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)를 참고한다.
