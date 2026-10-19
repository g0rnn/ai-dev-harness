# spring-java 프로필

Spring Boot 3.x 이상(jakarta 패키지) · Java 21 · Gradle 백엔드 영역의 기본값 묶음이다. 기능 우선 패키지
`<base_package>.domain.<기능>.{controller,service,repository,entity,dto.request,dto.response}`를 쓴다. 엔진 계약은
[profiles/README.md](../README.md), 명령은 [install.md 3.6](../../docs/install.md)을 본다.

## 적용 방법

```bash
python bin/harness.py init ../my-project --area backend --profile spring-java --var base_package=com.acme.app
python bin/harness.py scaffold backend domain MovieReview --target ../my-project
```

영역은 **Gradle 빌드 루트**(feelm `backend/`처럼 `gradlew`·`settings.gradle`이 있는 디렉터리)로 잡는다. 판정 경로는 영역
기준이라 `/build.gradle`·`/gradle/` 같은 패턴이 그 빌드 루트의 파일에 맞는다. Gradle 루트가 저장소 루트이고 영역이 그 하위
디렉터리라면 루트의 Gradle 파일(`build.gradle(.kts)`, `settings.gradle(.kts)`, `gradle/libs.versions.toml`, `gradle/wrapper/`,
lock 파일)을 최상위 `judge.trigger_paths.strict`에 직접 넣는다.

`init`은 영역 설정(`harness.json`)에 아래 기본값을 굳히고 컨벤션 테스트 두 개를 영역의
`<java_test>/<base_package 경로>/architecture/`에 렌더한다.

| 항목 | 기본값 |
| --- | --- |
| `verify` | `cd <영역> && ./gradlew build`(spotlessCheck·test 포함. 영역 검증 명령은 저장소 루트에서 실행된다) |
| `trigger_paths.strict` | `/src/main/resources/db/migration/`, `/build.gradle(.kts)`, `/settings.gradle(.kts)`, `/src/main/resources/application*.{yml,yaml,properties}`, `**/global/`, `**/infrastructure/`, `**/*SecurityConfig.java`, `gradle.lockfile`, `settings-gradle.lockfile` |
| `test_paths` | `/src/test/`, `*Test.java`, `*Tests.java`(`java_test`를 바꿔도 테스트로 판정) |
| `triggers` | 계약 문서, 다른 담당 도메인·공통 코드, `@Transactional` 경계·여러 Repository 쓰기, 인증·토큰, 메시지·캐시 키 |
| `layers` | `controller`·`service`·`repository`·`entity`·`dto`(패턴 `..<이름>..`) |
| `allow` | controller → service·dto, service → repository·entity·dto, repository → entity, dto → entity(응답 DTO의 `from(엔티티)`) |

## 변수

| 변수 | 기본값 | 내용 |
| --- | --- | --- |
| `base_package` | (필수) | 기본 패키지. 예: `com.acme.app` |
| `java_src` | `src/main/java` | 영역 기준 소스 루트 |
| `java_test` | `src/test/java` | 영역 기준 테스트 루트 |
| `lombok` | `true` | 기본 템플릿은 Lombok을 쓴다. `false`면 아래 "Lombok 없이 쓰기"를 따른다 |
| `error_package` | `<base_package>.global.error` | 공통 `BusinessException`·`ErrorCode` 위치. 스캐폴드는 호출 예시를 주석으로만 둔다 |
| `webmvc_test_package` | `org.springframework.boot.webmvc.test.autoconfigure` | `@WebMvcTest`·`@AutoConfigureMockMvc` 패키지(Boot 4.x). Boot 3.x는 `org.springframework.boot.test.autoconfigure.web.servlet` |

변수는 `init --area backend --force --var 이름=값`으로 바꾼다. `scaffold`에서 `--var`로 한 번만 바꿀 수도 있다.

## 소비자 저장소에 추가할 의존성

키트는 빌드·lock 파일을 고치지 않는다. 다음은 직접 추가한다(그 저장소에서는 빌드 파일 변경이라 엄격 판정이다).

```groovy
testImplementation 'com.tngtech.archunit:archunit-junit5:1.4.1' // 1.x 버전
```

컨벤션 테스트는 ArchUnit 1.x API를 쓴다(`layeredArchitecture().consideringOnlyDependenciesInLayers()`,
`withOptionalLayers`, `allowEmptyShould`). 의존성이 없으면 테스트 컴파일에서 실패한다. 스캐폴드 생성물은
`spring-boot-starter-web`(Boot 4.x는 `-webmvc`)·`spring-boot-starter-data-jpa`·Lombok과 테스트 의존성
(Boot 3.x `spring-boot-starter-test`, Boot 4.x는 여기에 `spring-boot-starter-webmvc-test`)을 전제로 한다.

## 스캐폴드 `domain`

`scaffold <영역> domain <Name>`은 8개 파일을 만든다. 같은 경로 파일이 하나라도 있으면 아무것도 만들지 않는다.

| 파일 | 내용 |
| --- | --- |
| `controller/<Name>Controller` | `/api/v1/<name-kebab>` GET·POST. 응답 DTO 반환, 생성은 `201 Created` |
| `service/<Name>Service` | 클래스 `@Transactional(readOnly = true)`, 쓰기 메서드 `@Transactional` |
| `repository/<Name>Repository` | `JpaRepository<<Name>, Long>` |
| `entity/<Name>` | `@Getter`·`@NoArgsConstructor(access = AccessLevel.PROTECTED)`, 정적 팩토리 `create` |
| `dto/request/<Name>CreateRequest`, `dto/response/<Name>Response` | record. 응답은 `from(엔티티)` |
| `<Name>ControllerTest` | `@WebMvcTest` + `@AutoConfigureMockMvc(addFilters = false)` |
| `<Name>ServiceTest` | Mockito(`MockitoExtension`) |

- 오류 처리는 도메인 예외·응답 래퍼를 만들지 않는다. 없는 리소스는 `orElseThrow()`로 두고, 공통
  `BusinessException(ErrorCode.…)`로 바꾸는 예시를 주석으로 남긴다
- `@WebMvcTest`의 Service 대역은 `@MockBean`(Boot 3.4 폐기, 4.x 제거)이나 `@MockitoBean`(Boot 3.4 이상)이 아니라
  중첩 `@TestConfiguration`의 `Mockito.mock` 빈이다. Boot 3.0~4.x에서 같은 코드가 돈다. 패키지만 다르므로
  `webmvc_test_package`로 맞춘다
- 생성 테스트에 `@Disabled`를 쓰지 않는다(skip이 있으면 실패하는 CI 대비)
- 공유 파일(보안 설정의 허용 경로)과 DB 마이그레이션은 고치지 않고 붙일 조각과 안내를 출력한다

### 형식(spotlessApply)

템플릿은 google-java-format AOSP 형식(4칸 들여쓰기, 100자, 와일드카드 import 없음)으로 썼지만 이름 길이에 따라
줄바꿈과 import 순서(`base_package`가 `lombok`·`org`보다 뒤에 정렬되는 경우, Boot 3.x용 `webmvc_test_package`를 준
경우 `ControllerTest`의 `test.autoconfigure`·`test.context` 순서)가 달라질 수 있다. 생성 뒤 `./gradlew spotlessApply`를
실행한다.

### 줄끝(Windows)

키트는 생성 파일을 LF로 쓴다. Windows에서 아무 설정이 없으면 Spotless가 CRLF를 기대해 `spotlessCheck`가 모든 생성
파일에서 실패하고, `spotlessApply`로 CRLF로 바꿔도 다음 `init --force`가 LF로 되돌린다. 두 설정을 함께 둔다.

```groovy
spotless {
    lineEndings 'UNIX'
    java { /* ... */ }
}
```

```gitattributes
# <영역>/.gitattributes
*.java text eol=lf
```

- `.gitattributes`가 핵심이다. Spotless 기본 줄끝은 git 속성을 따르므로 git 저장소 안에서는 이것만으로 통과한다.
  `core.autocrlf=true`(Windows Git 기본)로 새로 clone해도 `.java`가 LF로 풀린다
- `lineEndings 'UNIX'`는 git 저장소 밖(소스만 복사한 빌드 등)에서 Windows가 CRLF를 기대하지 않게 하는 보조 설정이다.
  이것만 두면 `autocrlf` 새 clone의 CRLF 파일에서 실패한다
- 키트 CI는 Linux라 이 문제가 드러나지 않는다. Windows(Spotless 7.2.1)에서 두 설정의 조합을 확인했다(#83)

### Lombok 없이 쓰기

엔진에 조건문이 없으므로 `lombok=false`는 템플릿을 바꾸지 않는다. 대상 저장소에
`.harness/templates/spring-java/domain/<파일>`(예: `Controller.java`, `Service.java`, `Entity.java`)을 두면 프로필의
같은 이름 템플릿 대신 쓴다. 생성자와 getter를 직접 쓴 템플릿으로 덮어쓴다. 자리표시자는 프로필 템플릿과 같다.

## 컨벤션 테스트

계층 기본값은 feelm의 **문서 규칙**(`backend-convention.md`)을 옮긴 것이다. feelm 코드 자체는 이 규칙을 모두 지키지 않는다
(조회용 repository → dto, controller → 엔티티 enum, dto → service, service 밖 `@Transactional` 등). 기존 코드가 있는 저장소에
적용하면 첫 `./gradlew build`에서 위반이 드러날 수 있으므로, 아키텍처 ADR에 맞춰 영역의 `allow`를 고치거나 고정 규칙을
`disabled_rules`(이유 필수)로 끄고 `init --area <dir> --force`로 다시 생성한다.

`FixedRulesArchitectureTest`(고정 규칙)와 `LayerRulesArchitectureTest`(계층 설정 규칙) 두 ArchUnit JUnit 5 클래스다.
`harness check`가 드리프트를 보므로 손으로 고치지 않는다. 테스트 소스는 검사하지 않는다(`DoNotIncludeTests`).
아직 클래스가 없는 계층·규칙은 실패하지 않는다(`withOptionalLayers(true)`, `allowEmptyShould(true)`).

| 규칙 ID | 내용 |
| --- | --- |
| `transactional-in-service-only` | `@Transactional`(Spring·jakarta)을 `..service..` 밖의 클래스·메서드에 두지 않는다 |
| `controller-no-entity-return` | `..controller..`의 공개 메서드가 `@Entity` 타입을 반환하지 않는다(`ResponseEntity<엔티티>`·`List<엔티티>` 포함) |
| `no-field-injection` | `@Autowired`·`@Inject` 필드 금지 |
| `service-no-web-types` | `..service..`가 `jakarta.servlet..`·`ResponseEntity`에 의존하지 않는다 |
| `no-service-cycle-between-domains` | `<base_package>.domain.(*).service..` 사이 순환 금지 |
| `no-cross-domain-persistence` | `<base_package>.domain.<A>..`의 클래스가 다른 기능 `domain.<B>`의 `..repository..`·`..entity..`에 의존하지 않는다. 다른 기능의 상태 변경은 이벤트로, 조회는 그 기능의 public service로 한다. JPA 연관(`@ManyToOne` 다른 기능 엔티티)도 위반이므로 ID로 참조한다. `entity` 패키지 전체를 막으므로 다른 기능에 노출되는 enum·값 타입은 `entity/` 밖(예: `dto` 또는 공통 패키지)에 둔다 |
| `event-publisher-in-service-only` | `domain..` 안에서 `ApplicationEventPublisher`는 `..service..`만 의존한다(이벤트 핸들러·controller에서 재발행 금지). `global`·`infrastructure`는 보지 않는다. `ApplicationEventPublisher`에 대한 **직접 의존만** 본다: `global/event`의 발행 래퍼, `ApplicationContext` 주입, `AbstractAggregateRoot.registerEvent`는 잡지 않으므로 래퍼를 두면 그 래퍼도 service에서만 쓰도록 리뷰한다 |

고정 규칙을 끄려면 `harness.json` 영역에 이유를 적고 다시 생성한다. 끄면 그 규칙의 `@ArchTest` 필드만 빠진다.

```json
"disabled_rules": {"no-field-injection": "레거시 모듈 이관 중(#123)"}
```

계층 규칙은 영역의 `layers`·`allow`를 고친 뒤 `init --area <dir> --force`로 다시 생성한다. `allow`에 없는 계층 사이
의존은 금지이고, 어느 계층에도 속하지 않는 클래스(`global`, `infrastructure` 등)는 보지 않는다. 고정 규칙의
패키지 패턴(`..controller..`·`..service..`)은 계층 설정과 별개다.
