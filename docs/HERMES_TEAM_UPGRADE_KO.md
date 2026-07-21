# HERMES-TEAM 업스트림 버전업 계약

HERMES-TEAM은 Nous Research의 공개 `hermes-agent` Git 계보 위에 유지한다.
Python 배포명, `hermes` 명령, 패키지 import, `~/.hermes` 경로는 바꾸지 않는다.
`HERMES-TEAM`은 제품/프로젝트 이름이며 업스트림 런타임 식별자를 대체하지
않는다.

## 원칙

1. 업스트림 변경을 파일 복사로 덮어쓰지 않고 Git 3-way merge로 가져온다.
2. 충돌은 자동 선택하지 않고 정확한 경로를 보고한 뒤 해결한다.
3. 제외 목록이 더 이상 실제 테스트와 일치하지 않으면 검증 자체가 실패한다.
4. merge 후에는 새로 추가된 테스트까지 자동 발견하는 전체 검증을 실행한다.
5. 전체 검증 전에는 버전업 완료로 표시하지 않는다.

## 버전업 절차

```bash
git fetch upstream main
python3 scripts/check_hermes_team_upgrade.py \
  --upstream-ref upstream/main \
  --json-out artifacts/upgrade-preflight.json
```

`status=clean`이면 작업 브랜치에서 merge한다.

```bash
git switch -c upgrade/hermes-main-YYYYMMDD
git merge --no-ff upstream/main
scripts/run_tests.sh -j 8 \
  --exclude-manifest distribution/validation/test_exclusions.json
```

`status=conflicts`이면 보고서의 `conflict_paths`를 모두 해결한 뒤 같은 전체
검증을 실행한다. 검사기는 저장소나 브랜치를 수정하지 않는다. 예약된 GitHub
Actions 검사는 최신 업스트림을 가져와 이 preflight를 반복한다.

## 테스트 제외 경계

정식 제외는 `distribution/validation/test_exclusions.json` 한 파일뿐이다.
현재 범위는 선택형 ACP 패키지의 테스트 파일과 확인된 업스트림 Anthropic
baseline 3개 node ID다. 컨텍스트 압축기, Timeline, Code Map, NeuralLink,
Heartbeat, adapter, Role Shell 테스트는 제외하지 않는다.
