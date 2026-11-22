# Terra 구현·리뷰 규칙

**최소 스택.** `src/sourdough/`의 Python 패키지 하나, SQLite 트랜잭션 저장소 하나, macOS에서 회사 프로세스와 분리된 감독자를 사용한다. 모의 체결 원본도 회사 프로세스 밖에 둔다. 공통 회사 모듈은 퀀트·거래 SDK·Client에 의존하지 않는다. Agent는 회사 도구를, Client는 조회·통제 인터페이스를 사용한다. 둘 다 원장에 직접 쓰지 않는다. NautilusTrader 설정·실행은 거래 어댑터 책임이다. Client가 필요할 때만 Yogurt를 빌드 의존성으로 사용한다.

**01에서 정할 것.** 실제 호환성을 확인한 CPython 패치 버전을 `.python-version`에, 패키지·정상 실행 진입점·빌드 백엔드를 `pyproject.toml`에 기록한다. 빌드 백엔드와 Ruff를 포함해 CI 설치에 필요한 패키지를 해시가 있는 `requirements-dev.lock`에 고정한다. 첫 회사 기동은 표준 라이브러리만으로 실행한다. 패키지마다 서버·인터페이스를 만들지 말고 첫 제품 경로와 함께 필요한 모듈만 연다.

**코드 컨벤션.** 제품 용어는 [용어 사전](../../glossary.md)을 따른다. 모듈·함수는 `snake_case`, 타입은 `PascalCase`로 쓴다. 모듈 밖으로 노출하는 명령·결과에는 타입과 실패 의미를 명시한다. 한 기록에는 한 작성 주체만 둔다. 외부 입력은 어댑터 경계에서 검증한다. 금액·수량은 이진 부동소수점 대신 단위와 정밀도가 있는 값으로 기록한다. 재시도는 원래 행위 ID를 보존하며, 외부 효과가 불명이면 재전송하지 않고 원본을 조회한다.

**개발 단위의 완료.** 설치된 패키지의 정상 진입점을 사용한다. 제품 내부를 우회하는 시험용 성공 경로를 두지 않는다. 첫 단위부터 CI와 동일한 명령을 깨끗한 가상환경에서 실행한다. 포맷이나 명명 취향은 리뷰 토론이 아니라 자동 검사에 맡긴다.

```sh
python -m pip install --require-hashes -r requirements-dev.lock
python -m pip install --no-build-isolation --no-deps .
python -m ruff format --check src tests
python -m ruff check src tests
python -I .github/scripts/run_required_tests.py tests/unit
```

**리뷰 기준.** ① 상태를 바꾼 주체에 권한이 있는가? ② 재시도·중복·재시작 후 원본과 원장이 일치하는가? ③ 다음 소비자가 정상 진입점에서 변경 결과를 읽는가? 해당 경계를 건드렸으면 실패 사례 하나와 독립 관찰값을 PR에 남긴다. 권한 우회·중복 효과·유실·재현되지 않는 합격은 차단한다. 현재 결과에 영향 없는 리팩터링, 추가 런타임, Agent 품질 평가는 후속으로 남긴다.

**전달 규칙.** 한 흐름의 기준 브랜치 `feat/flow-<slug>`를 최신 `main`에서 시작한다. 각 개발 단위는 그 통합 브랜치의 최신 커밋을 기준으로 독립 PR을 올린다. 필수 검사와 소관 리뷰 후 squash하고, 흐름 전체가 연결되면 통합 브랜치 → `main` PR 하나를 연다. PR 설명에는 단위 PR 목록과 흐름 관찰값을 연결한다. 통합 PR에서는 macOS에서 같은 빠른 검사에 더해 `python -I .github/scripts/run_required_tests.py tests/flows/flow-<slug> test_flow.ConnectedFlow.test_end_to_end`를 실행한다. 조건부 시험을 skip해 합격시키지 않는다. `main`의 보호·리뷰와 머지 후 검사는 그대로 유지한다.
