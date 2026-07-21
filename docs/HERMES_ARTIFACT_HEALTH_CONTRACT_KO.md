# HERMES-TEAM 산출물 상태 계약

이 문서는 HERMES-TEAM heartbeat의 3층인 `artifacts`를 구성하는 방법을 설명한다.
배포본에는 특정 사용자의 보고서, 거래 서비스, 데이터베이스 또는 파일 경로가
등록되어 있지 않다. 산출물 점검이 비어 있으면 `not_configured`이며 오류가 아니다.

## 설정 원칙

`supervisor.artifact_health`에는 명시적으로 허용된 파일 또는 디렉터리 점검만
등록한다. 임의 셸 명령은 실행하지 않는다.

```yaml
supervisor:
  artifact_health:
    enabled: true
    checks:
      - name: daily-public-report
        path: "${HOME}/reports/daily.md"
        kind: file
        required: true
        min_bytes: 100
        max_age_seconds: 86400
```

지원 필드:

- `name`: heartbeat와 영수증에 표시할 안정적인 식별자
- `path`: 점검할 명시적 경로
- `kind`: `file`, `directory`, `any`
- `required`: 누락 시 전체 산출물 계층을 실패시킬지 여부
- `min_bytes`: 파일 최소 크기
- `max_age_seconds`: 허용 가능한 최대 노후 시간

## 완료 판정

각 점검은 존재 여부, 형식, 크기, 신선도와 SHA-256 근거를 반환한다. 모든 필수
점검이 통과하고 검사기 자체 오류가 없을 때만 산출물 계층이 정상이다. 도메인별
의미 검증이 필요하면 별도 verifier 어댑터가 검사 결과를 만든 뒤 이 계층에는 그
결과 파일만 등록한다.

## 보안 경계

- HERMES-TEAM은 개인 경로를 추측하거나 자동 탐색하지 않는다.
- 설정되지 않은 서비스나 산출물을 실패로 만들지 않는다.
- 산출물 점검은 읽기 전용이며 파일을 생성·수정·삭제하지 않는다.
- 경로와 해시 외 민감한 파일 본문은 heartbeat에 싣지 않는다.

## 회귀 점검

- 비활성/빈 설정이 `healthy=true`, `status=not_configured`인지
- 필수 파일 누락이 실패하고 선택 파일 누락은 전체 실패를 만들지 않는지
- 크기와 신선도 경계가 설정값대로 판정되는지
- heartbeat가 구성 상태, 서비스·스케줄, 산출물의 정확히 세 계층만 표시하는지
