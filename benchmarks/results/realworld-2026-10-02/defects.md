# Real-world run 2026-10-02: defects found

Verinoda 0.3.2 (code at 488b553; the run commits 8d9ab97 and 1ab4a71 add only gold files and the
manifest), Python 3.13.14, Windows 11, one process at a time. All 10 repositories ran:
**0 crashes, 0 timeouts, every clone clean after the revert.** Gold v1 77/100, v2 6/7.

Every miss below was looked at by hand: I ran the same read-only Verinoda command on the scanned
clone and read the source lines again. Two misses were the gold's own fault (express route checks).
They got v2 entries (`route-posts@v2`, `route-user-edit-put@v2`, both hit), and v1 was not edited. All
the other misses are Verinoda limitations, listed here. I did not change any Verinoda code.

Commands run from the clone (`C:/vbench/<owner>__<name>`, `C:/vbench-rw/Graphify-Labs__graphify`) as
`python -P -m verinoda ...`. Suspected files are guesses from reading the code, not confirmed fixes.

## D1. Node ids fold case and leading underscores, so distinct symbols merge (high)

`normalize_id` casefolds and collapses `_+` (verinoda/project_index/ids.py:50-93, `make_id`). As a
result:

- **axios:** `Axios.request` (lib/core/Axios.js:40) and `Axios._request` (lib/core/Axios.js:83) both
  become `lib_core_axios_axios_request`. `_request` disappears, and its calls are attributed to
  `request`.
  - Repro: `q 'match (f:function) where f.file = "lib/core/Axios.js" return f' --json`. Expected
    `_request` at :83. Actual: only `.request()` at :40. The edge "`request` -> `mergeConfig` at
    lib/core/Axios.js:93" is reported, but line 93 is inside `_request`.
  - Gold misses: `request-calls-private`, `private-request-merges-config`,
    `request-reaches-adapter` (`trace _request ...` is unresolved).
- **express:** the selector `lib/response.js::sendFile` resolves to the module function `sendfile`
  (lib/response.js:927). The two names differ only in case.
  - Repro: `trace lib/response.js::sendFile lib/response.js::sendfile --json`. Expected: the
    source does not resolve (`res.sendFile` is not a symbol, see D2), or it resolves at :378.
    Actual: both ends are `lib_response_sendfile`, with the status "ambiguous: both endpoints
    resolved to the same node". The ambiguity should come with the warning that the source was
    matched case-insensitively.
  - Gold miss: `sendfile-calls-helper`.
- The same can happen in any language that has `foo` / `_foo` or `getX` / `getx` pairs, which are
  common in Python (public method plus private helper) and in Java.
- Suspected files: verinoda/project_index/ids.py (normalize_id), verinoda/project_index/build.py
  (node merge by id), and the selector resolver in verinoda/naming.py or verinoda/cli.py (trace).

## D2. JavaScript methods assigned to an object property are not symbols (high, Express-shaped code)

`res.json = function json(obj) {...}`, `app.render = function render(...)` and similar
assignments create no node. In Express 4/5, lib/response.js, lib/request.js and
lib/application.js are written almost entirely this way. Only the plain function declarations are
indexed (`sendfile`, `stringify` and the callbacks nested in `sendfile`).

- Repro (express): `q 'match (f:function) where f.file = "lib/response.js" return f' --json`.
  Expected `json` at :239, `send` at :125, `sendFile` at :378, `render` at :900, and so on. Actual:
  9 rows, all plain `function` declarations.
- Gold misses: `json-calls-stringify`, `json-calls-send`, `render-reaches-tryrender` (`no symbol
  ... named lib/response.js::render`). The scenario `trace lib/response.js::json stringify` is
  unresolved for the same reason.
- Suspected file: the JS extractor in verinoda/project_index/extract.py. It has to handle
  `assignment_expression` with a `member_expression` left side and a (named) `function_expression`
  or arrow function on the right. The name should be the function's own name, else the property
  name.

## D3. FastAPI route paths miss the `include_router(..., prefix=settings.X)` prefix (medium)

full-stack-fastapi-template mounts every router with
`app.include_router(api_router, prefix=settings.API_V1_STR)` (backend/app/main.py:49). The value is
`API_V1_STR: str = "/api/v1"` (backend/app/core/config.py:22).

- Repro: `routes --json`. Expected `POST /api/v1/login/access-token` and
  `GET /api/v1/items/{id}`. Actual: `/login/access-token` and `/items/{id}`, without the prefix
  and without a `mount: "not found"` note. The table looks complete, but it is wrong.
- Also, `@router.get("/")` under `prefix="/items"` is listed as `/items`. FastAPI serves `/items/`,
  and the generated client calls `/api/v1/items/`.
- Because of this, the generated SDK
  (frontend/src/client/sdk.gen.ts:31 `url: '/api/v1/login/access-token'`) links to nothing
  (`linked: 0`).
- Gold misses: `route-login-access-token`, `route-read-item`.
- Suggested behaviour: resolve a class attribute default of a pydantic `Settings` instance (or any
  module-level constant). If the prefix cannot be resolved, mark the row `mount: "prefix not
  resolved"` instead of dropping it silently.
- Suspected file: verinoda/cross_service.py (the `prefixes()` and `join_path` logic used by
  `routes()`, around lines 1540-1585).

## D4. Python `@overload` stubs hide the implementation (medium)

sqlmodel declares `Field` three times with `@overload` (sqlmodel/main.py:242, 291, 349) and
implements it at :388.

- Repro: `q 'match (f:function) where f.name = "Field" and f.file = "sqlmodel/main.py" return f' --json`.
  Expected: the implementation at :388 (the stubs at most as extra rows). Actual: one row, at :242
  (the first stub).
- A trace or a "what does Field call" question therefore starts from an empty stub body.
- Gold miss: `def-field-impl`.
- Suspected file: the Python extractor in verinoda/project_index/extract.py. Same-name defs in
  one scope collapse to one id (D1), and the first one wins. It should prefer the definition
  without `@overload`.

## D5. Static and associated-function calls go to the class, not the method (PHP, Rust) (medium)

- **guzzle:** `Utils::chooseHandler()` (src/HandlerStack.php:58) and `Middleware::redirect()` (:60)
  produce INFERRED `calls` edges to the classes `Utils` and `Middleware`, not to the methods
  `chooseHandler` (src/Utils.php:73) and `redirect` (src/Middleware.php:213). Both methods are
  indexed.
  - Repro: `q 'match (a)-[calls]->(b) where a.file = "src/HandlerStack.php" return a, b' --json`.
  - Gold misses: `stack-create-chooses-handler`, `stack-create-pushes-redirect`.
- **bat:** `Controller::new(config, &assets)` (src/bin/bat/main.rs:279) has no edge to
  `Controller::new` (src/controller.rs:30, indexed as `src_controller_controller_new`).
  - Repro: `q 'match (a)-[calls]->(b) where a.name = "run_controller" return a, b' --json`.
  - Gold miss: `run-controller-new`.
- Suspected files: the PHP and Rust call resolution in verinoda/project_index/extract.py (a
  `scoped_call_expression` or `scoped_identifier` callee should resolve `Class::method` to the
  method).

## D6. Method calls on a local variable, a trait object or through `.call`/`.bind` make no edge (medium)

This is the same family as gin's `root.getValue`, already noted in DESIGN section 134.

- **bat:** `controller.run(inputs, None)` (src/bin/bat/main.rs:283, `controller` a local of type
  `Controller`) makes no edge. The scenario `trace src/bin/bat/main.rs::run_controller
  src/controller.rs::run_with_error_handler` gives "no directed path".
- **bat:** `printer.print_header(...)` (src/controller.rs:222, `printer: &mut dyn Printer`) makes
  no edge to the trait method or to its impls. Gold miss: `print-file-calls-header`.
- **axios:** `dispatchRequest.call(this, newConfig)` (lib/core/Axios.js:242) and
  `dispatchRequest.bind(this)` (:195) make no edge. The scenario `trace lib/core/Axios.js::request
  lib/core/dispatchRequest.js::dispatchRequest` gives "no directed path". Part of this is D1:
  `_request` is merged into `request`.
- Suspected file: the receiver-type inference in verinoda/project_index/extract.py. For a local
  `let x = T::new(...)` or `const x = new T()`, the type is known from the initializer.
  `f.call(...)` and `f.bind(...)` should count as calls of `f`.

## D7. A nested arrow function inside a function is not a symbol (low)

`const login = async (data) => {...}` inside `useAuth` (frontend/src/hooks/useAuth.ts:41) is not a
node. Its call to `LoginService.loginAccessToken` (:42) is attributed to `useAuth` (:18). The edge
itself is right.

- Repro: `q 'match (f:function) where f.file = "frontend/src/hooks/useAuth.ts" return f' --json`
  returns only `isLoggedIn` (:14) and `useAuth` (:18).
- Gold miss: `frontend-login-calls-sdk`.
- Suspected file: the TS/JS extractor in verinoda/project_index/extract.py (function-scoped
  `lexical_declaration` with an arrow function value).

## D8. Java methods with an annotation are located at the annotation line (low)

`@SuppressWarnings("fallthrough")` is at JsonReader.java:581, and `int doPeek()` is at :582.

- Verinoda gives `doPeek` at :581.
- For Python, a decorated function is placed on its `def` line (template `login_access_token` at
  login.py:24, under the decorator at :23).
- This inconsistency makes "defined at file:line" answers for Java one line off whenever an
  annotation is present (`@Override` is on almost every method).
- Repro: `q 'match (f:function) where f.name = "doPeek" return f' --json`.
- Gold miss: `def-dopeek`. The gold is kept: the declaration is at :582.
- Suspected file: the Java extractor in verinoda/project_index/extract.py (it uses the
  `method_declaration` start, which includes the `modifiers` node; it should use the name's line or
  the first non-annotation token).

## D9. `routes --json` is very large on repositories with many tests (low)

- express: 248 KB. 929 "clients" (supertest calls in test/) are matched against 286 routes of
  unrelated example apps, and most become `ambiguous` entries with 8 candidates each.
- sqlmodel: 214 KB (from its docs_src tutorials).
- Linking a test's `request(app).get('/')` to every example app's `GET /` adds nothing. Clients
  in test files could be linked only to the app their file imports. At the least, the output
  should be capped like `query --max-items`.
- Suspected file: verinoda/cross_service.py (`link_http`, `ambiguous`).

## Harness defects (benchmarks/realworld/run.py, not Verinoda)

- **H1. The routes judge cannot check a method.** `judge()` compares `check["method"]` with
  `r.get("method")`, but route rows have `methods` (a list, or null for `all`/`use`). A routes fact
  with a method would always miss, so the gold files of this run keep the method in the fact text
  only. Fix: test `check["method"].upper() in (r.get("methods") or [])`.
- **H2. The routes judge matches the handler as a substring of the whole row**
  (`check["handler"] in json.dumps(r)`), and the handler field holds a node id. A gold file must
  name node ids (`examples_route_separation_post_list`), not file paths. README step 3 should say
  so. This is what the two express v2 entries fix.
- **H3. The summary header lists one environment line per run.** It now shows two (8d9ab97,
  1ab4a71) although the Verinoda code is the same. The commits differ only in benchmark files. A
  note "code unchanged since <sha>" (for example `git diff --quiet A B -- verinoda/`) would avoid
  the false impression that two Verinoda versions were measured.
- **Note.** Graphify ran from its own clone (`--work C:/vbench-rw`) because `C:/vbench/Graphify-Labs__graphify`
  is shared read-only with another agent, and the run writes `.verinoda/` and makes one scripted
  edit there. Same sha, same files.
