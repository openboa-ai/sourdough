# 회사 · Agent · 퀀트 책임 계층과 업무 흐름

설계 초안 · 모든 제품·통합시험 **UNRUN** · [제품 목적](intent.md) · [기능별 소유자](README.md#system-hierarchy) · [설계 규칙](conventions.md)

**목적:** 사람이 일상 운용에 개입하지 않아도 정해진 위험·비용 한도 안에서 회사의 장기 순수익을 창출한다. Agent-native 회사가 이를 달성할 수 있다는 것은 가설이다. 이 문서의 계층은 역할과 책임의 구분이지 별도 프로세스나 고정 부서의 요구가 아니다. 다이어그램은 계약을 이해하기 위한 설계 증거이며 실행·합격 증거가 아니다. 기능 요구사항은 링크된 intent와 spec이 소유한다.

## 전체 시스템 경계와 연결

직원 Agent의 역할, 펀드 업무 시스템의 구성, 회사 관리형 소프트웨어의 개수는 예시이며 고정 조직이나 필수 배포 단위가 아니다. Agent가 개발한 결과물은 별도 계층이 아니라 담당 업무와 회사 자산에 속한다. 사람은 Client로만 조회·통제하며 Client가 없어도 회사는 운영된다.

```mermaid
flowchart TB
    Human["사람 · 이사회"]
    subgraph SD["Sourdough"]
        Client["Client · 조회와 통제"]
        subgraph Staff["직원 Agent · 개인 작업공간"]
            Agent["Agent · 판단과 업무 수행"]
            Harness["Harness · 맥락과 도구 중재"]
            Desk["개인 작업공간 · 학습"]
            Agent <-->|맥락 · 요청| Harness
            Agent <-->|작업물 · 경험| Desk
        end
        subgraph Common["공통 회사 시스템"]
            Interface["조회 · 명령 인터페이스"]
            Org["조직 · 직원 명부"]
            Auth["위임 · 권한"]
            Work["업무 · 근무 · 인계"]
            Comms["대화 · 검토 · 통지"]
            Docs["공유 문서 · 규정"]
            Resource["자원 · 접근 관리"]
            Assets["회사 자산 · 버전"]
            Finance["재무 · 예산"]
            Accounting["회계 · 결산"]
            Verify["독립 검증"]
            Ops["운영 · 변경 · 복구"]
            Registry["외부 연결 등록"]
            Gateway["외부 커넥터 호출 · 수신"]
        end
        subgraph Fund["펀드 업무 시스템 · 구성은 예시"]
            Research["시장 관측 · 투자 연구"]
            Strategy["전략 후보 · 운용 버전"]
            Risk["계좌 권한 · 거래 위험"]
            Ledger["투자 원장 · 예약 · 포지션"]
            Execution["거래 실행 · 대사"]
            Performance["성과 · 순수익"]
        end
        subgraph Managed["회사 관리형 소프트웨어"]
            NT["NautilusTrader · 회사 관리 인스턴스"]
        end
    end
    subgraph External["외부 운영 시스템"]
        Binance["Binance · 시장 · 실거래 계좌"]
        Providers["모델 · 자료 · 저장소 · 청구"]
        Maint["제품 유지보수 · Issue · 릴리스"]
    end
    Human <-->|조회 · 통제| Client
    Client <-->|조회 · 명령| Interface
    Interface -->|권한 확인| Auth
    Interface -->|소관 명령| Org
    Interface -->|소관 명령| Ops
    Interface -->|거래 통제 명령| Risk
    Org -->|직무 위임| Auth
    Auth -->|유효 권한| Harness
    Auth -->|호출 시 권한| Gateway
    Registry -->|연결 정의| Gateway
    Agent <-->|담당 · 다음 근무| Work
    Agent <-->|대화 · 검토| Comms
    Agent -->|자원 요청| Resource
    Resource <-->|권한 · 할당| Auth
    Resource <-->|예산 · 할당| Finance
    Resource -->|공간 할당| Desk
    Desk -->|선별 공유| Docs
    Desk -->|후보 등록| Assets
    Assets -->|검증 대상| Verify
    Verify -->|검증 결과| Ops
    Ops -->|운영 통지| Comms
    Ops -->|복구 의무| Work
    Harness <-->|외부 도구| Gateway
    Agent <-->|가설 · 평가| Research
    Agent -->|전략 후보| Strategy
    Agent -->|직접 주문| Risk
    Research -->|연구 근거| Strategy
    Strategy -->|후보 검증| Verify
    Ops -->|운용 활성화| Strategy
    Strategy -->|프로그램 주문| Risk
    Risk -->|예약 요청| Ledger
    Ledger -->|예약 확인| Execution
    Execution <-->|실행 · 조회| NT
    Execution <-->|원본 조회| Gateway
    Execution -->|대사 결과| Ledger
    Research <-->|자료 조회| Gateway
    NT <-->|자료 · 실행| Gateway
    Ops -->|설정 · 기동 · 복구| NT
    Gateway <-->|시장 · 계좌| Binance
    Gateway <-->|자료 · 작업| Providers
    Gateway <-->|Issue · 릴리스| Maint
    Gateway -->|비용 증빙| Accounting
    Ledger -->|투자 손익| Performance
    Accounting -->|회사 비용| Performance
    Performance -->|성과 평가| Agent
```

커넥터는 통제된 외부 접근 경계다. 연구·대사는 직접 조회할 수 있지만 등록만으로 호출이 허가되지는 않는다. 위험 증가 주문은 원주문과 예약의 영속 기록, 도메인 승인, 전송 시점 권한과 현재 송신자 확인을 거친다. 모의 계좌와 실거래 계좌는 독립 바인딩이며 현재 실시간 시장 데이터 조회 권한은 실거래 권한이 아니다.

```mermaid
flowchart LR
    Research["연구 결과"] --> Candidate["전략 후보"] --> Verify["백테스트 · 검증"] --> Active["운용 버전"] --> Program["전략 프로그램"]
    Agent["Agent"] -->|직접 주문| Risk["권한 · 위험 심사"]
    Program -->|주문 요청| Risk
    Risk --> Reserve["원주문 · 예약"] --> Dispatch["전송 시점 확인"] --> Venue["허용 목적지"]
```

직접 주문에는 전략 프로그램 활성화를 요구하지 않는다. 프로그램의 실행 권한은 작성자의 권한과 구별한다. 과거 재생과 현재 시장 기반 모의 운용은 동일한 회사 통제 계약을 사용하되 각 실행 환경과 원본을 분리한다.

```mermaid
flowchart LR
    Fill["독립 체결 원본"] --> Crash["회사 중단"] --> Restart["감독자 재기동"] --> Fence["이전 송신자 차단"] --> Lookup["원주문 조회"]
    Lookup -->|확인| Book["원장 대사"] --> Duty["다음 근무"] --> Agent["같은 Agent의 새 판단"]
    Lookup -->|미확정| Hold["관련 신규 위험 보류"] --> Lookup
```

독립 원본의 지속성과 NautilusTrader 연동 방법은 검증 대상이며 별도 거래 엔진 구현 결정이 아니다. 최초 창업 Agent와 신규 채용 Agent의 각 통합시험은 서로 대체하지 않는다. [창업 시험](acceptance/first-shift/spec.md) · [채용 시험](acceptance/hired-agent-continuity/spec.md) · [거래 환경 시험](acceptance/execution-parity/spec.md).

```mermaid
flowchart LR
    Observe["운영 · 성과 관찰"] --> Agent["Agent"] --> Candidate["개선 후보"]
    Candidate --> Functional["기능 · 복구 검증"]
    Candidate --> Economic["운용판 순수익 비교"]
    Candidate --> Learning["학습 방법 전이 평가"]
    Functional --> Decision["각 소관 채택 판단"]
    Economic --> Decision
    Learning --> Decision
    Decision --> Work["후속 업무"]
```

최초 운용판의 비용 차감 후 모의 순수익을 먼저 측정하고 이후 동일 조건으로 후보판을 비교한다. 기능 합격, 모의 수익과 [RSI 평가](acceptance/recursive-learning/spec.md)는 각기 다른 증거다. 누락된 비용이나 평가 자료는 0이 아닌 미확정이다.

## 전체 책임 계층

```text
Sourdough
│
├─ 회사 Company
│  │  목적·위임·자산·기록·의무를 지속적으로 소유
│  │
│  ├─ 공통 회사 시스템
│  │  ├─ 조직       회사 설립, 채용, 역할, 퇴직
│  │  ├─ 권한       신원 확인, 위임, 권한 철회
│  │  ├─ 업무       일 발견, 근무, 책임, 다음 행동
│  │  ├─ 협업       대화, 공유, 업무 인계
│  │  ├─ 자원       개인 작업공간, 회사 코드·자료의 소유와 접근
│  │  ├─ 커넥터     시장·거래소 등 외부 자원 연결
│  │  ├─ 재무       예산, 사용 가능 자금, 비용 예약
│  │  ├─ 회계       실제 비용·현금 흐름·회사 장부
│  │  └─ 운영       상태 관찰, 장애 대응, 재시작과 복구
│  │
│  └─ 퀀트 시스템 — 회사가 영위하는 사업
│     ├─ 시장 관측   가격·계좌 정보와 당시 알 수 있었던 시점
│     ├─ 투자 연구   가설, 실험, 전략 평가
│     ├─ 포트폴리오 목표 포지션, 자본 배분
│     ├─ 전략 운용   회사가 소유한 매매 코드와 버전
│     ├─ 거래·위험   주문 승인, 전송, 취소, 원주문 대사
│     └─ 투자 장부   체결, 포지션, 순자산, 수익률
│
└─ 직원 Employee
   └─ Agent — 직원을 소프트웨어로 구현
      ├─ 구성       어떤 런타임·모델로 생성된 Agent인지
      ├─ Harness    업무 맥락 전달, 실행과 도구 요청 중재
      ├─ Desk       허용된 개인 작업공간
      └─ Learning   경험, 개선 후보, 평가와 적용
```

회사·직원은 의미상 주체다. 재무와 포트폴리오는 각각 회사 비용 예산과 투자 노출을 다룬다. 직원의 투자 판단과 시스템의 위험 집행은 다르다. 이사회는 목적과 위임 한도를 정하고 필요할 때 통제하며, 보통의 근무를 지시하거나 승인하지 않는다. Client는 권한 있는 projection 조회와 control-plane 명령을 제공할 뿐 운영의 의존성이 아니다. 공통 기능은 퀀트 계좌나 주문 스키마 없이 동작한다. 계좌별 투자 원장은 예약·포지션의 단일 작성자이며 거래소는 체결 원본이다.

## 자원 할당과 변경 관리

```mermaid
flowchart LR
    Agent["Agent"] -->|자원 요청| Resource["자원 관리"]
    Resource -->|권한 확인| Authority["권한"]
    Resource -->|예산 확인| Finance["재무"]
    Authority -->|판정| Resource
    Finance -->|판정| Resource
    Resource -->|할당 · 회수| Desk["작업공간 · 연결"]
    Agent -->|변경 후보| Assets["회사 자산"]
    Assets -->|후보 버전| Verify["독립 검증"]
    Verify -->|검증 결과| Ops["운영"]
    Ops -->|활성화 · 복구| System["회사 업무 시스템"]
    Ops -->|상태 통지| Comms["사내 소통"]
    Comms -->|결과| Agent
```

자원 요청·예산·권한·실제 할당은 별도 기록이다. 회사 업무 시스템과 허용된 확장은 Agent가 개선할 수 있으며, 작성과 독립 검증 및 활성화는 분리된다. [자원 할당](company/systems/resources/provision-resource/intent.md) · [회사 변경](company/systems/operations/manage-change/intent.md).

## 제품 유지보수와 회사 업데이트

```mermaid
flowchart LR
    Agent["Agent"] -->|결함 · 근거| Work["회사 업무"]
    Work -->|공개 범위 확인| Ops["회사 운영"]
    Ops -->|변경 요청| Connector["외부 커넥터"]
    Connector <-->|Issue · 릴리스| Maint["외부 유지보수"]
    Connector -->|릴리스 정보| Ops
    Ops -->|출처 · 호환성 확인| Update["적용 · 복구 검증"]
    Update -->|적용 결과| Comms["사내 소통"]
    Comms -->|담당 업무 결과| Work
```

제품 기반의 변경은 유지보수 주체에 요청한다. Issue 종료, 릴리스 게시, 우리 회사의 적용 및 원래 결함 검증은 각각 다른 상태다. 기존 위임 범위의 업데이트는 Client 없이 처리하고, 범위를 벗어난 결정만 사람에게 Client로 요청한다. [유지보수 요청](company/systems/operations/request-maintenance/intent.md).

## 1. 채용

```mermaid
flowchart TD
    Need["회사에서 처리할 필요 발견"]
    Judge["기존 Agent가 판단<br/>직접 처리 · 코드 작성 · 인계 · 채용"]
    Choice{"채용이 필요한가?"}
    Other["다른 방법을 선택하고<br/>결과와 다음 확인 조건 기록"]
    Check["회사: 채용 권한 · 예산 확인"]
    Create["회사: 새 Agent ID,<br/>역할 · 한정된 권한 · 예산 기록"]
    Desk["회사: 개인 작업공간과<br/>첫 업무 준비"]
    Accept["새 Agent: 첫 업무를 읽고 수락"]
    Work["첫 근무 시작"]
    Need --> Judge --> Choice
    Choice -->|아니요| Other
    Choice -->|예| Check
    Check -->|권한·예산 없음| Other
    Check -->|허용| Create --> Desk --> Accept --> Work
```

회사는 신원·위임을 기록하고, Agent는 필요와 첫 업무 수락을 판단한다. 다른 모델·런타임은 새 Agent다. 새 역할을 미리 채워 넣을 필요는 없다. [직원 선택 intent](company/systems/organization/organize-work/intent.md) · [채용 intent](company/systems/organization/hire/intent.md) · [생성 spec](company/systems/organization/hire/issue-grant/spec.md) · [수락 spec](company/systems/organization/hire/onboard/spec.md) · [독립 H1 시험](acceptance/episodes.md#h1-hiring) · [채용 후 복구 통합시험](acceptance/hired-agent-continuity/spec.md).

## 2. 개인 업무

```mermaid
flowchart TD
    Wake["예정된 근무 또는 새 관측"]
    Context["회사: 목표 · 권한 · 열린 업무 제공"]
    Inspect["Agent: 회사 상태와<br/>허용된 자료 확인"]
    Choose{"무엇이 가장 필요한가?"}
    Act["조사 · 문서 작성 · 코드 개발"]
    Wait["기다림을 선택하고 이유 기록"]
    Desk["개인 Desk에 작업 저장"]
    Result["결과와 다음 행동을 회사 업무에 기록"]
    Next["다음 조건이 오면 다시 근무"]
    Wake --> Context --> Inspect --> Choose
    Choose -->|일한다| Act --> Desk --> Result
    Choose -->|기다린다| Wait --> Result
    Result --> Next --> Context
```

회사는 책임과 다음 조건을 보존하고 Agent가 무엇을 할지 고른다. Desk의 비공개는 접근 제한이지 개인 소유가 아니다. [업무 발견 intent](company/systems/work/discover-work/intent.md) · [결정 spec](company/systems/work/discover-work/decide/spec.md) · [Desk intent](employee/agent/desk/intent.md) · [Desk 복원 spec](employee/agent/desk/reopen/spec.md) · [W1 시험](acceptance/episodes.md#w1-personal-work).

## 3. 협업

```mermaid
flowchart LR
    A["Agent A<br/>연구 또는 개발"]
    Draft["개인 작업공간<br/>초안"]
    Share["공유 자산<br/>허용된 결과물"]
    Message["대화<br/>질문 · 설명 · 요청"]
    Offer["업무 인계 제안"]
    B["Agent B<br/>자료 확인 · 인계 판단"]
    Accept{"B가 업무를 수락했나?"}
    OwnedA["A가 기존 책임 유지"]
    OwnedB["B가 다음 업무 책임"]
    Complete["회사: 결과와 후속 의무 기록"]
    A --> Draft --> Share --> B
    A --> Message --> B
    A --> Offer --> B --> Accept
    Accept -->|아니요| OwnedA
    Accept -->|예| OwnedB --> Complete
```

공유·메시지 전달·업무 인수는 각각 다른 사실이다. 회사는 수락 전 A의 책임을 유지하고 수락 시 한 명의 책임자로 전환한다. [대화 intent](company/systems/work/conversations/intent.md) · [공유 spec](employee/agent/desk/publish/spec.md) · [인계 intent](company/systems/work/collaborate/intent.md) · [인계 spec](company/systems/work/collaborate/handoff/spec.md) · [W1 시험](acceptance/episodes.md#w1-personal-work).

## 4. 거래

```mermaid
flowchart TD
    Market["시장 데이터<br/>관측 시점과 누락 상태"]
    Research["Agent: 조사 · 가설 · 전략 판단"]
    Program["회사 소유 전략 코드<br/>또는 Agent의 직접 판단"]
    Decision{"보류 또는 주문?"}
    Hold["보류 이유와 다음 조건 기록"]
    Risk["퀀트 위험 관리<br/>권한 · 한도 · 기존 노출 확인"]
    Denied["거부 사유 기록"]
    Reserve["투자 장부<br/>위험 예약과 원주문 ID 기록"]
    Connector["회사 커넥터<br/>허용된 주문만 전송"]
    Venue["모의 거래소<br/>접수 · 체결 · 거절"]
    Ledger["투자 장부<br/>체결 · 포지션 · 잔여 예약 반영"]
    Market --> Research --> Program --> Decision
    Decision -->|보류| Hold
    Decision -->|주문| Risk
    Risk -->|불허| Denied
    Risk -->|허용| Reserve --> Connector --> Venue
    Venue -->|커넥터를 통한 확인된 사실| Ledger
```

Agent와 Agent가 만든 회사 프로그램은 같은 승인 경계를 거친다. 위험 서비스는 허용 여부를 판단하고 투자 보조원장이 예약을 생성·변경·해제한다. 거래소 체결과 회사 장부는 별도 사실이다. [시장 관측 intent](company/profiles/quant/investments/market-snapshot/intent.md) · [연구 intent](company/profiles/quant/investments/research/intent.md) · [주문 승인 spec](company/profiles/quant/trading/admit-intent/reserve/spec.md) · [체결 spec](company/profiles/quant/fund-books/book-fills/post/spec.md) · [첫 통합시험](acceptance/first-shift/spec.md#first-integration-run).

거래소에서 회사 주문과 독립적으로 발생한 계좌 변동도 원본을 대조한다. 출처나 포지션이 미확인인 동안 관련 신규 위험을 막고 후속 조사를 유지하며, Agent가 지시했다고 꾸미거나 장부를 맞추기 위한 가상 체결을 만들지 않는다. [외부 발생 효과 대사 spec](company/profiles/quant/trading/recover-order/reconcile-external/spec.md).

### 같은 거래 코어와 세 가지 검증 환경

```mermaid
flowchart LR
    Strategy["같은 회사 전략 · 권한 · 위험 · 주문 · 장부 코어"]
    Past["과거 시장 데이터<br/>시점 고정"] --> Replay["백테스트<br/>가상 시계 · 가상 체결"]
    Current["현재 실제 시장 데이터<br/>읽기 전용"] --> Paper["실시간 모의 운용<br/>독립 가상 계좌 · 가상 체결"]
    Strategy --> Replay
    Strategy --> Paper
    Strategy --> Test["외부 테스트 환경<br/>별도 권한의 기능 시험"]
    Strategy --> Live["실제 거래 커넥터<br/>별도 승인·검증 전 사용 불가"]
    Replay --> Results["각 환경별 결과 · 비용 · 불확실성"]
    Paper --> Results
    Test --> Results
    Live --> Results
```

현재 시장 데이터는 읽기 전용 source binding이고 주문 목적지는 별도 execution binding이다. 테스트 환경이 최신 시각을 표시해도 실제 시장 호가·유동성과 같다는 뜻은 아니다. [백테스트 spec](company/profiles/quant/investments/backtest/replay/spec.md) · [실시간 모의 spec](company/profiles/quant/trading/live-paper/run/spec.md) · [공통 코어 spec](company/profiles/quant/trading/execution-environment/bind/spec.md) · [공통 코어 시험](acceptance/execution-parity/spec.md).

회사가 선택한 거래 런타임을 직접 준비·설정·실행·감독하고, 런타임의 거래·백테스트·모의 기능을 사용한다. 회사는 권한·멱등성·재시도·복구를 책임지며 거래 엔진을 새로 구현하지 않는다. [실행·설정 spec](company/profiles/quant/trading/execution-environment/launch/spec.md) · [통합 검증 spec](company/profiles/quant/trading/execution-environment/qualify-engine/spec.md). 런타임 선택은 설계 결정이며, 아직 설치·검증·운용됐다는 뜻은 아니다.

```mermaid
flowchart LR
    Agent["Agent 또는 회사 서비스<br/>권한 내 운용 설정"]
    Config["회사: 버전·모드·시장·전략·계좌 설정<br/>검사 · 영속화"]
    Supervise["회사: 런타임 준비 · 실행 · 상태 확인 · 재시작"]
    Runtime["거래 런타임<br/>데이터 · 백테스트 · 모의 · 거래"]
    Control["회사: 권한 · 위험 · 원주문 · 복구"]
    Gateway["회사 외부 커넥터"]
    External["시장 데이터 · 모의 또는 외부 거래소"]
    Agent --> Config --> Supervise --> Runtime
    Runtime -->|요청·결과| Control
    Control -->|허용된 작업| Runtime
    Runtime <-->|허용된 요청| Gateway
    Control <-->|원본 조회| Gateway
    Gateway <-->|연결된 목적지| External
```

화면은 설정·상태를 표시할 수 있지만 위 경로는 Client가 닫혀 있어도 동작한다. 런타임 실행 상태와 기능별 거래·복구 합격 증거는 별도로 기록한다.

## 5. 복구

```mermaid
flowchart TD
    Send["회사가 원주문을 전송"]
    Fill["모의 거래소가 체결"]
    Crash["회사가 체결 응답을 받기 전 종료"]
    Restart["독립 감독자가 회사 자동 재시작"]
    Load["원래 저장소에서<br/>미해결 원주문과 위험 예약 확인"]
    Query["거래소에 원주문 ID 조회"]
    Known{"결과가 확인됐나?"}
    Unknown["미확인 상태 유지<br/>예약 보존 · 재조회"]
    Book["확인된 체결을 한 번만 장부에 반영"]
    Wake["같은 Agent의 다음 근무"]
    Decide["복구된 포지션을 읽고 새 판단"]
    Send --> Fill --> Crash --> Restart --> Load --> Query --> Known
    Known -->|아니요| Unknown --> Query
    Known -->|예| Book --> Wake --> Decide
```

정상적인 재시작·대사·다음 근무는 이사회나 Client를 기다리지 않는다. 조회가 불명확하면 원주문 예약을 유지한다. 체결과 회계 전달 대기 기록은 함께 보존한다. 다른 실행 구성의 Agent는 같은 Agent로 취급하지 않고 별도 인계한다. [감독 spec](company/systems/work/resume-work/supervise/spec.md) · [원주문 대사 spec](company/profiles/quant/trading/recover-order/reconcile/spec.md) · [회계 전달 spec](company/profiles/quant/fund-books/book-fills/journal-source/spec.md) · [창업 시험](acceptance/first-shift/spec.md) · [채용 시험](acceptance/hired-agent-continuity/spec.md).

## 6. 학습과 RSI

```mermaid
flowchart TD
    Work["업무·거래·장애 결과"]
    Review["Agent: 예상과 실제의 차이 조사"]
    Candidate["기억 · 업무 방법 · 전략 개선 후보"]
    Eval["기존 방법과 후보를<br/>분리된 사례에서 비교"]
    Pass{"권한·안전·평가 기준을<br/>충족했나?"}
    Reject["후보 기각 · 기존 방법 유지"]
    Adopt["권한 있는 Agent가 채택<br/>버전과 복원 대상 기록"]
    Shift["다음 실제 근무에서 사용 확인"]
    Regression{"나중에 퇴행했나?"}
    Rollback["기존 방법으로 복원"]
    Continue["계속 관찰"]
    RSI["별도 RSI 시험<br/>개선 절차가 새 문제에서도<br/>더 나은 방법을 만드는가?"]
    Work --> Review --> Candidate --> Eval --> Pass
    Pass -->|아니요| Reject
    Pass -->|예| Adopt --> Shift --> Regression
    Regression -->|예| Rollback
    Regression -->|아니요| Continue
    Review --> RSI
```

업무 방법을 한 번 고친 것과 개선 절차가 새로운 문제에 일반화되는 것은 다른 주장이다. Agent Learning은 후보와 평가를 소유하고 회사 권한은 적용 범위를 집행한다. [학습 intent](employee/agent/learning/methods/intent.md) · [비교·적용 spec](employee/agent/learning/methods/evaluate/spec.md) · [L1 시험](acceptance/episodes.md#l1-learning) · [독립 RSI 시험](acceptance/recursive-learning/spec.md).

## 7. 순수익 확인

```mermaid
flowchart LR
    Venue["거래소 원본<br/>체결 · 수수료 · 펀딩 · 자금 흐름"]
    Position["투자 장부<br/>포지션 · 거래 손익"]
    Costs["회사 회계<br/>모델 · 데이터 · 인프라 등 실제 비용"]
    Marks["같은 기준 시점의 가격 · 자산 평가"]
    Coverage{"자료가 모두 확인됐나?"}
    Unknown["회사 순수익 미확정<br/>누락 항목과 재확인 책임 표시"]
    Net["회사 순손익 · 순수익률 계산<br/>외부 입출금과 구분"]
    Client["Client에서 결과 조회"]
    Review["Agent: 결과 해석 · 다음 운용 판단"]
    Venue --> Position --> Coverage
    Costs --> Coverage
    Marks --> Coverage
    Coverage -->|아니요| Unknown --> Client
    Coverage -->|예| Net --> Client
    Net --> Review
    Unknown --> Review
```

거래 이익 12, 거래비용 4, 회사 운영비 10이면 거래 순손익은 +8, 회사 순손익은 −2다. 비용 증빙이 없으면 회사 순수익은 미확정이다. Client는 계산하지 않고 회사 projection을 표시한다. [회사 회계 intent](company/systems/accounting/accounting-close/intent.md) · [경제성과 intent](company/profiles/quant/fund-books/fund-economics/intent.md) · [원천 대사 spec](company/profiles/quant/fund-books/fund-economics/reconcile-sources/spec.md) · [수익률 spec](company/profiles/quant/fund-books/fund-economics/total-return/spec.md) · [P1 시험](acceptance/episodes.md#p1-total-return).

## 8. 해고와 의무 인계

```mermaid
flowchart TD
    Need["Agent가 인력 조정 필요 판단"] --> Grant{"정당한 철회 권한?"}
    Grant -->|없음| Deny["거부 · 기존 권한 유지"]
    Grant -->|있음| Fence["회사: 새 업무와 이전 실행 차단"]
    Fence --> Inventory["업무 · 자산 · 미확인 외부 효과 확인"]
    Inventory --> Owner{"인계 수락 또는 회사 복구 책임자?"}
    Owner -->|없음| Pending["퇴직 대기 · 후속 확인 유지"]
    Owner -->|있음| Transfer["자산 관리·업무 인계<br/>이전 private 접근은 복사하지 않음"]
    Transfer --> Settle["기존 주문·체결 등은 소유 시스템이 계속 대사"]
    Settle --> Retired["의무 소유와 차단 확인 후 퇴직"]
```

직원의 권한은 끝날 수 있지만 회사 의무는 사라지지 않는다. [해고 intent](company/systems/organization/offboard/intent.md) · [차단 spec](company/systems/organization/offboard/fence/spec.md) · [자산·의무 정리 spec](company/systems/organization/offboard/settle-custody/spec.md) · [O1 시험](acceptance/episodes.md#o1-offboarding).

## 순서와 증거 경계

먼저 창업 Agent의 무인 복구를 한 번의 연속 관측으로 검증하고, 새로 채용된 Agent의 별도 통합시험을 유지한다. 이후 첫 운용판의 회사 순수익을 전향적으로 측정하고, 후보판이 생기면 새로운 같은 기간에 기존판과 비교한다. RSI는 별도로 평가하며 수익 관측을 막지 않는다. 실행 순서와 독립 증거는 [통합·인수](README.md#integration-and-completion)와 [구현·증거 순서](README.md#build-and-evidence-order)를 따른다.
