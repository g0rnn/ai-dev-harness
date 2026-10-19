# react-ts 프로필

React + TypeScript + Vite 웹 프론트용 기본값 묶음(M3-3). 근거는 feelm 프론트(`7d86dab5`)의 구조와
M3 결정(R-1~R-9)이다. 엔진 계약은 [profiles/README.md](../README.md)를 본다.

- 기준 스택: React 18 이상, TypeScript, Vite, react-router 데이터 라우터(`createBrowserRouter`), Vitest + Testing Library,
  ESLint flat config(9.7 이상. 생성 파일이 쓰는 `no-restricted-imports`의 `patterns[].regex`가 9.7.0에서 생겼다), Node 22
- 템플릿은 `react-router` 패키지에서 import한다(v7 이상). v6 데이터 라우터 프로젝트는 보통 `react-router-dom`만 선언하므로
  `.harness/templates/react-ts/screen/`에서 import를 `react-router-dom`으로 바꾼다(전이 의존성에 기대면 pnpm에서 깨진다)
- 새 런타임 의존성을 요구하지 않는다(axios·react-query·MSW 없음). 검사용 devDependency는 `eslint-plugin-jsx-a11y` 하나다

## 적용 방법

```bash
python bin/harness.py init ../my-project --area frontend --profile react-ts
python bin/harness.py scaffold frontend api-client Client --target ../my-project   # 클라이언트가 없을 때 한 번
python bin/harness.py scaffold frontend api movie-review --target ../my-project
python bin/harness.py scaffold frontend screen MovieDetail --var area=movies --target ../my-project
```

1. `init`이 영역 설정(검증 명령, 판정 경로, 트리거, `layers`·`allow`)을 `harness.json`에 굳히고
   `frontend/eslint.harness.js`를 렌더한다. 이 파일은 손으로 고치지 않는다(`harness check`가 드리프트를 본다)
2. 소비자가 직접 한다(키트는 빌드·lock 파일을 고치지 않는다). `package.json` 변경은 그 저장소의 엄격 트리거다
   - `npm install -D eslint-plugin-jsx-a11y`
   - ESLint 10을 쓰면 `eslint-plugin-jsx-a11y` 6.10.2의 peer 범위(`^3`~`^9`)에 10이 없어 `npm install`이 ERESOLVE로
     멈춘다. `package.json`에 다음을 둔다(실제 lint는 ESLint 10에서 동작함을 확인했다)

     ```json
     "overrides": { "eslint-plugin-jsx-a11y": { "eslint": "$eslint" } }
     ```

   - `eslint.config.js`에서 생성 파일을 가져와 **설정 배열 끝에** 펼친다

     ```js
     import harness from './eslint.harness.js';

     export default tseslint.config(/* 기존 설정 */, ...harness);
     ```

   - 생성 파일은 Prettier 대상이 아니다. `.prettierignore`에 `eslint.harness.js`와 영역 `AGENTS.md`를 넣는다
     (생성 `AGENTS.md`의 표는 Prettier 형식과 달라 `format:check`가 실패한다. 손으로 고치면 다음 `init --force`가 되돌린다)
3. `scaffold`는 파일을 한 번 만들고 팀이 소유한다. 라우트는 출력된 조각을 사람이 붙인다(아래)

## 변수

| 변수 | 기본값 | 쓰임 |
| --- | --- | --- |
| `src_root` | `src` | 영역 기준 소스 루트. 스캐폴드 경로와 ESLint `files`의 기준 |
| `api_client_import` | `./client` | api 도메인 모듈이 공통 클라이언트를 가져오는 경로(api 디렉터리 기준) |
| `api_client_fn` | `request` | 공통 클라이언트의 요청 함수(`request<T>(path, options)`) |
| `lint_script`·`format_script`·`test_script`·`build_script` | `lint`·`format:check`·`test:run`·`build` | 영역 검증 명령 `npm --prefix <영역> run <스크립트>` |
| `area`(screen) | 빈 값 | 화면 영역 디렉터리. 비우면 `pages/<Name>Page/` |

## 스캐폴드

| 종류 | 생성물 | 비고 |
| --- | --- | --- |
| `screen <Name>` | `src/pages/<area>/<Name>Page/{index.tsx, <Name>Page.module.css, <Name>Page.test.tsx}` | 테스트는 `vi.mock`으로 공통 클라이언트를 대체하고 `createMemoryRouter`+`RouterProvider`로 렌더, `getByRole`로 질의 |
| `api <domain>` | `src/api/<domain>.ts`, `src/api/<domain>.test.ts` | 공통 클라이언트 호출, 함수마다 `// API-ID:` 자리. 테스트는 `vi.stubGlobal('fetch', …)` |
| `api-client <Name>` | `src/api/<name>.ts` | 클라이언트가 없는 새 저장소용 최소 `request<T>()`(fetch, 2xx 밖이면 `ApiError` throw). 엔진이 이름 인자를 요구하므로 `Client`로 준다 |

- `screen`은 공유 파일 `routes/paths.ts`(`ROUTE_PATTERN`·`path`)와 `routes/router.tsx`(import·라우트 객체)에 붙일 조각을
  출력한다. 붙이는 위치(레이아웃, 인증 가드 안팎)는 사람이 정한다. `area`를 비우면 조각의 import 경로에 생기는 `//`를 `/`로 고친다
- 화면 템플릿은 영역 깊이가 달라도 컴파일되도록 화면 디렉터리 밖을 상대 경로로 import하지 않는다. 테스트의 `vi.mock`은
  Vite 루트 기준 경로(`/src/api/client`)를 쓴다(영역 디렉터리가 Vite 루트라고 가정). 모듈 이름 `client`는 고정이라
  `api_client_import`를 바꾼 저장소는 테스트의 `vi.mock` 경로도 직접 맞춘다. Vitest는 없는 모듈을 mock해도 경고하지 않는다
- 템플릿 형식은 feelm Prettier 설정(`singleQuote`, `printWidth: 100`, `trailingComma: all`)에 맞췄다. 설정이 다르면 생성 후
  `prettier --write`
- 아키텍처가 다르면 저장소의 `.harness/templates/react-ts/<종류>/<파일>`로 템플릿을 덮어쓴다(엔진 계약)

## 컨벤션 규칙 (`eslint.harness.js`)

| 규칙 ID | 종류 | 내용 |
| --- | --- | --- |
| `no-mocks-import` | 고정 | `pages/`·`components/` 소스는 `mocks/`를 import하지 않는다 |
| `no-cross-screen-import` | 고정 | `pages/` 소스는 위로 올라가 다른 화면 디렉터리(`*Page/`)로 들어가는 import를 하지 않는다 |
| `jsx-a11y-recommended` | 고정 | `eslint-plugin-jsx-a11y` recommended(`src/**/*.{jsx,tsx}`) |
| (설정) | `layers`·`allow` | 계층마다 허용하지 않은 계층으로의 상대 import 금지(ESLint 내장 `no-restricted-imports`) |

- 고정 규칙은 `harness.json` 영역의 `disabled_rules`에 `"규칙 ID": "이유"`를 적고 `init --area frontend --force`로 끈다
- `layers`의 값은 glob이 아니라 **소스 루트 기준 디렉터리**다(예: `"pages": ["pages"]`). 기본 계층과 허용 방향:

  | 계층 | 의존해도 되는 계층 |
  | --- | --- |
  | `routes` | pages, components, api, types, utils |
  | `pages` | components, api, types, utils, routes(경로 상수 `routes/paths.ts`) |
  | `components` | types, utils |
  | `api` | types, utils |
  | `types` | (없음) |
  | `utils` | types |

  계층에 속하지 않는 디렉터리(`layouts/`·`hooks/`·`contexts/`·`mocks/` 등)는 계층 규칙 대상이 아니다. 필요하면 계층으로 더한다
- 검사 범위와 한계: 소스 루트 아래 상대 경로 import만 본다. 별칭 import(`@/…`)의 계층 방향은 보지 않는다(`mocks` 금지는 별칭도
  잡는다). 테스트 파일(`*.test.*`·`*.spec.*`)은 경계 규칙에서 뺀다(목업 데이터·여러 화면을 다루는 흐름 테스트).
  디렉터리 아래 12단계보다 깊은 파일은 보지 않는다
- `no-restricted-imports`는 한 파일에 설정 객체가 여러 개 겹치면 마지막 옵션만 남는다. 생성 파일은 제한을 파일 묶음마다 하나로
  모으고, 계층 디렉터리가 겹치면(`components` 안의 `components/shared`) 안쪽 묶음에 바깥 묶음의 제한을 합쳐 뒤에 둔다.
  소비자 설정에서 같은 규칙을 이 파일 **뒤에** 다시 켜면 이 파일의 제한이 사라지고, **앞에** 둔 소비자 규칙도 계층 디렉터리
  안 파일에서는 이 파일의 옵션으로 대체된다. 소비자 고유 import 금지 패턴을 계층 디렉터리에 함께 걸 방법은 아직 없다(후속 과제)

## 영역 판정 기본값

- 검증: `npm --prefix <영역> run lint`, `… run format:check`, `… run test:run`, `… run build`(영역 검증 명령은 저장소 루트에서
  실행되므로 `--prefix`로 영역의 `package.json`을 쓴다)
- 엄격 경로: `/package.json`, lock 파일, `/vite.config.*`, `/eslint.config.*`(`...harness`를 빼면 규칙이 조용히 꺼진다)
- 표준 경로: `/src/routes/`, `/src/components/`. 경량(테스트만): `*.test.ts`·`*.test.tsx`
- `src_root`를 바꾸면 `harness.json`의 `trigger_paths`도 함께 고친다(판정 경로는 변수를 쓰지 않는다)

## 템플릿 작성 제약

템플릿 본문에는 자리표시자가 아닌 `{{`·`}}`를 쓸 수 없다(이스케이프 없음). JSX 인라인 스타일은 `style={ { color: 'red' } }`처럼
중괄호 사이를 띄우거나 CSS Module을 쓴다. 정규식 반복 횟수 `{n}`처럼 `}}`가 생기는 JS 코드도 문자열을 이어 붙여 피한다.
