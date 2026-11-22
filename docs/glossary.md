# Domain glossary

[System map](README.md) · [Design conventions](conventions.md). Use the English term in contracts and the Korean gloss in discussion. An owner is the component entitled to change a fact, not the screen showing it.

## Actors and authority

| Term | Meaning and owner | Not the same thing |
| --- | --- | --- |
| Company (회사) | Persistent owner of mandate, assets, obligations and records. | Not a provider account, a process or a trading account. |
| **Employee** (직원) | Organizational participant who accepts duties, exercises judgment and answers for results. In Sourdough an Agent realizes this participant. | Not a department, role title, runtime session or second database identity. |
| Company system (회사 시스템) | Mechanism that implements a company responsibility and owns a defined record or transition, such as recording a grant. | Not the Company, an employee, or necessarily a separate process. |
| Company-managed software (회사 관리형 소프트웨어) | Software whose configured instance the company provisions and operates, such as its selected trading runtime. | Neither necessarily authored by the company nor an externally operated venue. |
| External service / connector (외부 서비스 / 외부 커넥터) | A service operated by another party / the company-controlled boundary for a scoped operation and observed result. | A connector is not the external service, a grant, a trading engine or the authority over the external source of record. |
| Candidate / effective revision (후보 / 적용 버전) | An immutable proposed company asset / the authorized version actually activated by its domain owner. | Sharing, passing a functional test, activation and economic improvement are distinct facts. |
| Business profile (사업 프로필) | Optional domain rules, resource types and measures used for a particular business. | Not an Agent execution profile, universal job description or test fixture. |
| Board (이사회) | Human governance principal that sets purpose and delegated limits. | Not the Board Client or the control plane. |
| **Agent** (에이전트) | One company work actor with its own `agent_id` and immutable execution profile selected at creation. It can take multiple shifts. In the organization it is an *employee* (직원). | Not a position, session or model name. Two hires using the same profile are still two Agents. A different bound runtime is a different Agent; there is no second technical identity for the employee. |
| Principal (인증 주체) | Identity authenticated for a request; may represent a Board member, Agent, program or service. | Not every principal is an Agent. An ID is not authorization. |
| Role / grant / mandate (역할 / 위임 권한 / 헌장) | A role describes responsibility; a grant permits specified operations on resources under limits; the mandate sets standing policy. Organization assigns roles, authority validates grants and governance versions the mandate. | A title, prompt or visible control is not permission. |
| Custody / work ownership (자산 관리 / 업무 책임) | Custody identifies who is responsible for an asset. Work ownership identifies who owes the next disposition of a duty. | Access, receipt of a message and acceptance of a duty are different facts. |
| Resource request / assignment (자원 요청 / 할당) | A request proposes scoped use; the resource owner records the actual provision after authority and budget checks. | Neither a request nor a budget alone is an access grant or evidence that an external resource is available. |

## Work and execution

| Term | Meaning and owner | Not the same thing |
| --- | --- | --- |
| Work item / shift (업무 / 근무) | A work item is a durable duty with an owner and next condition. A shift is an activation for due work. The first integration's *next shift* requires a distinct work ID and a fresh decision. | Resuming the interrupted work or redelivering a wake is not the next shift. |
| Wake / claim / generation (기동 사유 / 점유 / 세대) | A wake makes work eligible; a claim assigns one current writer; the scheduler uses its generation to fence obsolete callbacks. | A notification is not acceptance or permission to act. |
| **Agent Harness** (실행 조율기) | Product component that prepares scoped context, checks the assigned Agent’s admitted binding, mediates tool calls and settles run obligations. | Not the Agent, a model, a vendor SDK or the external **test driver** that observes and injects declared faults. |
| Execution profile (실행 구성) | Immutable choice of runtime implementation, adapter revision, provider and model revision for constructing an Agent. A session/run is one execution of that binding. | Reopening the same binding after process loss is recovery. Choosing a different runtime or model is a new configuration for a new Agent, not an in-place fallback. |
| Runtime adapter / Agent Runtime / model (실행 어댑터 / 실행기 / 모델) | The adapter translates the company run protocol; the runtime executes turns; the model supplies inference. | A provider handle or model name cannot identify the company's Agent or prove portability. |
| Run / turn / session (실행 시도 / 턴 / 세션) | A run is a company-accounted execution attempt. A turn is a bounded runtime interaction. A native session is a runtime-specific context handle. | Keep work, run, generation and provider handles distinct. Native resume is optional; recovery of company receipts is not. |
| Tool request / action / receipt (도구 요청 / 논리 행동 / 처리 기록) | A runtime requests a tool call; the company identifies one logical action across retries; the owning service records what it admitted, rejected or observed. | A new provider call ID does not authorize a second economic action. |
| Resource connector (자원 커넥터) | Scoped port to an external resource with explicit operation and recovery capabilities. | An adapter runs an Agent; a connector accesses a resource. A venue is one concrete resource. |
| Resource / Agent Desk / scratch (회사 자원 / 개인 작업공간 / 임시 공간) | The inventory owns asset identity and custody. A Desk resolves one Agent's permitted durable working set; scratch may disappear with the run. | A desk is neither personal ownership of a host nor a security boundary merely because it is a directory. |

## Learning and evidence

| Term | Meaning and owner | Not the same thing |
| --- | --- | --- |
| **Agent Learning** (에이전트 학습) | An Agent capability: propose a correction from observed outcomes, evaluate it and observe its use in a later shift. Lives under Agent → Learning. | Not a corporate actor called Learn, model training, or automatic policy modification. |
| Recursive self-improvement / RSI (재귀적 자기개선) | A testable claim that experience improves the procedure used to produce subsequent methods. Agent Learning owns procedure and method lineage; an independent evaluator measures transfer to new cases. | Remembering an episode, adopting one correction, switching models or making a profit does not establish RSI. |
| Evidence / memory / method (근거 / 기억 / 방법) | Evidence refers to a checkable source. Memory is a scoped, fallible interpretation. A method is a versioned procedure with an explicit adoption scope. | A private note is not a ledger fact. Sharing an artifact does not adopt its contents. |
| Evaluation / promotion (평가 / 채택) | Evaluation compares candidate and incumbent on sealed cases. Promotion selects an effective revision under authority for an Agent, project or company. | A good score is not a promotion. A company-owned shared method is not a second learning engine. |
| Research finding / decision (조사 결과 / 판단) | A quant finding binds a question and result to as-of inputs and an author; the later decision explains how that Agent used it. | A finding is not a grant or an order. |

## Client, control and finance

| Term | Meaning and owner | Not the same thing |
| --- | --- | --- |
| Projection / presentation (프로젝션 / 화면 표현) | The service produces an authorized read model with revision, as-of and coverage. The client renders it and may locally filter already permitted data. | Neither a read cache nor the displayed value is the source of truth. A read cannot trigger business work. |
| **Board Client / control plane / operating loop** (클라이언트 / 제어면 / 업무 실행) | The optional client presents projections and submits explicit governance commands. The service control plane authenticates and authorizes them; the domain owner enforces the effective change. | The client is a control-plane **interface**, not the control plane. Pending on screen does not mean applied. |
| Query / command / event (조회 / 명령 / 사건 기록) | A query reads; a command requests a mutation; an event records a fact at its source. | Do not disguise a wake, venue refresh or order as a query. |
| Feature intent / order intent / order / fill (기능 의도 / 주문 의도 / 주문 / 체결) | An `intent.md` describes a desired feature outcome. An order intent proposes an economic action; admission precedes dispatch; the venue establishes a fill. | A specification, a submit acknowledgment and a confirmed fill are different records. |
| Exposure reservation / spending commitment / cash (위험 예약 / 지출 약정 / 현금) | Trading reserves order headroom. Finance reserves operating budget. Accounting establishes posted cash. | Unused budget, liquidity and trading headroom are not interchangeable. |
| Investment subledger / general ledger / return (투자 보조원장 / 회사 총계정원장 / 수익률) | Positions and fees come from deduplicated fills. Company accounting maps economic sources to balanced journal entries. Quant performance computes flow-adjusted returns. | P&L is an amount, return is a percentage. The client calculates neither. |
| Net ROI objective (순수익 목표) | Earn a positive return on company capital after trading and operating costs within the mandate. Quant performance reports signed company net P&L and flow-adjusted TWR for a specified period and perimeter. | Trading-only return excludes company costs; relative outperformance while losing capital is not absolute profit. |
