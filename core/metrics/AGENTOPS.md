# 실행 근거 수집

`.harness/agentops.py`는 비용·토큰 수집과 별도로 작업의 실행 근거를 기록한다. Python 표준 라이브러리만 사용한다.
키트에서는 `python bin/harness.py agentops <인자>`로도 실행한다. 모든 예시는 대상 저장소 루트 기준이다.

## 시작과 적용

새 Codex 채팅마다 수동 연결하지 않으려면 작업 폴더에서 한 번 자동 연결을 켠다.
인증 환경변수와 수집기가 이미 준비되어 있으면 이후에는 새 세션을 열기만 하면 된다.

```sh
python .harness/agentops.py auto-bind enable
python .harness/agentops.py auto-bind status
```

다음 sync·30초 수집 주기에, 활성화 이후 생성되고 현재 worktree에서 열린 Codex 세션을 자동 연결한다.
같은 worktree의 하위 폴더는 포함하지만 다른 worktree·중첩 저장소·다른 저장소는 제외한다.
세션 시작 메타데이터의 브랜치에서 Jira 키를 읽고 키나 브랜치 정보가 없으면 `unbound`로 기록한다.
이후 작업이 정해지면 수동 bind로 연결한다. 자동 연결은 기존 수동 연결·역할·PR·시작 경계를 덮어쓰지 않는다.
활성화 전에 생성한 세션은 재개하거나 로그가 갱신돼도 자동으로 소급 수집하지 않는다. 기존 세션은 아래 bind를 사용한다.
반복 enable은 시작 경계를 유지하고, disable 후 다시 enable하면 새 활성화 시각부터 적용한다.
`auto-bind disable`은 새 세션의 자동 등록만 끄며 이미 연결한 세션 수집은 계속한다.
설정은 현재 worktree의 Git 관리 디렉터리에만 남는다. 다른 worktree에서는 각각 켜고 수집기를 실행한다.
기존 watcher는 코드 업데이트 후 한 번 재시작해야 이 기능을 읽는다. 재부팅 자동 시작과 인증 설정은 이 기능이 바꾸지 않는다.

특정 세션을 직접 연결하려면 아래 명령을 사용한다.

```sh
python .harness/agentops.py bind --task TEAM-123 --tool codex --session <세션-ID>
python .harness/agentops.py sync
python .harness/agentops.py status
python .harness/agentops.py report --task TEAM-123
python .harness/agentops.py report --task TEAM-123 --html plans/agentops.html
```

Codex의 `CODEX_THREAD_ID`가 있으면 `--session`을 생략한다. 다른 저장소에서 연 세션도 명시적으로 연결할 수 있다.
연결 전 기록은 새 작업에 귀속하지 않는다. 과거 연결 복원에만 `--since 2026-10-21T10:00:00+09:00`을 지정한다.
작업을 옮기면 새 worktree에서 다시 연결한다. 부모 세션은 `--parent`, 리뷰 역할은 `--role review`, PR은 `--pr 123`으로 연결한다.
같은 작업으로 재연결하면 기존 시작 경계를 보존한다. 작업 전환 이력도 보존하므로 늦게 수집한 사건은 발생 시각의 작업에 귀속한다. 이미 저장한 시작 경계는 바꾸지 못한다. 세션 종료·응답 완료는 작업 완료와 다르다.

현재 worktree의 Git 관리 디렉터리 `harness/agentops/`에 `bindings.json`·`events.jsonl`·`transport.json`·`sync.json`·`watch.json`을 쓴다.
이 파일은 커밋하지 않는다. worktree끼리 상태를 덮어쓰지 않는다. 같은 세션을 여러 worktree에 연결하면 해당 구간을
겹치지 않게 관리한다. 보고서는 한 worktree의 증거를 보여 주며 기존 MR 비용 집계를 대체하지 않는다.

## 자동 수집과 명시적 기록

| 항목 | 자동 수집 | 사람이 명시할 내용·한계 |
| --- | --- | --- |
| 작업 연결 | 브랜치 Jira 키, 세션·턴·부모 ID | 이슈·PR·역할·역사적 연결 경계 |
| 실제 완료 | 완료 응답과 검증별 check 결과 | 전체 인수 조건별 결과와 외부 CI 근거 ID |
| 검증 | 공통 `verify` 실행의 결과·시간·상태 해시, 기존 Claude 종료 검증 결과 | 기존 hook 결과에는 명령별 해시가 없어 같은 증거 수준으로 취급하지 않음 |
| 도구 | Claude Pre/PostToolUse, Codex transcript 호출·응답 | Codex 도구 결과는 판독이 확실하지 않으면 unknown. 오류·재시도 사유는 명시 |
| 개입 | 정형 질문에 대한 실제 답변(answer) | 명시적 승인·거절·방향 수정·직접 수정·인계·재시도와 사유 |
| 구성 | 모델·추론 설정(로그에 있는 값), CLI 버전, 규칙 해시, 환경·기준 커밋 | 영역별 규칙·스킬 버전은 configuration/context로 추가 |
| 시간 | 사건 시각, 도구 호출 구간, 정형 질문·답변 대기 | 정형 도구 밖의 대기는 명시. 사건 사이 시간을 실제 작업 시간으로 단정하지 않음 |
| 컨텍스트 | compact·resume·clear, 로그에 있는 사용량·한도, Claude Read/Skill 자원 식별자 해시 | 자료를 로드한 사실은 모델이 내용을 따랐다는 증거가 아님 |
| 리뷰 후속 | 등록한 최종 처리표의 지적·처리 상태·명시적 증거 열 | 표에 없는 수정 커밋·검증 실행·사후 결함 ID |
| 수집 상태 | 전송 시각·대기·거절 수·최근 동기화 오류·수집기 생존 신호 | 생존 신호는 수집 프로세스의 상태이며 에이전트 실행 상태와 다름 |

`source`는 observed(직접 관측), human(사람 신고), agent(에이전트 신고)다. 신원 인증 기능이 아니라 기록의 출처 표시다.
서버의 사람 식별은 기존 인증 프록시가 정한다. 미수집·미실행·불명·실패를 성공이나 0으로 대체하지 않는다.

## 검증과 결과 근거

```sh
python .harness/agentops.py verify --check-id unit --tool codex --timeout 300 -- python -m unittest discover tests -q
python .harness/agentops.py record --kind outcome --data '{"criterion":"acceptance-1","status":"pass","run_id":"<검증-실행-ID>"}'
python .harness/agentops.py record --kind intervention --source human --data '{"action":"redirect","reason":"requirements","related_turn":"<턴-ID>"}'
python .harness/agentops.py record --kind wait --data '{"wait_id":"approval-1","status":"started","reason":"approval"}'
python .harness/agentops.py record --kind wait --data '{"wait_id":"approval-1","status":"ended","reason":"approval"}'
python .harness/agentops.py record --kind review --data '{"finding_id":"F1","status":"fixed","revision":"<수정-커밋>"}'
```

셸별 JSON 인용 규칙을 따른다. `record`는 식별자·분류값만 받으며 자유 서술을 저장하지 않는다.
허용 분류와 필드는 `agentops.py`의 `KINDS`, `ENUMS`, `STATUS`, `ACTIONS`, `REASONS`가 정의한다.
완료 조건 상태는 pass·fail·unverified다. 외부 근거는 URL 본문 대신 `evidence: ci:123` 같은 참조 ID로 남긴다.

검증은 셸을 통하지 않는 argv로 실행하고 코드·명령 출력은 기록하지 않는다. timeout은 후손 프로세스까지 종료한다.
검증 전후 HEAD·tracked diff·ignore되지 않은 새 파일을 해시한다. 실행 중 변경이 있으면 pass로 기록하지 않는다.
`verified_criteria`는 동일 작업의 observed/pass 검증 실행과 상태 해시가 연결된 조건 수다. 외부 CI 참조나 신고만으로 증가하지 않는다.
이 값도 검사 자체가 인수 조건을 충분히 검사하는지는 보장하지 않는다. 기존 검증 결과는 당시 상태의 증거이며 현재 상태의 보증이 아니다.

## 두 도구의 기록

새 키트 설치는 Claude의 관측 hook을 기존 보호·검증 hook 뒤에 추가한다. 기존 프로젝트는 파일을 배포한 뒤
`python .harness/agentops.py install-hooks`로 기존 설정·사용자 hook을 보존하면서 관측 등록만 추가할 수 있다.
관측 hook은 실패해도 차단하지 않는다. 기존 차단 로직을 변경하지 않는다. 쓰기 권한이 없으면 기록이 남지 않을 수 있으므로 status를 확인한다.

Codex는 `sync`가 수동 연결한 세션과 위 자동 연결 정책으로 등록한 세션만 `CODEX_HOME/sessions`에서 찾는다. 원문은 변환 중 메모리에서만 읽고 저장하지 않는다.
현재 채팅에서도 watcher로 읽을 수 있다. Codex hook 지원·신뢰 설정에 의존하지 않는다. 다른 PC 기록은 수집하지 않는다.
Claude의 과거 transcript는 아래 명령으로 명시적으로 가져온다. 도구 입력·출력은 버리고 메타데이터만 추출한다.

```sh
python .harness/agentops.py import --tool claude-code --transcript <파일.jsonl> --session <세션-ID>
python .harness/agentops.py import --tool codex --transcript <파일.jsonl>
```

가져올 세션은 원래 작업 폴더와 관계없이 먼저 bind한다. 과거 기록은 첫 bind에서 --since로 경계를 지정한다. 현재 HEAD·브랜치를 과거 사건에 붙이지 않는다.
동일 파일의 반복 import는 안정적인 사건 ID로 중복을 막는다. 파일 끝의 아직 완성되지 않은 JSON 한 줄은 다음에 읽는다.
다른 손상 행은 오류를 보이며 보고·전송을 멈춘다. 두 경로(hook과 transcript)를 같은 Claude 세션에 중복 적용한 도구 사건은
독립 관측이 될 수 있으므로 수집 경로를 하나로 운영한다. 전체 원문을 새 파일로 편집한 뒤 재수입하면 줄 기준 ID가 달라질 수 있다.

## 전송·주기 실행·끄기

```sh
python .harness/agentops.py push --dry-run
python .harness/agentops.py push
python .harness/agentops.py watch --interval 30
```

기존 `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT`/`OTEL_EXPORTER_OTLP_ENDPOINT`와 대응 HEADERS를 사용한다. 새 Secret은 필요 없다.
원격은 HTTPS만 허용한다. `watch`는 기본 30초마다 연결 세션을 동기화하고 전송한다. 중복 watcher는 잠금으로 막는다.
foreground 실행은 Ctrl+C로 끝낸다. Windows에서 백그라운드 실행할 때는 숨김 창으로 실행하고 PID·status로 확인한다.
재부팅 후에는 다시 실행하거나 운영자가 승인한 작업 스케줄러로 실행한다. 이 명령은 전역 스케줄러·사용자 환경변수를 바꾸지 않는다.

새 서비스 속성 `service.name=harness-agentops`, 사건 속성 `harness.evidence.*`, 스키마 버전 1을 사용한다.
기존 `harness` 비용·토큰 집계와 합산하지 않는다. `push --dry-run`은 전송·커서를 바꾸지 않는다.
HTTP 실패는 해당 묶음을 다시 전송한다. 일부 거절은 거절 수를 보존하고 묶음 전체의 커서를 전진시킨다(거절된 행의 ID를
서버가 알려주지 않으므로 자동으로 골라 재전송하지 않는다). 응답을 잃은 재시도에는 중복 가능성이 있어 사건 ID로 구분한다.
OTLP 수락은 최종 Loki 저장 보장이 아니다. 시계 오차·늦은 전송·Loki 거절은 별도 운영 확인이 필요하다.
전송 결과 사건은 다음 주기에 전송되므로 전송 장애 중에는 대시보드도 지연된다. 로컬 status가 우선 진단 경로다.

`core/observability/grafana/dashboards/harness-evidence.json`은 별도 대시보드다. 기존 대시보드를 교체하지 않는다.
저장소·사람·작업 정규식으로 범위를 선택한다. 사건 발생 횟수를 보여 주며 사람의 생산성 점수나 작업 성공률로 해석하지 않는다.

끄려면 watcher를 종료하고 `.harness/agentops.py`를 호출하는 관측 hook만 제거한다. 기존 보호·검증·작업 연결 hook은 보존한다.
파일 보관은 프로젝트의 Git 관리 디렉터리 보관 정책을 따른다. 이 버전은 자동 삭제를 하지 않으며 운영자가 보관 기간을 정해 삭제한다.

## 선택적 원문 보관

명시적인 `import --capture-local-content`만 사용자 프롬프트를 `harness/agentops/private/`에 로컬 보관한다.
기본값은 꺼짐이다. 원문을 중앙 전송·보고서에 포함하는 경로는 없다. 알려진 토큰·password 패턴을 마스킹하지만 완전한 탐지는 아니다.
파일 권한을 가능한 범위에서 제한하며 Windows ACL은 운영자가 확인한다. 팀 합의·보관 승인 없는 원문 수집은 켜지 않는다.
응답 본문·모델의 내부 추론·전체 도구 출력은 이 기능으로도 저장하지 않는다.

## 업무 결과·개입·대기·리뷰 자동 연동

`sync`와 기존 30초 watcher가 아래 기록도 처리한다.

- 검증 기록마다 `check:<검사 ID>`라는 검사 단위 결과를 만든다. 성공은 pass, 실패·시간 초과는 fail, 미실행·불명은 unverified다. 전체 작업 완료나 사람이 정한 모든 인수 조건 통과로 승격하지 않는다. 과거 검증도 한 번만 연결한다.
- 연결한 Codex transcript에서 정형 질문 호출과 그 질문 ID를 참조하는 정형 사용자 답변을 연결한다. 질문별 wait 시작·종료와 intervention.answer를 남긴다. 질문·선택지·답변 본문은 저장하지 않는다. 답변 문구로 승인·거절·방향 수정을 추정하지 않는다.
- 질문을 기다리는 동안 작업을 옮겨도 답변과 대기는 질문을 시작한 작업에 귀속한다. 미응답은 열린 대기로 유지하고, 여러 질문이 겹친 시간은 세션별로 한 번만 합산한다. 일반 메시지 사이 간격을 사람 대기 시간으로 해석하지 않는다.
- 등록한 최종 리뷰의 7장 처리표에서 지적 ID·심각도·채택·기각·후속·판단 필요를 추출한다. 명시적 열이 있을 때만 수정 커밋·검증 ID·사후 결함도 연결한다. 채택을 수정 완료나 검증 통과로 해석하지 않는다. 처리 상태가 이전 값으로 돌아와도 새 전이로 남기고, 삭제된 행은 removed로 표시한다. 리뷰 본문·지적 설명은 전송하지 않는다.

최종 리뷰를 watcher에 등록한다. 같은 Git 저장소의 다른 worktree에 있는 최종 파일도 가능하다. 파일명은 `<작업키>.md` 또는 `<작업키>-review.md`여야 한다. 세션별 독립 리뷰 초안은 등록하지 않는다.

```sh
python .harness/agentops.py track-review --file docs/reviews/TEAM-123.md --session <세션-ID>
python .harness/agentops.py sync
```

정형 질문 도구로 관측할 수 없는 승인·거절과 외부 확인 대기는 다음처럼 짝을 맞춰 기록한다. `--source human`은 실제 사람의 결정을 옮기는 경우만 사용한다. 이는 신원 인증이 아니라 기록 출처 표시다.

```sh
python .harness/agentops.py interaction begin --wait-id deploy-approval-1 --reason approval --session <세션-ID>
python .harness/agentops.py interaction resolve --wait-id deploy-approval-1 --action approve --reason approval --source human --session <세션-ID>
```

나머지 직접 수정·방향 변경·인계는 기존 `record --kind intervention`으로 실제 발생한 사건을 기록한다. `record --kind outcome`은 검사 결과와 별도로 정한 인수 조건에 쓴다.
`reviews.json`과 `review-cursors.json`은 Git 관리 디렉터리의 로컬 추적 상태이며 전송하지 않는다. 등록부·리뷰가 손상되면 sync.errors에 표시하고, 다른 세션과 전송을 계속한다.

검증에서 나중에 파생한 outcome, 질문에서 복원한 intervention/wait, 처리표에서 발견한 review는 중앙 로그 조회 시 수집 시각으로 색인한다. 원래 발생 시각은 `harness.evidence.ts` 속성에 보존하고 로컬 보고서·대기 시간 계산에는 원래 시각을 사용한다. 이는 Loki의 과거 로그 제한으로 늦게 발견한 근거가 버려지는 것을 방지한다. 따라서 중앙 그래프의 해당 사건 건수는 발견 시점 기준이다.
서버 조회로 누락을 확인한 사건만 `push --event-id <사건-ID>`로 선택 재전송할 수 있다. 일반 전송 커서는 그대로 유지한다. 이미 저장된 사건을 재전송하면 중앙에 중복이 생길 수 있으므로 누락 확인 없이 사용하지 않는다.

질문 대기는 질문 호출부터 정형 답변까지의 경과 구간이다. 이 사이에 에이전트가 다른 작업을 수행한 시간도 포함하므로 순수한 실행 중단 시간이나 사람의 응답 지연에 따른 손실 시간으로 해석하지 않는다.
