## Why

The real-world run of 2026-10-02 (benchmarks/results/realworld-2026-10-02/defects.md, D5 and D6) found method
calls that the vendored extractor (`verinoda/project_index/`) left without an edge, or bound to the wrong node:

- PHP `Utils::chooseHandler()` and `Middleware::redirect()` (guzzle src/HandlerStack.php:58, :60) made INFERRED
  `calls` edges to the classes `Utils` and `Middleware`, not to the methods (src/Utils.php:73,
  src/Middleware.php:213). The engine named a static call by its class. Gold misses:
  `stack-create-chooses-handler`, `stack-create-pushes-redirect`.
- Rust `Controller::new(config, &assets)` (bat src/bin/bat/main.rs:279) made no edge. A `Type::m()` path bound only
  inside its own file, because a bare last-segment match across files had bound unrelated crates (#908). Gold
  miss: `run-controller-new`.
- `controller.run(...)` on a local `let controller = Controller::new(...)` (bat main.rs:283) and
  `printer.print_header(...)` on `printer: &mut dyn Printer` (bat src/controller.rs:222) made no edge. Gold miss:
  `print-file-calls-header`. The scenario `trace run_controller run_with_error_handler` gave "no directed path".
- Go `root.getValue(...)` (gin gin.go:715) made no edge. `root := t[i].root` and `t := engine.trees` go through a
  field, an element of the slice type `methodTrees`, and the field `root *node`, which are declared in tree.go. The
  D65 receiver typing read only the caller's own file and only plain names and fields. Gold misses:
  `request-reaches-tree@v2` and `get-registers-route` (`group.engine.addRoute`, then `root.addRoute` on
  `root := engine.trees.get(method)`).
- JS `dispatchRequest.call(this, newConfig)` and `dispatchRequest.bind(this)` (axios lib/core/Axios.js:229, :242,
  :195) were member calls of a method named `call` or `bind`, so `_request` never reached `dispatchRequest`.

## Decisions

- **Extend, not a parallel resolver.** Each language's existing D65 receiver machinery types the receiver in the
  caller's file: Go's local type table (`extractors/go.py`), Rust's `methods_by_owner` and the `self.m()` raw call
  (`extractors/rust.py`), PHP's receiver class tables and `_class_method` (`extractors/engine.py`), and the JS/TS
  call binding. What one file cannot decide goes to a cross-file pass in the existing resolver registry
  (`run_language_resolvers`), next to `rust_self_member_calls`: `go_receiver_calls`, `rust_typed_member_calls`,
  `php_static_calls`. The Python receiver sidecar (`verinoda/index.py`, `_visible_class`) is not changed.
- **Only a type the source states.** Every rule binds only when the type can be read from declarations: a
  parameter or variable type, a struct or composite literal, a constructor whose declaration returns the type, a
  field declaration, a declared method result, a written path or an import. A name bound twice in one function with
  different types has no type. That covers shadowing, and in Go a `:=` in an inner block. A name bound by a pattern,
  a closure parameter, a `for`, `match` or `if let`, a type switch or a select receive has no type either. Generic
  type parameters type nothing. Anything else makes no edge: precision over recall.
- **Interfaces and traits.** A Go value of an interface type, a Rust `dyn Trait`, `impl Trait`, `Box/Rc/Arc<dyn
  Trait>` binds to the interface's or trait's own declaration of the method, as a lead to its implementations. It
  never binds to one implementation, even when there is only one. These edges are INFERRED with score 0.75.
- **Confidence.** An edge whose target comes from a receiver's inferred type is INFERRED with score 0.85: Rust
  locals and parameters, Go chains through an element, a method result or another file. Two kinds stay EXTRACTED,
  as before: Go in-file receivers of the forms D65 already read (a stated type, a constructor result, a package
  variable, a field), and the explicit forms `Type::m()` (Rust, imported or written with its path) and
  `Class::m()` (PHP, by FQN). A Rust `Type::m()` whose name only a glob import (`use super::*`) supplies is
  INFERRED (0.85). So is a PHP static method found up an `extends` chain.
- **Go: receiver chains.** A receiver is a chain: a start (a stated type, the i-th result of a package function or
  a conversion, a package-level name of another file) and steps (`.field`, `[i]` of a slice, array or map,
  `.method()` result). Locals are recorded as chains, including `x := y.f`, `y[i]`, `y.m()`, `x.(T)`,
  `a, b := f()`, a range value, `new(T)`, named results and variadic parameters. The file's facts (`_go_type_facts`:
  struct fields and embedded types, slice/map underlying types, aliases, interface methods, function and method
  results, package variables) evaluate the chain in the file. The package pass evaluates what the file could not:
  `resolve_go_receiver_calls` reads every `.go` file of the caller's directory with the same package clause from
  disk. It does not read them from the node list, because on an incremental build the unchanged files reach the
  passes as nodes without lines or types. Two files that state a name differently (build-tagged twins) state
  nothing for it. A promoted method of an embedded struct binds as D65 did: one answer at the shallowest depth,
  none when an embedded type of another package comes first. A member call on a receiver of known type is no
  longer dropped for sharing a name with some language's builtin, so gin's own `c.JSON()`, `c.Error()` and
  `c.Set()` now bind.
- **Rust: `Type::m()` across files.** A path call whose type is not in the caller's file binds when the corpus
  declares exactly one struct, enum or trait of that name and the name comes from this crate's source. The roots
  are the first segment of the written path or of the `use` that imports it: `crate`/`self`/`super`, a crate of the
  corpus (`[package]`/`[lib] name` of the nearest Cargo.toml, `-` as `_`), or a module file of it. A `use` inside a
  function body counts. So `bat::controller::Controller` binds, and `std::process::Command::new()` never binds to a
  `Command` the project declares. `Self::m()` of an impl block in another file binds as `self.m()` does (the
  existing pass). `let x = T::f(..)` types `x` only when `f`'s declaration returns `Self` or `T`. The new
  `_rust_returns_self` node marker records that. `T::f(..)?` and `Option<Self>` returns type nothing.
- **PHP: `Foo::m()` is a call of Foo's `m`.** `self::`/`static::` bind to the own class (then its in-file bases),
  as `$this->` does; `parent::` binds to a base's method the file defines. Another file's class is found by its FQN:
  the engine resolves the written name with the file's namespace and `use` imports (`_php_namespace_facts`), and
  each class node carries its own FQN (the new `_php_fqn` marker). So `use GuzzleHttp\Psr7\Utils;
  Utils::streamFor()` in a test no longer binds to the project's `GuzzleHttp\Utils`. When the class is in the corpus
  but no method of that name is (an external base), the call links the class as before (INFERRED).
- **JS/TS `fn.call(this, ...)`, `fn.apply(this, args)` are calls of `fn`.** They bind as a bare `fn()` would (in
  file, nested scopes, then the cross-file pass with its import gate). `fn.bind(this)` hands the function on and
  becomes an `indirect_call`, as a callback passed by name does. Nothing binds when `fn` is a parameter or local of
  the caller, or an import from outside the corpus.
- **Incremental builds.** `_rust_returns_self` and `_php_fqn` are added to the persisted marker lists of `cli.py`
  and `watch.py`, as `_rust_impl_key` is. An incremental rebuild of the callers keeps every receiver edge
  (test below).
- **Vendored code and cache.** All changes in `verinoda/project_index/` are marked "Verinoda patch" (or extend an
  existing "Local change (Verinoda)" block) and listed in docs/UPSTREAM.md ("Modified"). `cache._AST_CACHE_SCHEMA`
  goes from 12 to 13.
- **Tests whose expectation changed.** `tests/test_graph_precision.py`: gin's `b.Bind()` on `b Binding` (an
  interface) now binds to `Binding.Bind`, still never to `Context.Bind`. `tests_upstream/test_php_object_creation.py`:
  `Baz::create()` reaches `.create()`, not the class. `tests_upstream/test_rust_self_member_calls.py`:
  `Self::fetch_value(self)` across impl files now binds (the old test documented that it did not).

## Measured

Benchmark `benchmarks/realworld/run.py --repos <name>` on the shared clones, one repository at a time, on one machine
with other agents' work running beside it. Before is a96f60e (the base of this branch), run from a `git archive` of it
with `--verinoda-root`; the gold numbers equal those of benchmarks/results/realworld-2026-10-02/summary.md. After is
the committed change.

| repository | gold v1 before | gold v1 after | gold v2 before / after | crashes, timeouts | clean after |
|---|---:|---:|---|---|---|
| guzzle/guzzle 8.2.0 | 8/10 | 10/10 | - | 0, 0 | yes |
| sharkdp/bat v0.26.1 | 8/10 | 10/10 | - | 0, 0 | yes |
| gin-gonic/gin v1.12.0 | 7/10 | 8/10 | 1/2 / 2/2 | 0, 0 | yes |
| axios/axios v1.20.0 | 9/10 | 9/10 | - | 0, 0 | yes |

- New hits:
  - guzzle: `stack-create-chooses-handler`, `stack-create-pushes-redirect`.
  - bat: `run-controller-new`, `print-file-calls-header` (to the trait's declaration, src/printer.rs:72).
  - gin: `get-registers-route` and `request-reaches-tree@v2`.
- No hit was lost.
- The bat scenario `trace run_controller run_with_error_handler` went from "no directed path" (exit 2) to found.
- gin's two v1 misses are the gold's own (their v2 entries hit).
- axios was 9/10 already at a96f60e. The 2026-10-02 run had 7/10 because of D1, which D167 fixed.
- axios `request-reaches-adapter` still misses. `_request -> dispatchRequest` now exists (Axios.js:229 and :242 by
  `.call`, :195 by `.bind` as an `indirect_call`), but `dispatchRequest -> adapters.getAdapter` is a member call on
  a default-imported object literal (see Limits).
- fzf (Go) and express (JS) were rerun as well, because the change reaches their languages: fzf 8/10 and v2 3/3,
  express 7/10 and v2 2/2. These are the numbers before the change.
- Scan times, before and then after, in seconds: guzzle 25.0 and 23.2; bat 35.6 and 36.1; gin 11.2 and 16.0;
  axios 24.9 and 24.3. The gin scan took 12.0 s in an earlier run of the change, so the 16.0 s is load from the
  other agents, not a slowdown. No time difference is claimed.

Edge delta: `extract()` over each whole corpus, before (a96f60e) and after. The corpora are copies of the pinned
clones without `.git`, `vendor`, `node_modules`, `dist`, `build`, `target`, `.min.js` and `.d.ts`. Nodes are
unchanged on every corpus, and only `calls` / `indirect_call` edges change.

| corpus | files | edges before / after | added | removed | sample of added read | right |
|---|---:|---:|---:|---:|---:|---:|
| guzzle (PHP) | 137 | 6915 / 8359 | 2022 | 578 | 20 | 20 |
| bat (Rust) | 63 | 2741 / 2829 | 88 | 0 | 20 | 20 |
| gin (Go) | 98 | 4462 / 5225 | 763 | 0 | 20 | 20 |
| axios (JS/TS) | 235 | 2587 / 2592 | 5 | 0 | 5 (all) | 5 |
| fzf (Go) | 89 | 3806 / 4071 | 265 | 0 | 20 | 20 |
| express (JS) | 142 | 761 / 762 | 1 | 0 | 1 (all) | 1 |

- **guzzle.** Added: 1295 in-file `self::`/`static::` calls, EXTRACTED as `$this->` calls are (before, `self::m()`
  made no edge), and 727 cross-file `Class::m()` calls, EXTRACTED by FQN. Removed: 578 edges to classes. 573 are
  replaced by the edge to the method of that class from the same caller. The other 5 were false: tests that
  import `GuzzleHttp\Psr7\Utils` were bound to `GuzzleHttp\Utils`.
- **bat.** 47 `Type::m()` across files (EXTRACTED), 35 calls on a local or parameter of a stated type (INFERRED
  0.85; 14 of them in-file), and 6 trait-method leads (INFERRED 0.75): `Printer.print_header`, `print_footer`,
  `print_line`, `print_snip` from `printer: &mut dyn Printer`, and `ColorSchemeDetector.should_detect` / `detect`.
- **gin.** 39 edges in product code and 724 in tests (test_helpers.go counted as a test): package-level chains (`engine.trees.get(m)` ->
  `methodTrees.get`, `root.addRoute`, `root.getValue`, `c.engine.isTrustedProxy`, `c.Params.ByName`,
  `c.AbortWithError(..).SetType`), receivers of builder results in tests (`router := New(); router.GET(..)` ->
  `RouterGroup.GET`, promoted through the embedded `RouterGroup`), interface leads (`c.Writer.WriteHeaderNow` ->
  `ResponseWriter.WriteHeaderNow`, `Validator.ValidateStruct` -> `StructValidator.ValidateStruct`), and 8 in-file
  own-receiver calls once dropped as "builtin" names (`c.JSON`, `c.Error`, `c.Set`).
- **axios and express.** axios: `_request -> dispatchRequest` (`.call`), `dispatchRequest -> transformData`
  (`.call`), the `.bind` `indirect_call`, and a test's `record.call(this, ...)` twice. express:
  `app.handle -> logerror` (`logerror.bind(this)`).
- **Samples.** A seeded random sample of 20 added edges per corpus (all of them for axios and express) was read
  against the source lines. Every one is right: a call of that method on a value of that type, or of the
  interface's or trait's declaration for a trait object or interface value.
- **Extraction time.** In one pass each, before and then after, in seconds: guzzle 11.0 and 10.9; bat 2.4 and 2.6;
  gin 4.0 and 4.5; axios 12.9 and 13.0. Gin's package pass parses each Go package with unresolved chains once.

## Limits

- Rust: a field's type (`self.printer.print()`), a method's result (`x.f().g()`), `T::f(..)?`, `Option<Self>`
  returns, closures and tuple patterns type nothing. A type declared twice in the corpus (two crates' `Config`)
  never binds across files. A module-file root is accepted by name: an external crate named like a module file of
  the project (`log` and `src/log.rs`) counts as the project's.
- Go: a chain that leaves the package (`pkg.NewT()`, a field of another package's type) types nothing. Generic
  instantiations (`NewT[int]()`), function-typed fields, method values, channels and an interface embedded in a
  struct type nothing. Locals are per function, not per block, so a name rebound in a sibling block with another
  type loses its type everywhere in the function. The package pass reads the files from disk, so it sees a file
  of the directory that the scan excluded (a `.verinodaignore`d sibling).
- PHP: only static calls and `$this->` / typed receivers in the file (D65) bind. `$x->m()` on a parameter typed with
  another file's class, `parent::m()` of a base in another file, and `$cls::m()` make no edge. A file with two
  namespaces is read by short class name only, and binds only when the corpus has one class of that name.
- JS/TS: `fn.call/apply/bind` bind only for a plain identifier `fn`. `obj.method.call(...)`,
  `Foo.prototype.bar.call(this)` and `adapters.getAdapter(...)` (a method of an imported object literal) make no
  edge. When a function both binds `fn` and calls `fn.call(...)`, it can have an `indirect_call` and a `calls` edge
  to the same target, depending on which comes first.
- An interface or trait lead names the declaration only. Reaching the implementations is left to the reader (or to
  `implements` / `embeds` edges where they exist).
- The 20-edge samples are small. The edge tables count, but nobody read every added edge.

## Upgrading note

The first scan or update after upgrading re-extracts every code file once (`cache._AST_CACHE_SCHEMA` 12 -> 13).
Graphs gain `calls` edges:
- Go calls on receivers typed through other files of the package (INFERRED 0.85), and to interface method
  declarations (INFERRED 0.75).
- Rust `Type::m()` across files (EXTRACTED; INFERRED through a glob import), calls on locals and parameters of a
  stated type (INFERRED 0.85), and to trait method declarations for trait objects (INFERRED 0.75).
- PHP `self::`/`static::`/`parent::` and `Class::m()` calls to the method.
- JS/TS `fn.call`/`fn.apply` as calls of `fn`, and `fn.bind` as an `indirect_call`.

A PHP `calls` edge from a static call to a class becomes an edge to its method. A query or saved claim that relied
on `X -calls-> Utils` should name the method (`Utils.chooseHandler`). Two new node markers, `_rust_returns_self` and
`_php_fqn`, are stored in graph.json. The tq-audit table (`tests/test_tq_measured.py`) needs a rerun after the merge.

## Tests

- New `tests/test_rw_receivers.py`, 12 tests, each on a small fixture:
  - Go: chains across the files of a package (field, slice element, method result, constructor of another file,
    `var x T`, range value). A same-named type in another directory is another type. An interface receiver binds to
    the interface method only, not to its two implementations. A name shadowed by another type binds nothing.
  - Rust: `Type::m()` through `crate::` and through the crate's own name from Cargo.toml; no edge for
    `std::process::Command` (imported or written). A local from a `Self`-returning constructor and a typed
    parameter bind. Nothing binds when a local is shadowed by another type or comes from a function that does not
    return `Self`. A trait object binds to the trait's declaration only, with two implementations present.
    In-file cases: a generic parameter types nothing, and a name rebound by a struct pattern binds nothing.
  - PHP: FQN binding with a `use` alias to another namespace and a test importing a same-named class;
    `self::` and `parent::`; no edge to the class; an inherited static method is INFERRED.
  - JS: `.call`, `.apply`, a nested function's `.call`, a parameter named like the function, and `.bind` alone as an
    `indirect_call`.
  - An incremental build of the callers keeps the Go, Rust and PHP edges.
- Changed: `tests/test_graph_precision.py` (Go interface expectation), `tests_upstream/test_php_object_creation.py`
  and `tests_upstream/test_rust_self_member_calls.py` (see Decisions).
- Run:
  - `tests/test_rw_receivers.py`, `tests/test_graph_precision.py`, `tests_upstream/test_php_object_creation.py` and
    `tests_upstream/test_rust_self_member_calls.py`: 66 passed (12 of them new).
  - `tests/test_docs.py`: 19 passed.
