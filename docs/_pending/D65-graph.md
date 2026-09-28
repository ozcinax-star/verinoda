# D65 - A graph with fewer false calls, and the tests of more ecosystems (pending doc text)

Branch of the night study "indexing / performance limits and language coverage" (report section 4, items 1,
3, 4, 5 and 10a). To merge into docs/DESIGN.md (a new section and one table row) and docs/UPGRADING.md. The
local changes to the vendored extractor are already listed in docs/UPSTREAM.md ("Modified").

## DESIGN section

## NN. A graph with fewer false calls, and the tests of more ecosystems (D65, 2026-09-28)

### NN.1 Why

Measuring the graph on eight public repositories (Go, Rust, C#, Ruby, PHP, C, C++, TypeScript) and on a large
Python web framework showed calls edges that do not exist, labelled `EXTRACTED`, and noise that crowds out
the code a question is about:

- A member call bound to whatever function of the same name its file defines. Python `super().__delattr__()`
  inside `__delattr__` became a self-loop (828 of the framework's 944 self-loop calls edges, 825 of them
  `EXTRACTED`), or an edge to another class's `__delattr__` of the file. Go `b.Bind(...)` (a parameter of an
  interface type from another package) became `Context.ShouldBindWith -> Context.Bind`, and with it a false
  cycle. Rust `builder.build_parallel().run()` bound to the file's free `run`, `Command::new()` to the file's
  own `new`; PHP `$this->middlewareDispatcher->handle()` became `App::handle -> App::handle`. analyze printed
  these as context (`called by: __delattr__ (...:111); __delattr__ (...:291)`, both false).
- Vendored, minified and generated code was extracted like product code: three minified chart libraries were
  31 % of the nodes and 69 % of the calls edges of a Ruby job-queue repository; flex/bison output added 188
  call edges to a C repository.
- A C# test project (`src/UnitTests/`, `src/IntegrationTests/`, `*.Tests/`) was product code to
  `testcode.is_test_file`: 365 files of a 591-file C# repository, whose tests then led a ranking where the
  product file belonged (study question A1). PHPUnit `FooTest.php` outside `tests/`, GoogleTest `x_test.cc`,
  XCTest, Dart and Elixir tests were missed the same way.
- The C# type-reference pass searched every node for each unresolved reference: 44 % of C# extraction in a
  profile.
- `.hh .hxx .ipp .inl .tpp` were in neither the detection nor the extractor table.

### NN.2 Decisions

- **Member calls bind in the file only through the method's own receiver** (`project_index/extractors/
  engine.py`, `go.py`, `rust.py`; local changes to the vendored extractor, docs/UPSTREAM.md):
  - Python `super().m()` binds to `m` of the enclosing class's bases that the file defines (depth first, left to
    right; a base the file does not define ends the search), otherwise it stays in `raw_calls`, where no pass
    binds it by name. `self.m()` / `cls.m()` take the own class's `m`, then an in-file base's, and only then
    the file-wide name as before.
  - JS/TS, PHP and Ruby: a member call binds in the file only on `this` / `$this` / `self` (the own class first),
    or when the receiver names an object the file defines with that method (`const api = {}; api.load = ...;
    api.load()`). Any other receiver (`other.match()`, `$this->dispatcher->handle()`, `capsule.fetcher.x`,
    `super.m()`) is deferred to the receiver-typed resolvers (Ruby `x = Foo.new`, TypeScript typed parameters
    and imports), which still bind it when they can.
  - Go: a selector call binds to a method of the receiver's type, where the receiver is the method's own
    receiver or a parameter declared with a type of the package (`func record(h *metricHistory)`); a local,
    a chained receiver or a parameter of another package's type stays in `raw_calls`. A bare `f()` never
    binds to a method.
  - Rust: `self.m()` binds to the impl type's (or trait's) own `m`, else the existing `rust_self_type` path;
    `Self::m()` / `Type::m()` bind only when the file has an impl of that type with `m`; a bare `f()` or
    `module::f()` never binds to a method; any other receiver stays in `raw_calls`.
  - `cache._AST_CACHE_SCHEMA` 6: per-file results of the old rules are not reused.
- **Vendored, minified and generated files are a file node only** (`project_index/extract.py`,
  `vendored_reason`): a directory `vendor/`, `vendors/`, `_vendor/`, `third_party/`, `third-party/`,
  `thirdparty/`, `deps/` or `extern/` below the scan root; a `.min.js` / `.bundle.js` name; more than 300 bytes
  per line on average over the first 64 KiB (at least 4 KiB); or a generator's header comment in the first 40
  lines ("Code generated ... DO NOT EDIT", `@generated`, "A Bison parser", flex, protoc, "auto-generated
  file"). The marker must be in a comment, so a generator that writes the marker in a string is product code.
  The file node carries `vendored: <reason>`; its symbols, edges and raw calls are dropped. The search index
  still reads the file's text. `"index": {"vendored": true}` in `.verinoda/config.json` (or
  `VERINODA_GRAPH_VENDORED=1`) keeps everything.
- **Test files** (`testcode.TEST_FILE_RE`): a folder ending in `Tests` (`UnitTests/`, `AutoMapper.UnitTests/`,
  `MyAppTests/`), `Test/` or `Foo.Test/`, `unit_tests/`, `integration-tests/` ...; `FooTest.php`,
  `FooTests.swift`; `x_test.cc`, `x_unittest.cpp` (also `.c`, `.cxx`); `x_test.dart`, `x_test.exs`.
  `Contests/`, `Latest.php` and `attest.cc` are not tests.
- **C# placeholders by label**: a dict label -> first placeholder, built once and updated on each new stub,
  replaces the scan; the result is the same by construction and by test.
- **C++ suffixes**: `.hh .hxx .ipp .inl .tpp` are C++ in detection, extraction, the C++ member-call resolver,
  spans, anchors, search and the lexicon.

### NN.3 Measured

Graph built with `index.build(force=True)` on fresh copies, base = dd60358, with and without each change.

- **C# placeholders** (591-file C# repository, `extract()` of its 513 `.cs` files, twice each): the pass
  4.15 / 4.27 s -> 0.05 / 0.05 s, `extract()` 15.7 / 15.5 s -> 12.8 / 10.7 s (loaded box). Full graph: 15,139
  nodes, 30,677 edges before and after, 0 nodes and 0 edges different.
- **Member calls** (calls edges whose call site is in a file of the language, before -> after, with the
  vendored switch on so #4 does not mix in; "retargeted" = the same caller and line still has a calls edge, to
  another target; hand-checked random samples of the purely removed edges):

  | repository | calls | removed | retargeted | added | purely removed: sample read by hand |
  |---|---|---|---|---|---|
  | Python web framework (2,888 .py) | 22,844 -> 21,805 | 1,944 | 907 | 905 | 1,037, all at a `super()` call (1,017 `super().m()`, 20 `super(C, x).m()`): the caller itself or another class's `m`; none at `self.`. 483 of the added edges are `super()` calls bound to an in-file base; self-loop calls edges 944 -> 118 |
  | Go web framework (130 files) | 1,346 -> 1,267 | 85 | 8 | 6 | 77; 20 read: 4 true (untyped locals `pairs`, `engine`, `msg`, a test recorder), 1 interface method, 15 false (`c.Request.Context()`, `mw.Close()`, `b.Bind()` ...) |
  | Rust search tool (237 files) | 3,171 -> 1,694 | 1,809 | 636 | 332 | 1,173; 25 read: 3 true (a field and locals of in-file types), 22 false (`Command::new`, `PathBuf::from`, `path.parent()`, builder chains on external types) |
  | PHP micro-framework (145 files) | 551 -> 526 | 25 | 3 | 0 | 22, all `$this->x->m()` delegations bound to the caller's own method: false; self-loops 23 -> 0 |
  | Ruby job queue (.rb of 347 files) | 588 -> 458 | 133 | 11 | 3 | 122; 18 read: 5 true (`tab.quiet!`, `result.job_results[k].add_metric` ...), 13 false (`Array#each` -> `ProfileSet.each`, `config.handle_exception` -> the caller ...) |
  | TS web framework (.ts of 481 files) | 746 -> 743 | 6 | 4 | 3 | 2 (`super.route()` self-loop, a `v.toString()`) |
  | C (432 files), C++ (145 files) | 2,775 -> 2,775; 2,408 -> 2,408 | 0 | | 0 | unchanged |

  Added edges, sampled the same way: 15 of the 483 `super()` edges in the Python framework, 14 right (the base
  that defines the method, past in-file mixins that do not); the 15th was `super(override_settings,
  self).__init__()` in a subclass, bound to `override_settings.__init__` - `super(C, obj)` now searches the bases
  of C (after the measurement; the test covers it). 10 of Rust's 332 added edges: all right (`FormatBuilder::new()`
  to `FormatBuilder.new`, not the file's other `new`s; `self.is_empty()` to the own type). A base written as
  `module.Class` gives no `inherits` edge, so the search passes over it (right in the one case seen,
  `socketserver.ThreadingMixIn` defines no `__init__`, but not in general).

  A first version also deferred every JS/TS receiver other than `this`; on the TS framework it removed 37
  edges of which a sample of 12 had 5-6 true (untyped locals of classes the file defines, a typed parameter the
  TS resolver did not bind), so JS/TS defer `super.m()` only.

  The false edges of the study's probe table are gone (`ShouldBindWith -> Context.Bind` and its cycle,
  `files_parallel -> run`, `App::handle -> App::handle`, the `super().__delattr__` self-loop and the
  cross-class `LazySettings.__delattr__ -> UserSettingsHolder.__delattr__`). The price: true same-file edges
  through an untyped local (`engine := New(); engine.With()`, `let p = ...; p.add_child()`, a Ruby block
  parameter) are gone too, about one in five of the purely removed Go edges, one in eight in Rust and one in
  four in Ruby by the samples; typing Go locals from constructors and Rust `let x = Type::new()` would bring
  them back. The graph does not report them as unknown yet: analyze's callers answer says "no call edge".
- **Vendored / minified / generated** (graph before -> after, the member-call change in both):

  | repository | nodes | edges | calls | files reduced to a node |
  |---|---|---|---|---|
  | Ruby job queue | 2,851 -> 1,981 | 4,468 -> 2,413 | 1,628 -> 507 | 3 minified chart libraries, a generated `db/schema.rb` |
  | C JSON processor | 1,353 -> 903 | 5,465 -> 3,864 | | `vendor/decNumber/` (31 files), flex/bison `lexer.c/h`, `parser.c/h` |
  | Python web framework | 47,392 -> 46,937 | 102,969 -> 102,186 | 22,127 -> 21,846 | 68 files under `admin/static/.../vendor/` (jQuery, select2, XRegExp) |
  | Go web framework | 2,017 -> 1,980 | 4,761 -> 4,710 | 1,267 -> 1,260 | a protoc-generated `test.pb.go` |
  | Rust, PHP, TS, C++ | unchanged | | | none |
- **Test files** recognised by `is_test_file`: C# repository 61 -> 421 of 514 code files (+360: `src/UnitTests`
  325, `src/IntegrationTests` 37, `src/AutoMapper.DI.Tests` 3); the C repository +1 (`src/jq_test.c`, its test
  runner); the other seven repositories unchanged.

## DESIGN decision table row

| D65 | A graph with fewer false calls, and the tests of more ecosystems | implemented | Built 2026-09-28 (section NN): member calls bind in the file only through the method's own receiver (Python `super()` to an in-file base; Go, Rust, PHP, Ruby, JS/TS); vendored, minified and generated files are a file node only (`index.vendored` keeps them); .NET, Xcode, PHPUnit, GoogleTest, Dart and Elixir tests are test files; the C# type-reference pass is linear; `.hh .hxx .ipp .inl .tpp` are C++. |

## UPGRADING note

- The graph changes on the next `verinoda update` (the extraction stamp and the AST cache schema changed, so the
  whole graph is rebuilt once, unchanged files included): fewer calls edges in Go, Rust, PHP, Ruby, JS/TS and
  Python (`super()`) code - a callers or trace answer that listed a same-named method of another object no
  longer does, and some true same-file calls through an untyped local are no longer in the graph (analyze says
  `unknown` for them instead of a wrong caller). Vendored (`vendor/`, `third_party/`, `deps/`, `extern/` ...),
  minified and generated files keep only their file node: their functions no longer appear in the map, the
  callers views or the clusters, while search still reads their text. To keep them, set
  `"index": {"vendored": true}` in `.verinoda/config.json` and run `verinoda scan . --force`.
- Test files: .NET test projects (`UnitTests/`, `Foo.Tests/`), Xcode test targets (`MyAppTests/`), PHPUnit
  `FooTest.php`, GoogleTest `x_test.cc`, Dart and Elixir `x_test.*` now count as tests: they rank lower in
  search, and the tests view, impact's `tests_to_run` and the change review list them as tests.
- `.hh .hxx .ipp .inl .tpp` files enter the graph as C++ on the next `verinoda update`.
