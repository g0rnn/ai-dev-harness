# core/observability

팀 실시간 대시보드 선택 조각(M5-8). 팀 전체의 에이전트 세션·비용·도구 허용/거부·hooks 개입을 한 화면에서 본다.
기본으로 꺼져 있고 대상 저장소에 복사하지 않는다. 켜는 팀이 수집기를 띄우고 운영한다(호스팅·백업·알림 당번·SSO는 키트 범위 밖).
혼자 로컬에서 보려면 수집기 없이 [`harness dashboard`](../dashboard/README.md)를 쓴다.

세 화면의 역할과 목데이터 스크린샷은 [루트 README의 팀 대시보드](../../README.md#팀-대시보드)에서 볼 수 있다.
팀 사용량은 공통 수집 v2(#140) 적용 화면이며, 작업 검증 기록은 프로젝트 확장으로 별도 적용하는 화면이다.
사진에 보이는 가상 팀원의 수치와 수집 상태는 기능 설명을 위한 예시다.

```text
Claude Code ──OTLP gRPC :4317──┐                   ┌─ Prometheus(지표, 30일) ─┐
                               ├─ OTel Collector ──┤                          ├─ Grafana :3000 "harness 수집·차단 진단"
telemetry push ──OTLP HTTP :4318┘                  └─ Loki(이벤트·로그, 30일) ┘
(.harness/telemetry.py 또는 키트 harness telemetry push: hooks 기록 + Codex 세션)
```

팀원이 여럿이면 수집기 앞에 인증 프록시(TLS·팀원별 토큰)를 두고 사람 구분(`member`)을 프록시가 정한다(아래 "팀 서버").

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

### 팀원 PC 설정 (`telemetry setup`·`doctor`, #145)

Claude Code와 Codex는 각자 내장 텔레메트리로 사용량을 직접 보낸다. 두 도구 모두 **프로젝트 설정으로는 켤 수 없다**(Claude Code는
프로젝트·로컬 설정의 OTEL 변수를 무시하고, Codex는 프로젝트 `.codex/config.toml`의 `otel`을 거부한다). 그래서 팀원마다 PC에서
한 번 사용자 전역 설정을 맞춘다. 수집기 주소는 저장소 `harness.json`의 `telemetry` 블록(팀 공통, 커밋), 토큰은 사용자 자격 파일
(`%APPDATA%\harness\credentials.json`, 그 밖은 `~/.config/harness/credentials.json`)에 둔다.

```bash
python .harness/telemetry.py setup     # 토큰 입력 → 바꿀 내용 확인 → 적용. 처음 한 번은 --endpoint <수집기 주소>로 블록을 만든다
python .harness/telemetry.py doctor    # 설정을 점검하고 수집기에 빈 요청을 보내 토큰이 받아들여지는지 본다(--offline이면 생략)
```

| 대상 | setup이 하는 일 |
| --- | --- |
| Claude Code `~/.claude/settings.json` | `env`의 내장 텔레메트리 변수(OTLP/HTTP)와 `otelHeadersHelper`. 토큰은 설정 파일에 쓰지 않고 헤더 스크립트(`otel_headers.py`, 사용자 설정 디렉터리)가 자격 파일에서 읽어 준다. 평문 토큰이 든 `OTEL_EXPORTER_OTLP_*HEADERS`는 지운다 |
| Codex `config.toml` | 표시 주석으로 감싼 `[otel]` 블록(로그·지표). Codex는 헤더에 환경 변수 치환을 하지 않아 토큰을 이 파일에 쓴다(POSIX 600). Orca 터미널은 창마다 `CODEX_HOME`을 계정 홈으로 바꾸므로 `CODEX_HOME`·`~/.codex`·Orca 계정 홈을 모두 맞춘다 |
| 안전장치 | 바꿀 내용을 먼저 보이고 승인 뒤에만 쓴다. 사용자 설정은 바꾸기 전 내용을 `.harness-backup`에 남긴다. 사용자가 직접 둔 다른 수집기 설정이 있으면 멈춘다 |

터미널에서는 설치 도구처럼 단계·색·가린 입력·화살표 선택으로 보이고, 파이프·CI·`NO_COLOR`에서는 글자만 쓴다(`--yes`·`--token-stdin`).

### 키트 기록과 Codex 세션 (`telemetry push`)

hooks가 남긴 `sessions.jsonl`·`events.jsonl`(모든 worktree)에서 지난번 이후 새 줄만, 그리고 `CODEX_HOME` 세션 로그 중 이 저장소
worktree에서 연 Codex 세션의 새 사건을 OTLP/HTTP(JSON) 로그로 보낸다. 기본으로 아무것도 보내지 않으며, 팀이 정한 주기로 실행한다
(cron·작업 스케줄러, 또는 세션을 마친 뒤 손으로). 수집기 주소가 정해진 저장소에서는 Claude Code의 SessionEnd hook이
종료 기록 뒤 `.harness/telemetry.py push`를 자동으로 백그라운드 실행한다. 별도 스케줄러 없이 이 저장소의 Codex 기록도 같이 보낸다.

- 활성 조건: `harness.json`에 `telemetry` 블록이 있거나 hook 프로세스 환경에 OTLP 엔드포인트 변수가 있고, 저장소에
  `.harness/telemetry.py`가 설치돼 있어야 한다. **Claude Code는 설정 파일 `env`의 `OTEL_*` 변수를 hook에 넘기지 않으므로**(#145 실측)
  Claude Code 안에서는 `telemetry` 블록이 사실상 유일한 활성 경로다. `telemetry.harness_events: false`면 자동 전송하지 않는다.
- 엔드포인트는 **OTLP/HTTP**여야 한다. 위 Claude Code 예시의 gRPC `:4317` 설정만으로는 push가 동작하지 않는다.
  두 도구가 같은 변수를 쓰려면 `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`, `OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318`
  (팀 서버는 HTTPS 주소)로 Claude Code도 HTTP로 보낸다. 신호별 endpoint 변수가 있으면 그것이 우선하므로 함께 확인한다.
- 전송 프로세스는 표준 입출력을 닫고 분리하며, Windows에서는 창을 띄우지 않는다. 세션 종료는 전송 결과를 기다리지 않고 항상 0이다.
  실패한 전송은 기존 전송 위치를 유지하고 **다음 Claude Code 세션 종료 때** 다시 시도한다. 실패를 즉시 확인하려면 수동 push를 실행한다.
- Codex만 사용하는 PC와 오래 열린 Claude Code 세션에는 이 트리거가 없다. 수동 push 또는 작업 스케줄러로 보낸다.
  Codex는 이 PC의 `CODEX_HOME`에서 이 저장소 worktree에 해당하는 기록만 수집한다. 다른 PC의 기록은 보내지 않는다.
- 자동 전송을 끄려면 `telemetry.harness_events`를 `false`로 두고 hook에 전달되는 endpoint 환경변수도 비운다. Claude Code
  텔레메트리 활성 변수만 지우면 키트 전송은 꺼지지 않는다. `.harness/telemetry.py`가 없는 갱신 전 저장소에서는 자동 전송을 건너뛴다.

`init`이 대상 저장소에 `.harness/telemetry.py`를 넣으므로 키트 없이 저장소(어느 worktree든) 안에서 실행한다(#127). 키트의
`harness telemetry push`와 같은 코드(`core/metrics/telemetry.py`)라 같은 내용을 보내고 보낸 위치도 함께 쓴다.

```bash
python .harness/telemetry.py push --endpoint http://127.0.0.1:4318 --dry-run   # 대상 저장소 안에서. 보낼 내용만 본다
python .harness/telemetry.py push --endpoint http://127.0.0.1:4318
OTEL_EXPORTER_OTLP_ENDPOINT=https://collector.example python .harness/telemetry.py push   # 팀 서버(인증 프록시)
python bin/harness.py telemetry push ../my-project --endpoint http://127.0.0.1:4318         # 키트에서(같은 내용)
```

| 항목 | 내용 |
| --- | --- |
| 엔드포인트 | `--endpoint`(기본 URL) → `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT`(그대로) → `OTEL_EXPORTER_OTLP_ENDPOINT` → `harness.json` `telemetry.endpoint`. 기본 URL에는 `/v1/logs`를 붙인다. OTLP/HTTP 포트(4318)다 |
| 인증 | `OTEL_EXPORTER_OTLP_LOGS_HEADERS` 또는 `OTEL_EXPORTER_OTLP_HEADERS`(`k=v,k2=v2`, 값은 퍼센트 인코딩), 없으면 사용자 자격 파일에서 주소의 헤더(`telemetry setup`이 넣는다). Secret이므로 저장소에 두지 않는다. 값은 출력하지 않고, 원격 `http`로 보내면 경고한다 |
| 리소스 속성 | `service.name=harness`, `service.version`(키트), `harness.repo`(저장소 이름), `OTEL_RESOURCE_ATTRIBUTES`(Claude Code와 같은 값을 두면 팀·사람으로 함께 묶인다) |
| 보내는 필드 | 세션: 사건(`start`·`stop`·`end`)·세션 ID·브랜치·도구·모델·시작 종류·종료 검증 결과·바뀐 파일 수·재시도·토큰 수·추정 비용(세션 누적). hooks: hook·결정·규칙·경로·도구 이름·세션 ID·브랜치 |
| Codex 세션 | MR 기록 요약(`python .harness/summary.py agent`)과 같은 규칙으로 고른다: 세션 작업 폴더가 이 저장소 worktree 안, 사건마다 그 시각에 체크아웃한 브랜치(HEAD reflog). 세션 기록과 같은 형식(`harness.tool=codex`, 사건 `start`·`turn`·`stop`·`usage`, 모델별 누적 토큰·추정 비용)이고, 승인 검토·서브에이전트 세션은 `harness.parent`(부모 세션 ID)를 붙인 `usage` 사건이다. 위치는 `CODEX_HOME`(없으면 `~/.codex`) |
| 보내지 않는 것 | transcript 경로, 종료 검증 명령, HEAD 커밋, 프롬프트·코드 내용. Codex는 대화 내용·원격 URL·커밋을 읽지도 보내지도 않는다 |
| 위치 | 공용 git 디렉터리의 `harness/telemetry.json`. 파일마다 앞에서부터 끊김 없이 보낸 줄까지만 전진하므로 실패하면(종료 1) 다음 실행이 나머지를 보낸다. 쓰는 중인 마지막 줄은 다음에 보낸다. 다른 push가 실행 중이면 종료 1로 알린다 |
| Codex 위치 | Codex 로그는 Codex가 쓰는 파일이고 사건을 파일 전체에서 다시 만들므로(진행 중인 턴의 `usage` 사건은 턴이 끝나면 `stop`으로 바뀐다) 바이트 위치가 아니라 세션 ID마다 워터마크 `[보낸 마지막 사건 시각(ns), 그 시각에 보낸 사건 이름]`을 같은 파일의 `codex`에 둔다. 워터마크보다 뒤의 사건, 같은 시각이면 아직 보내지 않은 이름의 사건만 보낸다. 줄과 같이 세션 안에서 앞에서부터 끊김 없이 보낸 사건까지만 전진한다. 진행 중에 보낸 `usage`와 끝난 뒤의 `stop`은 둘 다 남지만 토큰은 세션 누적이라 세션별 최댓값을 쓴다. 워터마크보다 앞 시각에 새로 생긴 사건은 보내지 않는다(Codex는 시각 순으로 쓴다) |
| 부분 거절 | 수집기가 일부 줄을 거절하면(OTLP `partialSuccess`) 거절 수와 사유를 알리고 종료 3. 거절된 줄은 다시 받지 않으므로 다시 보내지 않는다 |
| 스트림 | 세션마다 리소스 속성 `session.id`를 두고, Loki는 이것을 색인 레이블로 둔다(세션마다 스트림). Loki는 한 스트림의 가장 새 줄보다 1시간 넘게 오래된 줄을 버리므로, 팀원 여러 명의 기록이나 지난 기록을 늦게 보내도 버려지지 않게 한다. 수집기는 Loki에 넣기 전에 200으로 답하므로 Loki가 거절한 줄은 push가 알 수 없다(수집기 로그에 남는다). 색인 레이블로 두지 않는 선택지는 아래 "Loki 레이블 정책" |
| 사람 | 클라이언트가 정하지 않는다. 팀 서버의 인증 프록시가 토큰으로 `X-Harness-Member` 헤더를 넣고 수집기가 리소스 속성 `member`로 옮긴다(아래 "팀 서버"). push 쪽은 `OTEL_EXPORTER_OTLP_HEADERS`에 자기 토큰만 둔다 |
| 다시 보내기 | `--from-start`(Codex 워터마크도 함께 지운다). Loki는 보관 기간(30일)보다 오래된 줄을 받지 않는다 |

## 팀 서버: 인증 프록시와 사람 구분 (#127)

대시보드는 기본으로 Claude 계정(`user_email`)으로 사람을 나눈다. Claude Team 계정 하나를 여러 명이 같이 쓰면 계정으로는 나눌 수
없으므로, 수집기 앞 인증 프록시가 팀원별 토큰을 확인하고 그 팀원의 이름을 헤더 `X-Harness-Member`로 넣는다. 수집기는 이 헤더를
리소스 속성 `member`로 옮긴다. 지표는 `member` 레이블(`resource_to_telemetry_conversion`), Loki 로그는 `member` 구조화 메타데이터가
된다. 대시보드 위쪽 "사람 구분" 변수를 `member`로 바꾸면 사람별 패널이 이 값으로 묶인다.

- 위조 방지: 이름은 클라이언트가 아니라 프록시가 토큰으로 정한다. 프록시는 받은 `X-Harness-Member`를 버리고(덮어쓰고) 토큰이 맞지
  않으면 401로 막는다. 수집기(4317·4318)는 `127.0.0.1`에만 열어 프록시를 거치지 않는 길을 없앤다. 수집기는 헤더가 있을 때만
  `member`를 덮으므로(upsert), 프록시 없이 직접 받으면 클라이언트가 `OTEL_RESOURCE_ATTRIBUTES`로 보낸 `member`가 그대로 남는다
- 클라이언트: Claude Code와 `telemetry push` 모두 같은 변수로 자기 토큰만 준다. 토큰은 Secret이라 셸·개인 설정·CI 변수에만 둔다
  (`OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer%20<토큰>`, 값은 퍼센트 인코딩). 저장소·문서·이슈에 쓰지 않는다
- 키트 수집기 설정(`otel-collector.yaml`)에는 아래 수집기 조각이 이미 들어 있다. 헤더가 없으면 아무것도 하지 않으므로 혼자 쓸 때도
  그대로 둔다. 기존 수집기에 얹을 때는 아래 조각만 옮긴다

수집기 조각(OpenTelemetry Collector contrib 0.162 기준). `resource/member`는 반드시 `batch` 앞에 둔다. `batch`는 요청
메타데이터(헤더)를 버리므로 뒤에 두면 `member`가 비어 있다(`batch` 뒤에 둬야 하면 `batch`에 `metadata_keys: [x-harness-member]`).

```yaml
receivers:
  otlp:
    protocols:
      grpc:
        endpoint: 0.0.0.0:4317       # 호스트에는 127.0.0.1로만 연다(프록시만 접근)
        include_metadata: true       # 요청 헤더(gRPC 메타데이터)를 프로세서가 읽게 한다
      http:
        endpoint: 0.0.0.0:4318
        include_metadata: true

processors:
  resource/member:
    attributes:
      - key: member
        from_context: metadata.x-harness-member   # 헤더 이름은 대소문자를 가리지 않는다
        action: upsert

service:
  pipelines:
    metrics:
      receivers: [otlp]
      processors: [memory_limiter, resource/member, deltatocumulative, batch]
      exporters: [prometheus]          # resource_to_telemetry_conversion.enabled: true 여야 member가 레이블이 된다
    logs:
      receivers: [otlp]
      processors: [memory_limiter, resource/member, batch]
      exporters: [otlphttp/loki]
```

프록시 조각(Caddy 2 예시). OTLP/HTTP(`/v1/*`, `telemetry push`·HTTP로 보내는 Claude Code)는 4318로, 나머지 gRPC(Claude Code
`OTEL_EXPORTER_OTLP_PROTOCOL=grpc`)는 h2c로 4317에 넘긴다. 클라이언트 엔드포인트는 둘 다 `https://collector.example.com`(포트 443)이다.
토큰은 Caddy를 띄우는 환경 변수로 넣고 이 파일에 쓰지 않는다. 키트 CI는 이 조각을 실행하지 않는다. `caddy validate`는 문법만 보므로
적용한 뒤 맞는 토큰·틀린 토큰으로 한 번씩 보내(예: `telemetry push`, 401 여부) 수집기에 `member`가 붙는지 확인한다.

```caddyfile
collector.example.com {
	# 팀원별 토큰 → 이름. 맞는 토큰이 없으면 "-"
	map {http.request.header.Authorization} {harness_member} {
		"Bearer {$HARNESS_TOKEN_KIM}"  kim
		"Bearer {$HARNESS_TOKEN_LEE}"  lee
		"Bearer {$HARNESS_TOKEN_PARK}" park
		default                        "-"
	}
	@unknown vars {harness_member} "-"
	@otlphttp path /v1/*

	# route 안은 적은 순서대로 실행한다. map은 값을 읽을 때 Authorization을 다시 보므로(지연 평가) 토큰 확인과
	# member 헤더 설정을 Authorization 삭제보다 먼저 둔다. route 없이 두면 Caddy 기본 순서(request_header가
	# respond보다 먼저)로 Authorization이 먼저 지워져 맞는 토큰도 모두 401이 된다(#127 리뷰 F1)
	route {
		respond @unknown 401
		# 클라이언트가 보낸 X-Harness-Member는 덮어쓴다(위조 방지). 토큰은 수집기로 넘기지 않는다
		request_header X-Harness-Member {harness_member}
		request_header -Authorization
		reverse_proxy @otlphttp 127.0.0.1:4318
		reverse_proxy h2c://127.0.0.1:4317
	}
}
```

- 토큰 값에 빈 칸·따옴표를 쓰지 않는다(무작위 16진 문자열 등). 토큰을 바꾸면 Caddy를 다시 읽힌다(`caddy reload`)
- 이름(`kim` 등)은 대시보드에 그대로 보이는 값이다. 사람을 바꿔 쓰면 이력이 섞이므로 토큰과 이름을 같이 바꾼다

### Loki 레이블 정책: `session.id`를 색인 레이블로 두지 않는 선택지

키트 `loki.yaml`은 `telemetry push` 기록의 리소스 속성 `session.id`를 색인 레이블로 둔다(세션마다 스트림). 팀 Loki의 레이블 정책이
세션 ID 같은 값이 많은 레이블을 막으면 `loki.yaml`의 `otlp_config.resource_attributes.attributes_config`에서 `session.id` 항목을 빼고,
대시보드는 그대로 쓴다(`session_id`는 구조화 메타데이터로 남아 `sum by (session_id)`가 동작한다).

- 대가: 스트림이 `service_name`·`member` 등 색인 레이블 조합 단위로 커진다. Loki는 한 스트림의 가장 새 줄보다 1시간 넘게 오래된
  줄을 버리므로(`out of order`), 같은 스트림에 오래된 기록을 늦게 보내면 버려진다. 예: 한 팀원이 오늘 아침 기록을 보낸 뒤 어제 저녁
  기록을 처음 보내는 경우, 두 worktree 중 하나를 오래 push하지 않은 경우, `--from-start`
- 줄이는 법: push를 자주(세션을 마칠 때마다 또는 15분 주기) 실행한다. 사람마다 스트림을 가르도록 `member`를 색인 레이블로 두면
  (`attributes_config`의 `index_label`에 `member`) 다른 사람의 새 기록 때문에 버려지는 일은 없어진다. 버려진 줄은 push가 알 수 없고
  수집기 로그에만 남는다

## 대시보드 "harness 수집·차단 진단"

Grafana `harness` 폴더에 프로비저닝된다. UID는 `harness-agentops`를 유지한다.
기본 기간은 최근 24시간(KST)이다. 비용·토큰 비교는 `harness 팀 사용량`에서 한다.

| 위치 | 표시 내용 | 표현 |
| --- | --- | --- |
| 상단 | 관측 끊김, 전송·읽기 오류 이력, Claude 보호 규칙 개입, Claude 검증 실패 | 숫자 카드 4개 |
| 수집 상태 | 사람·저장소별 마지막 수신 시각과 180초 무신호 | 표 |
| 문제 사건 | 시간대별 Claude 차단·확인 요청, 최근 차단·검증 실패 | 막대와 로그 |
| 오류 이력 | 선택 기간 보고된 전송 실패·원천 읽기 오류 최대값 | 표 |
| 접힌 상세 | 관측 지연, Claude hooks, 기존 원천 진단, 수집 범위·정의 | 상세 패널 |

`member`(사람)·`repo`(저장소)는 새 진단·hooks 패널에 적용하는 정규식 필터이며 `.*`가 전체다.
기존 네이티브 비용·토큰·세션 패널은 별도 접힌 행에 보존하고, 해당 행은 이 필터를 적용하지 않는다.
`person`은 기존 원천의 묶음 기준으로 기본 `member`이며 화면에서 숨긴다.
`DS_PROMETHEUS`·`DS_LOKI`는 숨긴 데이터소스 변수다. 기존 Grafana에서는 `current.value`를 실제 UID에 맞춘다.
상단 대시보드 링크는 기간·사람·저장소 변수를 전달한다. 작업 ID는 근거 화면에서만 사용한다.

관측 끊김은 최근 7일 안에 관측된 사람·저장소의 수집기 신호 기준이다. 미설치 PC 목록이나
Claude/Codex 각각의 실행 상태를 알려 주지 않는다. 전송 실패·읽기 오류 이력은 기간 중 보고된
최대값이며 현재 미해결 건수가 아니다. 아래 표의 기존 비용·토큰 계산은 접힌 원천 진단에만 해당한다.

| 패널 | 원천 | 쿼리 요지 |
| --- | --- | --- |
| 관측된 추정 비용 · USD | 둘 다 | Claude `claude_code.cost.usage` 증가분 + Codex USD 추정 비용. 한쪽만 수신되면 부분 합계, 둘 다 미수신이면 기록 없음. 데이터소스 오류는 숨기지 않는다 |
| 완료 응답 · 두 도구 | Loki | 키트 세션의 `stop`, 부모 세션 ID가 있는 보조 세션은 제외 |
| 보호 규칙 개입 · Claude hooks | Loki | 키트 `block`·`ask`만 합산. 검증 실패는 분리 |
| 종료 검증 실패 · Claude hooks | Loki | 키트 `stop-verify`의 `fail`. Codex 동등 결과는 미수집 |
| 시간별 추정 비용 | 둘 다 | 각 시각 직전 1시간 증가분을 5분 간격으로 표시한다. 겹치는 이동 구간이므로 점들을 합산하지 않는다 |
| 사람별 추정 비용 | 둘 다 | `$person`별 선택 기간 비용을 하나의 가로 막대로 표시하고 Codex·Claude Code를 색으로 구분. 합계순 정렬. `person_name`으로 레이블을 정규화하며 식별자가 없으면 미식별로 남긴다 |
| Claude Code / Codex 토큰 · 종류별 | 각 원천 | Claude `type` / Codex `harness_tokens_*`. 일반 입력과 캐시 읽기를 구분 |
| 완료 응답 · 사람·도구별 / 브랜치별 | Loki | 키트 `stop`을 `$person`·`harness_tool` 또는 브랜치로 묶는다. 보조 세션 제외 |
| 키트 개입 추이 / 규칙별 상위 5개 | Loki | 키트 `block`·`ask`·`fail` 추이 / `block`·`ask` 규칙별 건수 |
| 종료 검증 결과 · Claude hooks | Loki | 키트 `stop`의 `verify`: pass·fail·cached-pass·cached-fail·none·error. Codex 미포함 |
| 도구 허용·거부 / 거부 비율 / 원본 hook 차단 | Loki | Claude 원본 `tool_decision`·`hook_execution_complete`. 키트 사건과 중복 합산하지 않는다 |
| 관측 세션 / 사람·도구별 세션 · Claude 원본 | Prometheus | 기간에 관측된 Claude `session_id` 수. 활성 세션이나 두 도구 합계가 아니다 |
| 키트 기록 없는 세션 · Claude 근사 | 둘 다 | Claude 원본 세션 − Codex를 제외한 키트 세션. 기간 경계·늦은 전송 때문에 음수도 가능하며 장애로 단정하지 않는다 |
| 수신 기록 수 · 원천별 | 둘 다 | Claude 비용 시계열 수·Claude/Codex 키트 사건 수. 단위가 달라 합산하거나 활동량으로 비교하지 않는다 |

`0`은 원천의 관측값이며, 해당 원천에 기록이 없으면 **기록 없음**, 도구가 해당 지표를 제공하지 않으면 **미수집**으로 안내한다.
기록 없음만으로 활동 없음·전송 누락을 구분하지 않는다. 조회 오류는 Grafana 오류로 유지한다. 비용 합계만 부분 수신을 지원하기 위해
한 원천의 미수신을 내부 계산에서 0으로 취급하며, 양쪽 미수신은 수신 존재 쿼리로 걸러 **기록 없음**으로 표시한다.
Codex 캐시 쓰기 5분 필드는 `cache_write_input_tokens`의 공통 필드 매핑으로 실제 TTL이 아니다. 1시간 필드는 현재 수집 코드가 채운 0이므로
미사용량으로 오해하지 않게 화면에서 숨긴다(원천 쿼리 회귀 검사는 유지한다).
최근 기록은 해결 상태가 아니라 발생 이력이다. 실행 중 여부는 확정하지 않는다. 새 공통 사용량·진단은 v2 기록으로 별도 제공한다.

Codex 비용·토큰은 누적 기록을 단순 합산하지 않는다. 사람·저장소·세션별로 기간 안 최댓값을 구하고, 기간 시작 전 7일의 최댓값이
있으면 뺀 뒤 합산한다. 같은 누적값을 `usage`·`stop`으로 다시 받거나 브랜치가 바뀌어도 중복 계산하지 않는다.
기간 전 기록이 없으면 기간 안 누적값 전체를 센다. 보관 기간 밖의 기준값, 누락·늦은 전송이 있으면 기간 귀속은 근사값이며,
기록이 보관돼야 기간 경계 차감이 정확해진다. 실제 운영 서버의 7일 보관에 맞춰 lookback은 7일로 두고 화면 조회도 최근 7일 이내를
권장한다. 이 저장소의 로컬 스택 기본 보관(30일)은 별도 설정이며 이번 화면 변경으로 서버 보관 정책을 바꾸지 않는다.
보관 기간과 `max_query_length`(단일 조회 범위 한도)는 다르다. 기준값 조회 기간과 화면 기간을 합친 범위가 한도 안에 있어야 한다.
Codex 비용은 `harness.json` 단가를 사용한 추정이며, 단가 미등록 모델은 비용에 포함되지 않는다(토큰은 센다).
합계에 Claude Code 키트 비용을 더하지 않으므로 Claude Code 텔레메트리와 중복되지 않는다. 청구액과는 다를 수 있다.

Prometheus 이름은 수집기가 바꾼 이름(`claude_code_cost_usage_USD_total` 등)이라 쿼리는 `{__name__=~"claude_code_cost_usage.*"}`로 쓴다.
Claude Code는 세션마다 새 시계열(`session_id`)을 만들어 `increase()`가 첫 내보내기를 세지 않으므로, 증가분은 `increase()`(수집기
재시작 등으로 누적이 0으로 돌아가도 이어 센다)에 기간 안에 처음 생긴 시계열의 첫 값을 더하고, 세션 수는 기간 안 `session_id` 수로
센다. `increase()`는 기간 끝으로 외삽하므로 짧은 기간에서는 조금 다를 수 있다. 수집기는 조용한 시계열의 누적을 24시간 유지한다.
Loki에서 속성은 점이 밑줄로 바뀐 구조화 메타데이터다(`event.name` → `event_name`, `harness.kind` → `harness_kind`).

### 주간 지표와의 대응

| 주간 지표(`harness metrics`, 플랫폼 API) | 대시보드 | 차이 |
| --- | --- | --- |
| 6. MR당 에이전트 비용·토큰(MR 본문 `## 에이전트 기록`) | 비용·토큰 패널, 키트 응답 수(브랜치별) | 주간은 병합된 MR 단위의 추정(키트 단가), 대시보드는 기간 안 Claude Code 값 + Codex 추정. 집계 단위와 단가 기준이 달라 다를 수 있다 |
| 7. 도구별 MR 수와 결과 | 사람·도구별 세션, 키트 응답 수(사람·도구별) | 세션 패널은 Claude Code 세션만 센다(텔레메트리가 있는 도구). Codex는 `telemetry push`가 보낸 응답 수·비용·토큰으로 보인다. 결과(CI 실패·누락)는 주간 지표만 있다 |
| 8. 리뷰 발견 채택률 | 없음 | 리뷰 파일·MR 본문에서만 센다 |
| hooks 기록(MR 본문 `## hooks 기록`) | 키트 hooks 개입, hook 차단(Claude Code) | 같은 사건. 대시보드는 병합 전 작업 중에도 보인다 |
| 기록 없는 커밋(`sessions summary`) | hooks 기록 없는 세션(근사) | 커밋 기준 대리 신호 대 세션 기준 신호 |
| 1~5(병합 후 누락·엄격 비율·CI 실패·판정 불일치 등) | 없음 | 병합 단위 결과는 주간 지표만 본다 |

## 확인한 것과 한계

- CI(`observability` job)가 실제 이미지로 스택을 띄우고, `telemetry push`(hooks 기록과 Codex 세션)와 Claude Code 형식의
  지표·이벤트(문서의 이름·속성으로 만든 것)를 `X-Harness-Member` 헤더와 함께 보내 `member`가 지표·로그에 붙는지 보고, 대시보드의
  모든 쿼리를 데이터소스 uid 두 벌(프로비저닝, `prometheus`·`loki`)과 사람 키 두 값(`user_email`·`member`)으로 Grafana에서 실행한다.
  단일 시점 조회에 더해 실제 Grafana `/api/ds/query`로 1시간·7일 범위 조회도 실행한다. 기준값 조회와 화면 기간이 합쳐져
  Loki 조회 한도를 넘는 회귀도 검사한다. Codex는 기간 경계의 기준값 차감과 같은 누적값의 중복 제거를 검사하고, 합계는 실제 Grafana 표현식으로 각 원천에 기록이
  있는 경우·없는 경우를 검사한다. 쿼리 구성은 [LogQL metric queries](https://grafana.com/docs/loki/latest/query/metric_queries/)와
  [Grafana expressions](https://grafana.com/docs/grafana/latest/visualizations/panels-visualizations/query-transform-data/expression-queries/)를 따른다.
  실제 Claude Code·Codex 세션의 텔레메트리와 인증 프록시(Caddy)는 CI에서 확인하지 않는다.
- 이 조각은 수집기 운영(가용성·백업·용량)을 다루지 않는다. 보관은 Prometheus·Loki 모두 30일이다.
- Claude Code 지표·이벤트 이름은 버전마다 늘어난다. 기준 문서는 [Claude Code 모니터링](https://code.claude.com/docs/en/monitoring-usage)이다.
