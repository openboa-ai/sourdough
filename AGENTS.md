# Sourdough 개발 진입점

Terra는 제품을 만드는 개발자다. 회사 안에서 근무하는 Agent나 회사 감독자를 대신하지 않는다. 개발에 별도 플러그인은 필요 없다.

1. [첫 Goal: 01–04](docs/engineering/flow-delivery/goal-01-04.md)에서 결과와 중단 조건을 읽는다. [작업 색인](docs/engineering/work-packages.md)에서 현재 단위의 소유 명세만 연다. 승인 범위와 [v1.1 기준선](docs/engineering/development-roadmap.md)을 확인한다.
2. [구현·리뷰 규칙](docs/engineering/flow-delivery/guide.md)을 적용한다. 한 개발 단위는 하나의 관찰 가능한 변경과 하나의 PR이다. 의존하는 단위는 앞 PR이 통합 브랜치에 들어간 뒤 그 결과 위에 쌓는다.
3. 작업 기록은 흐름의 Issue 하나에 모은다. PR에는 명세·관찰값·다음 단위만 연결한다. 증거 없는 완료, 기준선 체크섬 재작성, 권한 우회는 금지한다.
4. [CI·머지 계약](docs/engineering/flow-delivery/spec.md)과 [기여 규칙](CONTRIBUTING.md)을 따른다. CI나 권한 경계를 바꾸는 PR은 지정 리뷰를 거친다.
