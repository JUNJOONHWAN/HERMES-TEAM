# HERMES-TEAM 아키텍처 구현 보고서

문서 상태: 구현 기준 문서

대상: 이 저장소의 실제 코드

배포 기본값: OpenCode + 동적 무료 모델 라우팅
상위 기반: Nous Research Hermes Agent, MIT License

## 1. 결론

이 배포판은 단순한 Hermes profile 모음이 아니다. Hermes 루트를 작업자가
아닌 제어면으로 제한하고, 실행 권한을 버전된 Role Shell에 두며, 실제
모델/CLI는 교체 가능한 adapter로 취급한다. Kanban 카드가 작업 단위,
Binding이 역할과 실행기의 연결, Override가 변경 범위, Receipt가 완료
게이트다. Timeline·Code Map·NeuralLink는 모든 실행의 공유 증거면이고,
Heartbeat는 구성 상태·서비스/스케줄·산출물의 세 층으로 운영 상태를
분리한다.

공개 benchmark가 없으므로 다른 시스템보다 “몇 배 빠르다”거나 상위
몇 퍼센트라고 주장하지 않는다. 확인 가능한 차별점은 모델 선택이 아니라
역할 계약, 변경 범위, 증거 체인, 롤백을 런타임 데이터 구조로 만든 데 있다.

## 2. 실제 구성 트리

```text
Hermes root (zero-MCP control plane)
├─ Project Manager (Project DB)
│  ├─ stable p_* identity / phase / milestone
│  ├─ pa_* code-card approval queue
│  ├─ repository identity / branch policy
│  └─ commit / push audit events
├─ Kanban card / run / claim lock
├─ Supervisor registry (SQLite)
│  ├─ immutable Role Shell versions
│  ├─ executors (Hermes profile or command adapter)
│  ├─ many-to-many Bindings
│  ├─ once / temporary / permanent Overrides
│  └─ adapter events and health state
├─ isolated worker profiles
│  ├─ general
│  ├─ market (public-source frame)
│  ├─ browser
│  ├─ universal provider-neutral fallback
│  └─ multitool manager
├─ evidence graph
│  ├─ Timeline hash chain
│  ├─ repository Code Map + stored slices
│  ├─ NeuralLink incremental recall index
│  └─ typed Roadmap events/projections
└─ three-layer Heartbeat
   ├─ configuration
   ├─ service_schedule
   └─ artifacts
```

주요 구현 파일:

| 책임 | 파일 |
|---|---|
| 프로필·Role Shell·기본 Binding·controller 후보 | `hermes_cli/supervisor_bootstrap.py` |
| registry·선택·Override·Receipt 검증 | `hermes_cli/supervisor_registry.py` |
| bound worker prompt와 실행 환경 | `hermes_cli/executor_adapter.py` |
| 외부 CLI 신뢰 경계 | `hermes_cli/external_cli_adapter.py` |
| OpenCode 무료 모델 발견/health/cooldown | `hermes_cli/opencode_free_router.py` |
| HERMES-TEAM 설치 | `hermes_cli/public_edition.py`, `scripts/setup_public_edition.py` |
| 세 층 Heartbeat | `hermes_cli/supervisor_cli.py`, `hermes_cli/artifact_health.py` |
| Timeline/Code Map/NeuralLink/Roadmap | `extensions/hermes-timeline-code-map/` |
| Project DB·승인·Git 수명주기 | `hermes_cli/projects_db.py`, `hermes_cli/project_card_controller.py` |
| Project/Card Web API | `plugins/kanban/dashboard/plugin_api.py` |
| Telegram 이중 ID·재생 방지 | `gateway/kanban_watchers.py`, `hermes_cli/kanban_db.py` |

## 3. 제어면과 실행면

루트 설정의 `mcp_servers`는 비워지고 toolset은 supervisor, kanban, cronjob로
제한된다. 루트는 카드를 분해·배정·감사하지만 도메인 작업을 직접 수행하지
않는다. MCP와 웹/브라우저/터미널은 역할별 worker profile에만 배치한다.

이 분리는 모든 도구를 한 모델에 노출하는 구성보다 blast radius를 줄인다.
반면 profile 수와 registry 운영 비용이 늘고, 필요한 capability를 잘못
분류하면 카드가 fail-closed로 막힌다.

## 4. Role Shell

Role Shell은 모델 이름이 아니라 불변 계약 버전이다. 현재 역할은 code,
market, browser-research, operations, report, verification, tool-management다.

각 Shell은 다음을 가진다.

- required capabilities: 실행기가 반드시 제공해야 하는 교집합
- allowed capabilities: 실행기가 강해도 넘을 수 없는 상한
- instructions: 역할별 행위 계약
- evidence policy: Timeline, NeuralLink, Code Map slice, 출력, verify gate
- contract hash와 supersedes 연결

adapter를 바꿔도 Shell은 유지된다. Codex를 붙였다고 브라우저 권한이
생기지 않고, Grok을 붙였다고 거래 쓰기 권한이 생기지 않는다. 계약을
바꾸려면 새 Shell 버전을 만들고 기존 Binding을 새 head로 승계해야 한다.

## 5. Binding과 Override

Binding은 Shell ↔ executor의 many-to-many 연결이다. priority, weight,
capability cap, primary/candidate 책임, enable 상태를 저장한다.

Override는 세 범위로 분리된다.

- once: 특정 작업 한 번, 사용 후 자동 소진
- temporary: 만료 시각까지
- permanent: 명시적으로 clear할 때까지

Override 생성·사용·실패·clear는 adapter event로 남는다. 이 설계의 장점은
자동 라우팅보다 “누가 언제 왜 바꿨는가”를 설명하기 쉽다는 점이다. 단점은
승격 판단이 자동 비용 최적화 시스템보다 느리고 운영자가 책임져야 한다는
점이다.

## 6. Receipt 완료 게이트

bound run은 구조화 Receipt 없이는 완료할 수 없다. registry는 모델이
제출한 task/run/shell/executor/binding ID를 신뢰하지 않고 DB의 claim
provenance와 비교해 다시 stamp한다.

모든 기본 Shell은 다음을 요구한다.

- 정확한 Timeline goal ID
- `context_loaded=true`
- NeuralLink recall 수행 여부, query, candidate count, context chars
- action/output node IDs
- `verify_all.invalid_count=0`
- 구조화 outputs

code Shell은 추가로 저장된 `slice_ids`를 요구한다. 비코드 작업에서 억지
repository slice를 만들지 않는다.

## 6A. Project/Card Manager와 승인 게이트

장기 업무는 하나의 거대한 카드가 아니라 안정적인 `p_*` Project 아래에 여러
불변 `t_*` 실행 카드를 연결한다. Project DB와 Kanban DB의 책임은 분리된다.

| 원장 | 소유 상태 |
|---|---|
| Project DB | Project 상태·진행 단계·마일스톤·다음 행동·`pa_*` 승인·저장소·Git 이벤트 |
| Kanban DB | `t_*` 카드·typed link·run·claim·receipt·댓글·알림 cursor |

컨트롤러만 두 원장 사이의 상태 전이를 수행한다. 실행 adapter는 카드를 실행하고
후속 작업을 제안할 수 있지만 Project 상태, root card, typed relation을 직접 쓰지
못한다. 그래서 코드·브라우저·시장·운영 worker가 교체되어도 회사형 프로젝트의
관리 규칙은 동일하게 유지된다.

코드 후속 작업은 즉시 `t_*`를 만들지 않는다. 먼저 `pa_*` 제안만 저장하고
Project를 `paused`로 바꾼다. 운영자가 다음 Telegram/Web UI/CLI 동작에서 승인해야
정확히 한 개의 `t_*`가 생성된다. 같은 제안을 반복하면 기존 pending 승인을
돌려주며, 거절하면 카드가 생기지 않는다. 승인 생성 실패 시에는 `pa_*`와
`paused` 상태가 복원되어 자동 실행으로 새지 않는다.

실행 중 범위·산출물·Role Shell·완료 조건이 크게 바뀌면 worker 입력 스트림에 새
프롬프트를 주입하지 않는다. 컨트롤러의 `request_direction_change`가 먼저 후속
계약을 검증하고 Project를 pause한 뒤, 원본 `t_*`를 archive하여 프로세스 그룹을
종료한다. Git 작업공간은 그 시점의 변경을 checkpoint commit으로 고정하고, Git이
아닌 작업공간은 파일을 그대로 보존하며 `not_applicable`을 기록한다. 그 다음에도
새 카드는 만들지 않고 `pa_*` 승인 초안만 Project DB에 쓴다.

승인 후 생성되는 후속 카드는 원본과 비차단 `references`로 연결된다. 의도적으로
미완료인 원본이 후속을 `todo`에 묶지 않으면서도, 원본 댓글·run·checkpoint SHA와
후속 결과를 수개월 뒤 다시 추적할 수 있다. 승인 거절은 후속 카드를 만들지 않으며
원본 archived 감사 기록도 삭제하지 않는다. Telegram과 Web UI는 이 동일 컨트롤러
함수를 호출하고 `p_*`, 원본 `t_*`, checkpoint, `pa_*`를 함께 표시한다.

일반 Kanban `t_*` 카드는 Project 소속 여부와 관계없이 같은 ID로 중지·재개·지시
변경이 가능하다. `pause_card`는 먼저 카드를 durable `operator_pause` 상태로 옮긴
뒤 그 카드에 기록된 host-local worker process group만 종료한다. gateway, 다른 카드,
자동화 정의는 건드리지 않는다. `resume_card`는 종료할 worker PID가 남지 않았을 때만
같은 `t_*`를 dispatch queue로 되돌리고 새 run attempt를 시작한다.

`steer_card`는 같은 안전 중지를 수행하고 새 지시를 durable comment로 저장한 다음
같은 `t_*`를 재개한다. 작업자 종료가 확인되지 않으면 카드는 paused 상태를 유지한다.
다만 Project 카드의 산출물·범위·Role Shell·완료 조건이 달라지는 큰 변경은 이 경로가
아니라 `request_direction_change`와 별도 `pa_*` 승인을 사용한다.

`pause_project`는 실행 카드가 있을 때 거부하고, 성공하면 active pointer를
해제하며 모든 새 카드 쓰기를 막는다. `reopen_project`는 명시적 운영자 동작이다.
따라서 M0/M1 결과가 다음 마일스톤을 제안해도 M2가 자동 발행되는 불도저 구조가
아니다.

### 저장소와 커밋/푸시

Project는 `none`, `existing`, `init_local`, `github` 네 repository mode를 가진다.
GitHub mode는 private/public 선택과 원격 identity를 Project DB에 기록한다. 코드
카드는 카드별 worktree branch에서 checkpoint commit을 만들고 필요할 때 그
branch만 push한다. `main`, `master`, 기본 branch 직접 push는 컨트롤러가 거부한다.
저장소 생성·commit·push의 branch, SHA, 결과는 `project_git_events`에 누적된다.

Web UI와 Telegram은 항상 `Project: p_*`와 `Card: t_*`를 함께 표시한다. 신규 알림
구독은 현재 event cursor에서 시작하고 terminal/archived 구독은 제거하므로 과거
실패 이벤트가 새 구독자에게 폭탄처럼 재생되지 않는다.

## 7. Timeline, Code Map, NeuralLink

### Timeline

행동·출력·판단을 append-only node/edge로 기록하고 hash chain을 검증한다.
완료 직전 `verify_all`이 한 건이라도 무효면 Receipt가 거절된다.

### Code Map

repo 파일과 관계를 hot index로 만들고 작업 query에 맞는 bounded slice를
저장한다. slice는 editable scope를 자동 확대하는 권한이 아니라 변경 영향
증거다.

### NeuralLink

별도 embedding server 없이 Timeline node의 lexical/metadata/entity/time
feature와 graph hop을 증분 색인한다. Hermes 플러그인이 매 LLM turn 전에
bounded candidate recall을 수행한다. 비어 있으면 아무 context도 주입하지
않으며 Shell Receipt에는 0 candidate로 기록한다.

장점은 작은 로컬 의존성, 결정론적 색인, 감사 가능한 후보다. 한계는 추상적
의미 유사성, alias 품질, 2,600자 context cap, 최종 모델 reranking 의존성이다.
따라서 “메모리 문제 해소”가 아니라 실패 모드를 단순하고 관찰 가능하게
바꾼 것으로 평가한다.

## 8. 모델·provider adapter

| 경로 | 기본 여부 | 인증/활성 조건 | 용도 |
|---|---:|---|---|
| OpenCode free | 기본 후보 | live catalog에서 명시적 무료 모델 + tool smoke | controller와 code/ops/report/verification worker |
| Codex | 선택 | 기존 Codex 인증 + CLI/runtime probe | controller 또는 command worker |
| Grok | 선택 | `XAI_API_KEY` + xAI catalog/tool probe | controller |
| OpenRouter | 선택 | `OPENROUTER_API_KEY` + catalog/tool probe | controller |
| local vLLM | 선택 | local `/v1/models` + tool probe | controller |
| generic CLI | 선택 | JSON spec의 binary/probe/capability 계약 | 임의 worker |

OpenCode 무료 라우터는 고정 모델 이름을 snapshot test하지 않는다. 매 실행
catalog에서 무료 표시가 명시된 모델만 받고, health TTL과 cooldown을 적용해
강한 후보부터 선택한다. 실패한 작업 prompt를 다른 모델에 자동 재생하지
않아 중복 side effect를 피한다.

## 9. Market 기본 프레임

HERMES-TEAM에는 개인 데이터/API/노하우가 없다. market Shell은 공식 거래소,
규제기관, 발행사와 문서화 API를 우선하며 Yahoo Finance와 Naver Finance를
공개 discovery/cross-check surface로 사용할 수 있다. URL, 조회 시각,
시장/timezone, source state를 남기고 거래·계좌 쓰기를 금지한다.

개인 노하우는 두 방식으로 추가할 수 있다.

1. `market_memory.jsonl`: operator가 직접 add한 누적 기억. 읽기는 선택,
   쓰기는 명시 승인일 때만 한다.
2. role-scoped skill/MCP/research policy: tool-management Shell이 provenance,
   health, backup, rollback을 확인한 뒤 market profile에만 장착한다.

어느 방식도 Shell capability를 자동 확대하지 않는다.

## 10. 세 층 Heartbeat

### configuration

root isolation, active Role Shell route, worker health, Receipt missing count,
Timeline hash, Code Map index 수, NeuralLink node/index/pending 수를 본다.

### service_schedule

operator가 `expected_services`, `required_cron`, `expected_paused_cron`에 등록한
항목만 검사한다. 목록이 비면 미구성이 정상이다. heartbeat 자체 cron만
기본 required다.

### artifacts

기본은 `enabled=false`, `checks=[]`이다. 사용자가 path contract를 추가하면
file/directory 종류, required/optional, 최소 크기, 최대 age, SHA-256을 검사한다.
셸 명령은 실행하지 않아 Heartbeat 자체의 side effect를 차단한다.

## 11. 보안·배포 경계

- 비밀키를 저장소나 adapter JSON에 넣지 않는다.
- command adapter는 shell mode를 금지하고 argv 배열만 허용한다.
- prompt 소비와 capability enforcement가 없으면 등록을 거부한다.
- health gate 실패 adapter는 비활성으로 되돌린다.
- private absolute path, 개인 노하우, 개인 MCP alias, 개인 service/cron은 포함하지 않는다.
- upstream MIT license와 저작권 고지를 유지한다.

### macOS 호환 경계

- 게이트웨이 서비스는 launchd를 사용하고 Linux 컨테이너 전용 s6 경로와
  분리한다.
- stock macOS Bash 3.2에 없는 `BASHPID`에 의존하지 않고 `mktemp`로 동시
  snapshot 파일을 격리한다.
- shutdown 진단은 GNU `timeout` 대신 Python subprocess timeout을 사용하고,
  `ps -axo`, `sysctl`, `log show`의 macOS 진단 경로를 제공한다.
- `/private/var/folders` 아래 사용자 임시 파일은 정상 처리하지만
  `/private/var/db`와 Docker socket 같은 시스템 대상은 write guard가
  계속 거부한다.
- s6 event directory의 엄격한 `03730` 검증은 Linux live-container가
  담당한다. macOS 개발용 임시 디렉터리에서 커널이 setgid만 제거한
  `01730`은 허용하며, macOS 런타임은 이 s6 경로를 사용하지 않는다.

## 12. 알려진 단점

- governance data와 profile 수가 늘어 초기 학습 비용이 크다.
- Shell capability 모델이 부정확하면 유효한 adapter도 라우팅되지 않는다.
- OpenCode 무료 catalog와 품질은 외부 상태라 항상 사용 가능하다고 보장할 수 없다.
- NeuralLink는 embedding semantic search의 완전한 대체가 아니다.
- Receipt는 provenance와 필수 증거 모양을 강제하지만 모든 모델 판단의 진실성을
  수학적으로 증명하지 않는다.
- controller/worker가 서로 다른 provider일 수 있어 비용·latency·문체가 달라질 수 있다.

## 13. 재현 기준

다른 구현자가 따라 하려면 다음 순서를 지킨다.

1. upstream Hermes를 설치하고 config를 초기화한다.
2. Timeline extension과 NeuralLink plugin을 설치한다.
3. Timeline만 담은 MCP catalog를 만든다.
4. bootstrap으로 zero-MCP root와 격리 profile을 만든다.
5. Shell/Binding/Receipt DB schema를 설치한다.
6. OpenCode를 등록하되 health 통과 전 활성화하지 않는다.
7. 다른 provider는 candidate로만 추가한다.
8. once override로 시험하고 temporary, permanent 순으로 승격한다.
9. 세 층 Heartbeat와 Receipt/Timeline 무결성을 최종 확인한다.

AI가 실제로 운영하거나 확장할 때는 `AI_OPERATIONS_MANUAL.md`를 최우선으로
읽는다.

## 14. 공개 검증과 업스트림 버전업

정식 public-core 검증은 다음 한 명령이다.

```bash
scripts/run_tests.sh -j 8 \
  --exclude-manifest distribution/validation/test_exclusions.json
```

제외 manifest는 선택형 ACP 의존 테스트 파일과 독립 재현된 업스트림
Anthropic baseline 3개 node ID만 담는다. glob이 0개 파일과 일치하거나 node
ID가 사라지면 runner가 fail-closed로 중단된다. 새로운 업스트림 테스트는
별도 등록 없이 자동 포함된다. context compressor, Timeline, Code Map,
NeuralLink, Heartbeat와 HERMES-TEAM supervisor 경로는 제외 대상이 아니다.

버전업은 `distribution/upgrade/contract.json`과
`scripts/check_hermes_team_upgrade.py`가 공개 upstream baseline, 현재 HEAD,
대상 upstream commit의 공통 계보와 3-way merge 충돌을 검사한다. 런타임
패키지명·CLI·설치 경로를 유지했기 때문에 제품명 변경이 upstream update를
차단하지 않는다. 충돌은 자동 덮어쓰지 않고 경로를 보고하며, 해결 후 위
전체 검증을 다시 통과해야 완료다.
