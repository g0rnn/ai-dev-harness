# core/dashboard

로컬 실시간 대시보드(M5-7). 작업 중에 자기 저장소의 모든 worktree에서 도는 에이전트 세션과 hooks 개입을 브라우저로 본다.
키트에서 실행하며 대상 저장소에 복사하지 않는다. 표준 라이브러리만 쓰고 외부 리소스(CDN·폰트)를 부르지 않는다.

```bash
python bin/harness.py dashboard ../my-project          # http://127.0.0.1:8765/ (키트 자신은 --self)
python bin/harness.py dashboard ../my-project --port 0 # 빈 포트
python bin/harness.py dashboard ../my-project --once   # 서버 없이 지금 상태를 JSON으로
```

| 화면 | 내용 |
| --- | --- |
| 합계 | 진행 중 세션 수, 오늘 응답·추정 비용·hooks 개입, 이번 주 세션·추정 비용·hooks 개입(`--utc-offset`, 기본 +09:00, 주는 월요일부터) |
| 브랜치별 추정 비용 | 이번 주 비용이 큰 브랜치 10개의 오늘·이번 주 비용 |
| 진행 중 세션 | worktree·브랜치·도구·모델, 현재 작업 구간의 시작·경과(resume·2시간 넘는 공백에서 새로 센다), 마지막 기록, 응답 수, 마지막 종료 검증 결과, 세션 누적 토큰과 추정 비용 |
| 기록 없는 커밋 | worktree마다 현재 브랜치에서 어느 세션 기록에도 들지 않는 커밋 수(hooks 꺼짐·다른 도구의 대리 신호, 30초마다 갱신) |
| 최근 hooks 개입 | 보호 경로 `block`·`ask`와 종료 검증 실패 30개 |

- 데이터: 공용 git 디렉터리의 `harness/`와 `worktrees/*/harness/`의 `sessions.jsonl`·`events.jsonl`
  ([`core/hooks/README.md`](../hooks/README.md) "세션 기록")과 이 PC의 Codex 세션 로그(`CODEX_HOME`, 이 저장소 worktree에서 연 세션만,
  [`core/metrics/README.md`](../metrics/README.md) "에이전트 기록"). 진행 중 세션은 `end`가 없고 마지막 기록이 2시간 안인 세션이다.
- 진행 중 세션의 토큰은 세션 시작 기록의 transcript와 서브에이전트 transcript에서 바로 센다(모든 파일의 크기·수정 시각이 같으면 캐시. 일부 파일을 못 읽으면 줄어든 합을 다시 시작으로 보지 않는다). 그래서 응답이 끝나기 전에도
  늘어난다. 끝난 세션은 마지막 기록의 누적값을 쓴다. 비용은 `sessions summary`와 같은 단가(키트 기본값 + `harness.json` `pricing`)로
  계산한 **추정**이다.
- 오늘·이번 주 비용과 응답 수는 사건 단위로 센다. 사건마다 직전 누적과의 차(`sessions summary`와 같은 계산)를 그 사건의 날짜와
  브랜치에 넣으므로, 자정·월요일을 넘는 세션도 그날 쓴 만큼만 든다. 진행 중 세션이 마지막 기록 뒤 더 쓴 토큰은 지금 시각에 든다.
- 시각을 읽을 수 없는 줄(깨진 JSON, 범위 밖 월·오프셋)은 건너뛴다.
- 갱신: Server-Sent Events(`/api/stream`)로 1초마다 상태를 읽어 바뀌었을 때만 보낸다. 브라우저가 EventSource를 지원하지 않으면
  2초마다 `/api/state`를 읽는다. 탭이 여러 개여도 상태는 1초에 한 번 계산해 나눠 쓴다. 기록 없는 커밋(git 호출)은 별도 스레드가
  30초마다 세므로 상태 갱신을 늦추지 않는다(`--once`는 그 자리에서 센다).

## 보안

- `127.0.0.1`에만 바인딩한다. 다른 주소로 여는 옵션이 없다(원격 접속·인증은 범위 밖, 팀 공용은 M5-8 선택 조각).
- `Host` 헤더가 `localhost`·`127.0.0.1`이 아니면 403이다(DNS rebinding으로 다른 사이트가 상태를 읽지 못하게).
- 화면은 값을 `textContent`로만 넣는다(`innerHTML` 없음). CSP로 같은 출처 외의 요청을 막는다.
- 기록과 화면에는 프롬프트·코드 내용이 없다. 보호 경로 개입의 파일 경로와 규칙 이름은 보인다.
