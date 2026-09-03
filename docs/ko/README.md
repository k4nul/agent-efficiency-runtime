# Agent Efficiency Runtime 한국어 안내

Agent Efficiency Runtime(AER)은 큰 파일, 반복 생성 코드, 원시 데이터, 긴 로그와
작업 상태를 모델 컨텍스트 밖에서 처리하는 결정적 로컬 실행 계층입니다. 필요한
정보만 제한된 JSON과 되돌릴 수 있는 `aer://sha256/...` 참조로 반환하며 LLM
클라이언트를 포함하거나 모델 API를 호출하지 않습니다.

## 빠른 시작

Linux와 Python 3.11 이상이 필요합니다.

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install .
source .venv/bin/activate
aer --version
aer doctor
```

저장소 예제로 생성, 선택 조회, 패치와 검증을 실행할 수 있습니다.

```bash
mkdir -p example-output
aer discover "build and patch ppt"
aer schema presentation.build --compact --example
aer build examples/presentation.yaml --validate -o example-output/deck.pptx
aer inspect example-output/deck.pptx --selector "slide:id=metrics/shape:id=token-reduction-value"
aer patch example-output/deck.pptx --spec examples/patches/presentation.yaml --backup --validate
aer validate example-output/deck.pptx
```

## 주요 기능

- 로컬 기능 탐색과 작은 스키마 공개
- 콘텐츠 주소 저장소와 `aer://sha256/...` 참조
- 텍스트, 표, 저장소, Office와 PDF의 제한된 선택 조회
- 셸을 거치지 않는 argv 명령 실행과 비밀정보가 제거된 로그 보관
- 표 데이터 필터·정렬·집계와 문서·이미지·PDF·ZIP 생성 및 검증
- SHA-256 사전 조건을 지원하는 원자적 패치와 백업
- 장기 작업 상태, 신뢰된 레시피, 사용량 프로필과 로컬 벤치마크

## 설정과 안전 경계

`AER_HOME`으로 저장 위치를 바꿀 수 있으며 기본값은 사용자 로컬 `.aer`
디렉터리입니다. 기본 응답은 한 개의 작은 JSON 객체입니다. 사람이 읽는 출력이
필요할 때만 전역 `--pretty` 또는 `--human`을 사용하세요.

AER은 URL을 자동으로 가져오거나 사용자 코드를 평가하지 않고, Office macro와
내장 실행 파일을 실행하지 않습니다. 명령은 `shell=False`와 제한 시간으로
실행되며 저장 전 로그 비밀정보를 제거합니다. 스펙, 패치 대상, 명령 출력,
이미지와 압축 해제에는 문서화된 크기 제한이 적용됩니다. 원시 레시피 명령은
명시적인 신뢰와 허용 플래그가 모두 있어야 합니다.

LibreOffice, Pandoc과 `pdftoppm`은 선택 도구입니다. 필요한 도구가 없으면 해당
기능은 `DEPENDENCY_MISSING`을 반환합니다. 자동 렌더 검사는 사람의 시각 검토를
대체하지 않습니다.

## 검증

개발 검증은 다음 전체 게이트를 사용합니다.

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[dev]'
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy src
.venv/bin/pytest
.venv/bin/pytest --cov=aer --cov-report=term-missing
.venv/bin/python -m build
PYTHON=.venv/bin/python AER_SMOKE_VENV=/tmp/aer-wheel-smoke AER_SMOKE_HOME=/tmp/aer-wheel-home scripts/wheel_smoke.sh
PYTHON=.venv/bin/python AER_SDIST_SMOKE_ROOT=/tmp/aer-sdist-smoke scripts/sdist_smoke.sh
git diff --check
```

기능별 명령과 정확한 제한은 [원본 README](../../README.md)를 참고하세요.
