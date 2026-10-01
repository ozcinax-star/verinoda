## Why

The real-world run of 2026-10-02 (benchmarks/results/realworld-2026-10-02/defects.md, D2 and D7) found two gaps in
the JavaScript/TypeScript extractor (vendored Graphify, `verinoda/project_index/extractors/engine.py`):

- A function assigned to an object's property at module level (`res.json = function json(obj) {...}`,
  `app.render = function render(...)`, `app.use = (fn) => {...}`) made no symbol. Express 4/5 writes almost all of
  lib/response.js, lib/request.js and lib/application.js this way, so `q` found 9 plain function declarations in
  lib/response.js, `trace lib/response.js::json stringify` was unresolved, and `lib/response.js::sendFile` fell back
  to the module function `sendfile` (the D1 case fold).
- A function bound inside a function (`const login = async (data) => {...}` inside the `useAuth` hook) was no
  symbol, so its calls (`LoginService.loginAccessToken`) were credited to `useAuth`.

Before, only `Foo.prototype.bar = fn`, `exports.x = fn`, `this.x = fn` and an object literal built in the same
function were methods, and only nested `function` declarations were nested symbols.

## Decisions

- **Which assignments.** A module-level statement `obj.m = <function expression | arrow | generator>` makes a symbol
  when `obj` is bound at module level in the same file: a `var`/`let`/`const` declarator (any initializer:
  `Object.create(...)`, `exports = module.exports = {}`, `require(...)`, `X.prototype`), a function declaration or a
  class declaration (also exported). An undeclared receiver (`window.x = fn`, a global) and a value that is not a
  function (`res.limit = 10`, `res.handlers = [f]`) make nothing. This keeps the #1077 guard against bare-named
  phantom nodes. Assignments inside function bodies keep the earlier rules (`this.x`, a local object literal).
- **Alias chains.** In `res.contentType = res.type = function contentType() {...}` (also Express's
  `res.set = res.header = ...` and `req.get = req.header = ...`), every module-object member of the chain is a method
  at its own line. The body is walked once per name. Nested symbols are made once, under the name next to the
  function.
- **Label and shape, as for prototype and class methods.** The method is `.m()` with id `<file>_<obj>_<m>`, joined by
  a `method` edge from the owner. A `var`/`let`/`const` owner without a node gets one (label `obj`, at its
  declarator's line) and a `contains` edge from the file. A function or class owner is its own node. The method's
  line is the assignment's line. So `q ... f.name = "json"` finds `res.json`, and the selector
  `lib/response.js::sendFile` resolves to `res.sendFile` (lib/response.js:378), whose id no longer folds onto
  `sendfile`.
- **The property name is the method's name**, not the function's own name: `res.type = function contentType` is
  `.type()`, because callers write the property. In Express the two names agree for most methods.
- **Calls from and to the methods.** Each method's body is walked as its own, so calls inside `res.json` belong to
  `res.json`. `this.send()` binds to the owner's `send`: the existing own-receiver rule now applies, because the
  method has an owner. `res.send()` binds when `res` is the module binding and no parameter or local of the caller
  shadows it. `Foo.create()` binds to `Foo.create` through the same rule, which is checked before the
  upper-case-receiver defer. Any other in-file call that would bind to such a method is refused and left to the
  cross-file passes. Examples are a bare `send(path)` (in Express, the `send` package) and `app.render()` on another
  object (which used to make `res.render` call itself). Such a method takes no name in the file's bare-name map, so
  a module function of the same name keeps it whatever the order of the two (see Review round). Exports methods keep
  their old binding.
- **Nested functions.** A `variable_declarator` whose value is a function, in any function body, is a nested symbol
  `<parent>_<name>` with label `name()`. One inside an anonymous callback belongs to the nearest named function. It
  has a `contains` edge from the parent, and its own body and calls, the same shape nested `function` declarations
  already had. The scan now also runs on the bodies of `exports.x`, `Foo.prototype.x` and the new object methods.
- **Scope of nested names.** JS/TS nested symbols are recorded in `scope_parents` / `lexical_nids_by_scope`, as the
  Python extractor does. A bare call binds to the nested function visible from the caller: useAuth's `logout` inside
  `useAuth`, the module's `logout` elsewhere. A bare call from outside its scope does not reach a nested function,
  in the file or (since the review round) from another file, and a member call never does: `controller.abort()`
  inside a nested `const abort` is not a self-call. A nested name no longer overwrites a module-level one in the
  file's name map. A nested function passed by value from elsewhere in the same file can still be bound to it (see
  Limits).
- **Closure capture.** A nested function's locals include every non-function name bound in the enclosing body
  (declarators, parameters, `catch` and `for ... in/of` bindings). So `use(config)` in it names the enclosing
  parameter, not a module function `config`. This over-approximates, so it can only drop an indirect edge.
- **Dead view (`verinoda/deadcode.py`, not vendored).** A unit that a dynamic use keeps alive (weak) now also keeps
  alive what its folded members reach. Before, an unreached object or class counted its methods under itself. Code
  that only one of those methods called was then a `callers_unreached` claim with no dynamic use, so it was
  `strong_inference`. With the new methods, Express's `sendfile` (called only from `res.sendFile`, folded under
  `res`) became such a strong claim. The same was already true for a Python class loaded by name.
- **Vendored code.** The extractor changes are in `engine.py`; the review round adds the cross-file index in
  `extract.py` and the marker lists in `cli.py` and `watch.py`. All are marked "Verinoda patch" and listed in
  docs/UPSTREAM.md ("Modified"). `cache._AST_CACHE_SCHEMA` goes from 8 to 10.
- **Review round.** A review found that the first version overstated how isolated the new symbols were:
  - In the file, a method `res.send` took the bare name `send` from a module function `send` defined before it.
    The new refusal then dropped `other() -> send()` with nothing to fall back to, and a `send` passed by value
    went the same way. A method assigned to a module object now takes no name in the file's bare-name map.
  - Across files, the shared call pass of `extract()` matched bare calls and names passed by value against every
    node label, including the new nested functions and methods. So `Form() -> reset()` bound to a hook's nested
    `reset` when `reset` was the caller's own local, and in axios `stopUnixServer() -> done()` (a Promise
    executor's parameter) and `factory() -> unsubscribe()` (a local `const`) were false edges. In express,
    `.path()` and `.use()` of `app` were targets of false `indirect_call` edges from a parameter and a local. The
    engine now marks JS/TS nested functions, methods assigned to a module object and `Foo.prototype.bar` methods
    with `_no_bare_name`, and the pass leaves them out of its bare-name and by-value index. None of them can be
    imported under its bare name. Member calls reach the methods through the member resolvers, which do not read
    that index. Prototype methods had the same flaw before this change. In express, application.js's
    `resolve('views')` (node:path's `resolve`) bound to `View.prototype.resolve` once the call was inside the new
    method `app.defaultConfiguration`.
  - The extraction diff measured `extract_js` file by file, so it never saw these edges. The cross-file delta is now
    measured with `extract()` over each whole corpus (see Measured).
  - The marker is persisted for unchanged files on incremental builds (`cli.py`, `watch.py`), as `_callable` is.

## Measured

Benchmark (`benchmarks/realworld/run.py`) on private copies of the two pinned clones. The copies have the same sha,
were copied from `C:/vbench` without `.verinoda/`, and `--work` pointed at them, because other runs were using the
shared clones at the same time. Before: the code at f4ca326 (`--verinoda-root` on a `git archive` of it). After:
the committed change. All runs used one machine, one process at a time, with other agents' work running beside
them.

| repository | gold v1 before | gold v1 after | gold v2 before / after | crashes, timeouts | clean after |
|---|---:|---:|---:|---|---|
| expressjs/express v5.2.1 | 4/10 | 7/10 | 2/2 / 2/2 | 0, 0 | yes |
| fastapi/full-stack-fastapi-template 0.12.0 | 7/10 | 8/10 | - | 0, 0 | yes |

- New hits: express `json-calls-stringify`, `json-calls-send` and `sendfile-calls-helper`; template
  `frontend-login-calls-sdk`. No hit was lost. The express scenario `trace lib/response.js::json stringify` went
  from `unresolved` (exit 2) to `found` (exit 0).
- Three earlier "after" runs during the work gave the same gold.
- `render-reaches-tryrender` still misses: "source not resolved" before, "no directed path" after (see Limits).
- Wall times varied with the load from the other runs: express `analyze` median 3.3 s before, and 7.3 s, 2.9 s and
  3.1 s in after runs with the same output size. No time difference is claimed.
- `verinoda scan` of express: 441 nodes and 701 edges before, 497 and 826 after. Template: 1448 and 3395 before,
  1488 and 3468 after.
- `map --view dead` (a scan, then the view, on the same copies):
  - express before: 66 code symbols, 18 dead, 0 strong, 36 weak.
  - express after: 122 code symbols, 31 dead, 0 strong, 49 weak. The new methods of the module objects `res` and
    `req` are counted under them. Methods of `app` that nothing calls in the repository are weak claims, kept weak
    by name mentions.
  - Template: 630 -> 669 code symbols; dead 188, strong 17 and weak 194 both before and after, with the same strong
    claims.
  - With the extractor change but without the dead-view change, express had 1 strong claim: `sendfile`.

Extraction diff (`extract_js` per file, before vs after) on 494 JS/TS files: the private copies of express, axios
and the template's frontend, plus `tests_upstream/fixtures`:

- Nodes 2611 -> 2754 (+144, -1).
- Edges 5137 -> 5357. Added 238: 92 `calls`, 88 `contains`, 56 `method`, 2 `indirect_call`. Removed 18: 17 `calls`,
  1 `contains`.
- Express: +50 `method` edges (46 in lib/: response.js 22, application.js 16, request.js 8; 4 in examples), +31
  `calls`, nothing removed.
- Axios: +48 nodes (6 methods, the rest nested functions), +50 / -16 `calls`.
- Template frontend: +40 nested functions, +11 / -1 `calls`.
- `tests_upstream/fixtures`: no change.
- Every removed edge was read. Each removed `calls` edge is now made by the nested function that holds the call:
  `toJSONObject -> isObject` became `toJSONObject.visit -> isObject`, with `toJSONObject -> visit`. The removed node
  is axios `validator.js` `formatMessage`, which is now nested in the method `validators.transitional`.
- Extraction time over the 494 files, in two runs: 18.8 s before and 15.3 s after, then 23.8 s before and 17.7 s
  after. No slowdown.
- The review round leaves this per-file output unchanged: `extract_js` over 489 files (the three corpora and
  `tests_upstream/fixtures`) gives the same 2312 nodes and 4886 edges before and after the round.

Cross-file delta (review round): `extract()` over each whole corpus, so the shared cross-file pass is included. The
corpora are the JS/TS files of the express, axios and template-frontend clones at their pinned shas, without
`node_modules`, `dist`, `.min.js` and `.d.ts`. The table counts `calls` and `indirect_call` edges whose two ends are
in different files: f4ca326 (before), the first version of this change, and the commit after the review round.

| corpus | files | nodes before / after | cross-file, first version vs before | cross-file, after review vs before |
|---|---:|---:|---|---|
| express | 142 | 278 / 334 | +11 / -6 | +7 / -2 |
| axios | 238 | 1204 / 1251 | +10 / -2 | +6 / -2 |
| template frontend | 100 | 636 / 676 | +26 / -4 | +12 / -4 |

- Every edge of the last column was read.
  - Express added: 6 calls from the new methods to `lib/utils.js` helpers they import (`.set() -> compileETag`,
    `compileQueryParser`, `compileTrust`; `.format() -> normalizeType`, `normalizeTypes`; `.send() -> setCharset`),
    all right. One `indirect_call` moved from the file node of examples/view-locals/user.js to its new method
    `User.all`: `users` there is user.js's own variable, not index.js's `users()`. It was a false edge before and
    still is.
  - Express removed: that same edge's old form, and `examples/resource/index.js -> format()` in
    content-negotiation (a local `format`), which was false.
  - Axios added: 6 calls that moved from an outer function to the nested function or method that makes them
    (`onabort -> CanceledError`, `trackRequestStream -> trackStream`, ...). Removed: the outer form of one of them,
    and a false `parseParameter -> start()` into a smoke test.
  - Template added: 12 calls in the generated client that moved to nested functions (`beforeRequest ->
    mergeHeaders`, `querySerializer -> serializeArrayParam`, ...). Removed: 4 member calls on SDK services
    (`DeleteUser() -> .deleteUser()`, `useAuth() -> .loginAccessToken()`, ...). They are now made inside nested
    functions, and plain `extract()` does not bind them from there. `verinoda scan` does: the gold fact
    `frontend-login-calls-sdk` is a hit.
- What the review round removed against the first version: express `.path()` and `.use()` targets (a parameter and
  a local) and `req.signedCookies.js -> .cookie()` (a local `cookie`), all false; `.defaultConfiguration() -> View
  .resolve()` (it is node:path's `resolve`), false; axios `stopUnixServer -> done()` and `factory -> unsubscribe()`,
  false; template one `indirect_call` to client.gen.ts's nested `request` (a test fixture's parameter), false. Also
  gone: two calls from axios throttle.test.js to throttle.js's nested `throttled` and `flush`, and 13 template calls
  into the hooks' nested `showSuccessToast` and `logout`. These name the right function only because the caller
  destructures it from the returned value (`const [throttled, flush] = throttle(...)`,
  `const { logout } = useAuth()`); the pass has no evidence of that, so they are not kept (see Limits).
- Express also has 4 `indirect_call` edges from tests to examples/view-locals/index.js `count()`, where `count` is a
  test's local in an anonymous callback. They exist at f4ca326 too. The first version hid them by accident: the new
  method `User.count` made the name ambiguous.

Benchmark rerun (review round), on the shared clones with `benchmarks/realworld/run.py --repos <name>`, one at a
time: express gold v1 7/10 and v2 2/2, the template 8/10, axios 7/10 (the same 7 as the 2026-10-02 run). No
crashes or timeouts, clean after each.

## Limits

- Member calls from another file reach the new methods only where a member resolver knows the receiver's type.
  `res.json(...)` in an app, a test or an Express example is called on a parameter `res` of no known type.
  `render-reaches-tryrender` misses for the same reason: `res.render` calls `app.render` on `this.req.app`, a local
  of no known type. Bare calls and names passed by value from another file never reach them, nor a nested function
  or a prototype method (the `_no_bare_name` marker).
- A function a hook or factory returns and the caller destructures (`const { logout } = useAuth()`,
  `const [throttled, flush] = throttle(...)`) is a nested function, so the caller's `logout()` gets no edge. The
  first version bound these by name alone; they were right in the template but by the same rule that gave the
  false edges.
- In the same file, a nested function passed by value (`run(reset)`) from outside its scope can still be bound to
  it when no module-level function has that name: the by-value lookup does not walk scopes. The nested-scope walk
  for bare calls also ignores a non-function local of an inner scope that shadows a nested function's name (Python
  has the same gap).
- Comma and other sequence forms (`var q = {}; q.z = function () {}, q.w = function () {};`, common in minified
  code) make no symbol: a sequence expression is not walked.
- A test's local in an anonymous callback does not shadow a name passed by value from that callback (express tests
  passing `count` bind to examples/view-locals `count()`); this is older than this change.
- Only module-level assignments whose receiver the file binds at module level make a symbol. `$.fn.x = fn`,
  `a.b.c = fn`, `app[method] = fn` (Express's `methods.forEach`), `window.x = fn` and an assignment to a parameter
  inside a function make none.
- The function's own name (`function contentType` in `res.type = ...`) is not a name or alias of the symbol.
- `Foo.x = fn` and `Foo.prototype.x = fn` with the same `x` share one id.
- `this.set()` in `app.init` makes no edge to `app.set`. A member call named like a builtin global of any language
  (`set`) is not bound in the file; this is an older rule, kept here.
- A module-level `res.m()` binds to the module's `res`, even where the code means another object of the same name in
  a closure that does not bind `res` itself.
- A nested function passed by value in an object (`useMutation({ mutationFn: login })`) gets no edge from the
  enclosing function. Only `contains` links `useAuth` to `login`.
- Two nested functions with the same name in sibling callbacks of one function share one id.
- The closure capture is an over-approximation: any name bound anywhere in the enclosing body hides an indirect
  edge to a same-named function.

## Upgrading note

The AST cache schema is now 12 (10 on the branch; merged after the id and definition-line fixes), so the first `verinoda scan` or `verinoda update` after upgrading extracts every
file again.

JavaScript and TypeScript graphs gain symbols:

- methods assigned to a module-level object (`res.json`, `app.render`; label `.json()`, owner `res`), alias chains
  included;
- functions bound inside functions (`useAuth`'s `login`).

Calls made inside those functions now start from them. So `q`, `trace`, impact and `map` answers that named the
outer function for such a call now name the inner one. A selector like `lib/response.js::sendFile` now resolves to
the method instead of a same-named function that differs only in case. In the dead view, code reached only through
a member of a weak unit is weak as well (`via`).

A bare call or a name passed by value in one file no longer binds to a nested function, an object-assigned method
or a `Foo.prototype` method of another file. Some cross-file edges therefore go away, including right ones
to a function a hook returns and the caller destructures (`const { logout } = useAuth()`).

## Tests

- `tests/test_graph_precision.py`: four new tests on fixtures written to `tmp_path`. The fixtures are an
  Express-shaped response.js and application.js (with an alias chain), a TS hook with nested functions, and a file
  with a nested `abort`. The tests are `test_functions_assigned_to_a_module_object_are_its_methods`,
  `test_calls_from_and_to_assigned_methods_resolve`, `test_a_function_bound_inside_a_function_is_its_own_symbol` and
  `test_a_nested_function_is_not_a_property_and_sees_the_enclosing_locals`. All four fail on f4ca326.
- `tests/test_deadcode.py`: `test_what_a_kept_alive_class_reaches_through_its_members_is_weak_too` (a Python class
  named in a string, whose method alone calls a helper). It fails on f4ca326 (the helper is `strong_inference`).
- Review round, `tests/test_graph_precision.py`: four more tests, each failing on the first version of this change
  (dd46469):
  - `test_a_later_assigned_method_does_not_hide_a_module_function_of_the_same_name` (`function send` before
    `res.send = function send2`: the bare call and the name passed by value bind to the module function);
  - `test_nested_functions_are_not_bound_from_another_file_by_a_bare_name` (hooks.ts / Form.ts);
  - `test_assigned_methods_are_not_bound_from_another_file_by_a_bare_name` (lib.js / main.js, with a
    `View.prototype.resolve` and a bare `resolve()`);
  - `test_scoped_js_symbols_carry_the_no_bare_name_marker`.
- Run: `tests/test_graph_precision.py`, `tests/test_deadcode.py` and `tests/test_docs.py` together: 58 passed (23,
  16 and 19).
