# HERMES-TEAM 빠른 시작

## 1. 전제

- Python 3.11~3.13
- Git
- Node.js/npm: OpenCode가 없을 때 공식 npm 패키지 `opencode-ai`를 설치하는 데 사용
- 초기화된 Hermes 홈: 먼저 `hermes setup`을 한 번 실행해 `~/.hermes/config.yaml`을 만든다.

OpenCode 공식 문서의 설치 방법은 <https://opencode.ai/docs>에 있다. 이
배포판의 설치기는 셸 파이프를 실행하지 않고 `npm install -g opencode-ai`를
argv 방식으로 실행한다.

## 2. 설치

```bash
git clone https://github.com/JUNJOONHWAN/HERMES-TEAM.git
cd HERMES-TEAM
python3 -m pip install -e .
hermes setup
python3 scripts/setup_public_edition.py
```

먼저 변경 계획만 보려면:

```bash
python3 scripts/setup_public_edition.py --dry-run
```

설치기가 수행하는 일:

1. OpenCode 탐색 후 필요하면 공식 npm 패키지 설치
2. `extensions/hermes-timeline-code-map[mcp]` editable 설치
3. Timeline MCP와 OpenCode용 MCP 설정 생성
4. NeuralLink `pre_llm_call` 플러그인 설치
5. 루트를 zero-MCP 제어면으로 전환
6. 격리 워커 프로필, 8개 Role Shell, Binding 설치
7. OpenCode 무료 모델 라우터와 worker adapter 등록
8. 무료 모델 catalog + tool-call health gate가 통과할 때만 OpenCode를 기본 경로로 승격
9. 세 층 Heartbeat 설치

실시간 무료 모델 검증을 하지 않는 오프라인 준비:

```bash
python3 scripts/setup_public_edition.py --skip-live-health
```

이 경우 OpenCode adapter/controller는 등록되지만 비활성이다.

### 칸반 웹 UI

```bash
hermes dashboard
```

브라우저에서 `http://127.0.0.1:9119/kanban`을 연다. 기본 바인딩은
localhost로 제한된다. 원격 호스트의 대시보드는 다음처럼 포트를
포워딩한다.

```bash
ssh -L 9119:127.0.0.1:9119 user@remote-host
```

## 3. macOS 운영 메모

HERMES-TEAM은 macOS의 기본 Bash 3.2와 launchd 경로를 지원한다. 별도 GNU
`timeout` 명령은 필요하지 않으며 임시 파일은 macOS의
`/private/var/folders` 실경로를 허용하되 `/private/var/db` 같은 시스템
영역은 계속 차단한다. s6 서비스 관리기는 Linux 컨테이너용이고 macOS
게이트웨이는 기존 Hermes launchd 관리 경로를 사용한다.

Apple Silicon과 Intel 모두 Python 환경의 실제 아키텍처를 그대로 사용한다.
Rosetta와 네이티브 바이너리를 한 가상환경에 섞지 말고, OpenCode/Codex 같은
외부 CLI는 터미널에서 먼저 실행되는지 확인한 뒤 adapter health gate를
통과시킨다.

```bash
uname -m
python3 -c 'import platform; print(platform.machine())'
command -v opencode || true
command -v codex || true
hermes doctor
```

## 4. 검증

```bash
hermes supervisor adapter list --json
hermes supervisor shell list --active --json
hermes supervisor executor list --json
hermes supervisor binding list --json
hermes supervisor heartbeat --json
```

소스 전체 public-core 검증:

```bash
scripts/run_tests.sh -j 8 \
  --exclude-manifest distribution/validation/test_exclusions.json
```

제외 목록은 fail-closed다. glob이나 node ID가 업스트림 변경으로 사라지면
검증이 시작되기 전에 실패한다. 정확한 제외 범위는
`distribution/validation/README.md`에 있으며 NeuralLink와 context compressor는
제외하지 않는다.

Heartbeat JSON의 정식 최상위는 다음 세 개다.

```text
layers.configuration
layers.service_schedule
layers.artifacts
```

`lanes`는 기존 UI/클라이언트 호환을 위한 상세 뷰다.

## 5. 선택형 adapter

Codex CLI가 이미 로그인되어 있다면:

```bash
python3 scripts/register_external_adapter.py \
  distribution/adapters/codex-cli.json
```

Grok은 `controller_grok`으로 미리 등록된다. `XAI_API_KEY`가 있고 catalog와
tool-call probe가 통과할 때만 활성화한다. OpenRouter와 로컬 vLLM도 같은
방식의 비활성 후보이며 기본값이 아니다.

임시 전환 예:

```bash
hermes supervisor adapter switch code executor_codex_cli \
  --temporary-seconds 1800 \
  --reason '복잡한 코드 검토'
```

한 번만 전환:

```bash
hermes supervisor adapter switch TASK_ID executor_codex_cli \
  --once --reason '이 카드만 재검증'
```

영구 소유권 변경은 충분히 검증한 뒤에만 한다.

```bash
hermes supervisor adapter assign code executor_codex_cli \
  --primary --priority 120 --note 'operator-approved'
```

## 6. 선택형 마켓 기억

개인 노하우는 배포판에 들어 있지 않다. 원하면 빈 DB를 직접 만든다.

```bash
python3 scripts/market_memory.py \
  --db ~/.hermes/knowledge/market_memory.jsonl init
```

추가·검색 방법은 `distribution/market/README.md`를 따른다. 이 기억은 조사
순서를 돕지만 최신 가격이나 공시를 대체하지 않으며 Role Shell 권한을
넓히지 않는다.
