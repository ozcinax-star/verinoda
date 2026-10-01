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
  upper-case-receiver defer. Any other call that would bind to such a method is refused and left to the cross-file
  passes. Examples are a bare `send(path)` (in Express, the `send` package) and `app.render()` on another object
  (which used to make `res.render` call itself). Prototype and exports methods keep their old binding.
- **Nested functions.** A `variable_declarator` whose value is a function, in any function body, is a nested symbol
  `<parent>_<name>` with label `name()`. One inside an anonymous callback belongs to the nearest named function. It
  has a `contains` edge from the parent, and its own body and calls, the same shape nested `function` declarations
  already had. The scan now also runs on the bodies of `exports.x`, `Foo.prototype.x` and the new object methods.
- **Scope of nested names.** JS/TS nested symbols are recorded in `scope_parents` / `lexical_nids_by_scope`, as the
  Python extractor does. A bare call binds to the nested function visible from the caller: useAuth's `logout` inside
  `useAuth`, the module's `logout` elsewhere. A nested function is never reached from outside its scope, and never
  as a property: `controller.abort()` inside a nested `const abort` is not a self-call. A nested name no longer
  overwrites a module-level one in the file's name map.
- **Closure capture.** A nested function's locals include every non-function name bound in the enclosing body
  (declarators, parameters, `catch` and `for ... in/of` bindings). So `use(config)` in it names the enclosing
  parameter, not a module function `config`. This over-approximates, so it can only drop an indirect edge.
- **Dead view (`verinoda/deadcode.py`, not vendored).** A unit that a dynamic use keeps alive (weak) now also keeps
  alive what its folded members reach. Before, an unreached object or class counted its methods under itself. Code
  that only one of those methods called was then a `callers_unreached` claim with no dynamic use, so it was
  `strong_inference`. With the new methods, Express's `sendfile` (called only from `res.sendFile`, folded under
  `res`) became such a strong claim. The same was already true for a Python class loaded by name.
- **Vendored code.** Every extractor change is in `engine.py`, marked "Verinoda patch" and listed in
  docs/UPSTREAM.md ("Modified"). `cache._AST_CACHE_SCHEMA` goes from 8 to 9.

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

## Limits

- Calls from another file do not reach the new methods. `res.json(...)` in an app, a test or an Express example is
  called on a parameter `res` of no known type. `render-reaches-tryrender` misses for the same reason: `res.render`
  calls `app.render` on `this.req.app`, a local of no known type.
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

The AST cache schema is now 9, so the first `verinoda scan` or `verinoda update` after upgrading extracts every
file again.

JavaScript and TypeScript graphs gain symbols:

- methods assigned to a module-level object (`res.json`, `app.render`; label `.json()`, owner `res`), alias chains
  included;
- functions bound inside functions (`useAuth`'s `login`).

Calls made inside those functions now start from them. So `q`, `trace`, impact and `map` answers that named the
outer function for such a call now name the inner one. A selector like `lib/response.js::sendFile` now resolves to
the method instead of a same-named function that differs only in case. In the dead view, code reached only through
a member of a weak unit is weak as well (`via`).

## Tests

- `tests/test_graph_precision.py`: four new tests on fixtures written to `tmp_path`. The fixtures are an
  Express-shaped response.js and application.js (with an alias chain), a TS hook with nested functions, and a file
  with a nested `abort`. The tests are `test_functions_assigned_to_a_module_object_are_its_methods`,
  `test_calls_from_and_to_assigned_methods_resolve`, `test_a_function_bound_inside_a_function_is_its_own_symbol` and
  `test_a_nested_function_is_not_a_property_and_sees_the_enclosing_locals`. All four fail on f4ca326.
- `tests/test_deadcode.py`: `test_what_a_kept_alive_class_reaches_through_its_members_is_weak_too` (a Python class
  named in a string, whose method alone calls a helper). It fails on f4ca326 (the helper is `strong_inference`).
- Run: `tests/test_graph_precision.py`, `tests/test_deadcode.py` and `tests/test_docs.py` together: 54 passed (19,
  16 and 19).
