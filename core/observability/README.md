# core/observability

팀 실시간 대시보드 선택 조각(M5-8). 팀 전체의 에이전트 세션·비용·도구 허용/거부·hooks 개입을 한 화면에서 본다.
기본으로 꺼져 있고 대상 저장소에 복사하지 않는다. 켜는 팀이 수집기를 띄우고 운영한다(호스팅·백업·알림 당번·SSO는 키트 범위 밖).
혼자 로컬에서 보려면 수집기 없이 [`harness dashboard`](../dashboard/README.md)를 쓴다.

```text
Claude Code ──OTLP gRPC :4317──┐                   ┌─ Prometheus(지표, 30일) ─┐
                               ├─ OTel Collector ──┤                          ├─ Grafana :3000 "harness AgentOps"
harness telemetry push ─HTTP :4318┘                └─ Loki(이벤트·로그, 30일) ┘
```

## 띄우기

```bash
cd core/observability
GRAFANA_ADMIN_PASSWORD='<비밀번호>' docker compose up -d     # http://127.0.0.1:3000 (admin / 위 비밀번호)
```

- `GRAFANA_ADMIN_PASSWORD`는 필수다. 기본값이 없고 저장소·문서·이슈에 쓰지 않는다([Secret 규칙](../../docs/secret-environment-variables.md)).
- 포트는 기본으로 `127.0.0.1`에만 연다. 팀이 같이 보려면 Grafana는 `HARNESS_OBS_BIND`(사내망 주소 등)로 연다(로그인 필요).
  수집기(4317·4318)는 인증하지 않으므로 `HARNESS_OBS_COLLECTOR_BIND`는 `127.0.0.1`로 두고, 같은 호스트의 인증 프록시(TLS)만 밖에
  연다. 수집기 포트를 직접 열면 프록시를 거치지 않고 보낼 수 있다. Docker가 연 포트는 ufw·firewalld 규칙을 거치지 않는다
  (막으려면 `DOCKER-USER` 체인).
- 이미지는 버전과 digest로 고정했다(`docker-compose.yml` 머리 주석). 올릴 때 digest를 함께 바꾼다. CI의 `observability` job이 스택을
  띄워 스모크 검사를 하고 이미지 4개를 trivy로 보고한다.
- 끄기: `docker compose down`(데이터까지 지우려면 `-v`). 텔레메트리를 꺼도 키트의 다른 기능(hooks·`sessions summary`·`dashboard`·주간
  지표)은 그대로 동작한다. 수집기가 꺼져 있으면 Claude Code는 내보내기만 실패하고 세션은 영향받지 않는다.

## 보내기

### Claude Code 텔레메트리

각자의 셸이나 `~/.claude/settings.json`의 `env`, 또는 조직 managed settings의 `env`에 둔다([설치 가이드 14장](../../docs/install.md)).

```json
{
  "env": {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_METRICS_EXPORTER": "otlp",
    "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4317",
    "OTEL_METRIC_EXPORT_INTERVAL": "10000",
    "OTEL_RESOURCE_ATTRIBUTES": "team.id=my-team"
  }
}
```

- 지표는 기본 60초마다 나간다. "세션이 1분 안에 보인다"를 원하면 `OTEL_METRIC_EXPORT_INTERVAL`을 10초로 줄인다(이벤트는 5초).
- Claude Code 지표는 기본 delta다. 수집기가 `deltatocumulative`로 누적으로 바꿔 Prometheus에 넣는다.
- 내용 기록 변수(`OTEL_LOG_USER_PROMPTS`·`OTEL_LOG_ASSISTANT_RESPONSES`·`OTEL_LOG_TOOL_DETAILS`·`OTEL_LOG_RAW_API_BODIES`)는 켜지 않는다.

### 키트 기록 (`harness telemetry push`)

hooks가 남긴 `sessions.jsonl`·`events.jsonl`(모든 worktree)에서 지난번 이후 새 줄만 OTLP/HTTP(JSON) 로그로 보낸다. 기본으로 아무것도
보내지 않으며, 팀이 정한 주기로 실행한다(cron·작업 스케줄러, 또는 세션을 마친 뒤 손으로).

```bash
python bin/harness.py telemetry push ../my-project --endpoint http://127.0.0.1:4318 --dry-run   # 보낼 내용만 본다
python bin/harness.py telemetry push ../my-project --endpoint http://127.0.0.1:4318
OTEL_EXPORTER_OTLP_ENDPOINT=https://collector.example:4318 python bin/harness.py telemetry push ../my-project
```

| 항목 | 내용 |
| --- | --- |
| 엔드포인트 | `--endpoint`(기본 URL) → `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT`(그대로) → `OTEL_EXPORTER_OTLP_ENDPOINT`. 기본 URL에는 `/v1/logs`를 붙인다. OTLP/HTTP 포트(4318)다 |
| 인증 | `OTEL_EXPORTER_OTLP_LOGS_HEADERS` 또는 `OTEL_EXPORTER_OTLP_HEADERS`(`k=v,k2=v2`, 값은 퍼센트 인코딩). Secret이므로 셸·CI 변수로만 준다. 값은 출력하지 않고, 원격 `http`로 보내면 경고한다 |
| 리소스 속성 | `service.name=harness`, `service.version`(키트), `harness.repo`(저장소 이름), `OTEL_RESOURCE_ATTRIBUTES`(Claude Code와 같은 값을 두면 팀·사람으로 함께 묶인다) |
| 보내는 필드 | 세션: 사건(`start`·`stop`·`end`)·세션 ID·브랜치·도구·모델·시작 종류·종료 검증 결과·바뀐 파일 수·재시도·토큰 수·추정 비용(세션 누적). hooks: hook·결정·규칙·경로·도구 이름·세션 ID·브랜치 |
| 보내지 않는 것 | transcript 경로, 종료 검증 명령, HEAD 커밋, 프롬프트·코드 내용 |
| 위치 | 공용 git 디렉터리의 `harness/telemetry.json`. 파일마다 앞에서부터 끊김 없이 보낸 줄까지만 전진하므로 실패하면(종료 1) 다음 실행이 나머지를 보낸다. 쓰는 중인 마지막 줄은 다음에 보낸다. 다른 push가 실행 중이면 종료 1로 알린다 |
| 부분 거절 | 수집기가 일부 줄을 거절하면(OTLP `partialSuccess`) 거절 수와 사유를 알리고 종료 3. 거절된 줄은 다시 받지 않으므로 다시 보내지 않는다 |
| 스트림 | 세션마다 리소스 속성 `session.id`를 두고, Loki는 이것을 색인 레이블로 둔다(세션마다 스트림). Loki는 한 스트림의 가장 새 줄보다 1시간 넘게 오래된 줄을 버리므로, 팀원 여러 명의 기록이나 지난 기록을 늦게 보내도 버려지지 않게 한다. 수집기는 Loki에 넣기 전에 200으로 답하므로 Loki가 거절한 줄은 push가 알 수 없다(수집기 로그에 남는다) |
| 다시 보내기 | `--from-start`. Loki는 보관 기간(30일)보다 오래된 줄을 받지 않는다 |

## 대시보드 "harness AgentOps"

Grafana `harness` 폴더에 프로비저닝된다(화면에서 고칠 수 없다. 바꾸려면 `grafana/dashboards/harness-agentops.json`을 고친다).
기간 합계는 오른쪽 위 시간 범위를 따른다.

| 패널 | 원천 | 쿼리 요지 |
| --- | --- | --- |
| Claude Code 세션 | Prometheus | `claude_code.session.count` 증가분 |
| 비용(USD, Claude Code) | Prometheus | `claude_code.cost.usage` 증가분. Claude Code가 계산한 값이며 청구액이 아니다 |
| 도구 거부 비율 | Loki | `tool_decision` 이벤트 중 `decision=reject` |
| hook 차단(Claude Code) | Loki | `hook_execution_complete`의 `num_blocking` 합 |
| 키트 hooks 개입 | Loki | `telemetry push`로 받은 block·ask·fail |
| hooks 기록 없는 세션(근사) | 둘 다 | Claude Code 세션 수 − 키트 세션 기록이 있는 세션 수. 양수면 hooks가 꺼졌거나 없는 세션이 있다 |
| 시간당 비용(사람별) | Prometheus | `user_email`별. OAuth 로그인이 아니면 비어 있으니 `user_account_uuid`나 `OTEL_RESOURCE_ATTRIBUTES`의 키로 바꾼다 |
| 시간당 토큰(종류별) | Prometheus | `claude_code.token.usage`의 `type` |
| 도구 허용·거부 | Loki | `tool_decision`의 `decision`·`source`(config·hook·user_*) |
| 키트 hooks 개입(결정별)·응답 수(브랜치별)·최근 개입 | Loki | `service_name="harness"` |
| 사람·도구별 세션 | Prometheus | `user_email`·`service_name`(claude-code, claude-code-desktop) |

Prometheus 이름은 수집기가 바꾼 이름(`claude_code_cost_usage_USD_total` 등)이라 쿼리는 `{__name__=~"claude_code_cost_usage.*"}`로 쓴다.
Claude Code는 세션마다 새 시계열(`session_id`)을 만들어 `increase()`가 첫 내보내기를 세지 않으므로, 증가분은 `increase()`(수집기
재시작 등으로 누적이 0으로 돌아가도 이어 센다)에 기간 안에 처음 생긴 시계열의 첫 값을 더하고, 세션 수는 기간 안 `session_id` 수로
센다. `increase()`는 기간 끝으로 외삽하므로 짧은 기간에서는 조금 다를 수 있다. 수집기는 조용한 시계열의 누적을 24시간 유지한다.
Loki에서 속성은 점이 밑줄로 바뀐 구조화 메타데이터다(`event.name` → `event_name`, `harness.kind` → `harness_kind`).

### 주간 지표와의 대응

| 주간 지표(`harness metrics`, 플랫폼 API) | 대시보드 | 차이 |
| --- | --- | --- |
| 6. MR당 에이전트 비용·토큰(MR 본문 `## 에이전트 기록`) | 비용·토큰 패널, 키트 응답 수(브랜치별) | 주간은 병합된 MR 단위의 추정(키트 단가), 대시보드는 시간 단위의 Claude Code 값. 같은 기간이라도 단가 기준이 달라 다를 수 있다 |
| 7. 도구별 MR 수와 결과 | 사람·도구별 세션 | 대시보드는 Claude Code 세션만 센다(다른 도구는 텔레메트리가 없다). 결과(CI 실패·누락)는 주간 지표만 있다 |
| 8. 리뷰 발견 채택률 | 없음 | 리뷰 파일·MR 본문에서만 센다 |
| hooks 기록(MR 본문 `## hooks 기록`) | 키트 hooks 개입, hook 차단(Claude Code) | 같은 사건. 대시보드는 병합 전 작업 중에도 보인다 |
| 기록 없는 커밋(`sessions summary`) | hooks 기록 없는 세션(근사) | 커밋 기준 대리 신호 대 세션 기준 신호 |
| 1~5(병합 후 누락·엄격 비율·CI 실패·판정 불일치 등) | 없음 | 병합 단위 결과는 주간 지표만 본다 |

## 확인한 것과 한계

- CI(`observability` job)가 실제 이미지로 스택을 띄우고, `telemetry push`와 Claude Code 형식의 지표·이벤트(문서의 이름·속성으로
  만든 것)를 보내 대시보드의 모든 쿼리를 Grafana로 실행한다. 실제 Claude Code 세션의 텔레메트리로는 CI에서 확인하지 않는다.
- 이 조각은 수집기 운영(가용성·백업·용량)을 다루지 않는다. 보관은 Prometheus·Loki 모두 30일이다.
- Claude Code 지표·이벤트 이름은 버전마다 늘어난다. 기준 문서는 [Claude Code 모니터링](https://code.claude.com/docs/en/monitoring-usage)이다.
