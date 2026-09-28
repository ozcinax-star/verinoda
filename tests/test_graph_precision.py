"""Local changes to the vendored graph extractor (verinoda/project_index, docs/UPSTREAM.md "Modified"):
the C# placeholder index, member calls that no longer bind to a same-named function of the file, vendored,
minified and generated files kept to a file node, and more C/C++ header suffixes."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import random  # noqa: E402
from pathlib import Path  # noqa: E402

from verinoda.project_index.extractors import csharp  # noqa: E402


# -- C#: dangling type references reuse the first placeholder of their label -----------------------------------

def _oracle_targets(nodes: list[dict], edges: list[dict]) -> list[str]:
    """What the pass did before the index: a linear search of the growing node list per edge."""
    nodes = [dict(n) for n in nodes]
    ids = {n["id"] for n in nodes}
    out = []
    for edge in edges:
        label = edge["metadata"]["ref_token"]
        cur = next((n for n in nodes if n["id"] == edge["target"]), None)
        if cur is not None and not cur.get("source_file") and cur.get("label") == label:
            out.append(edge["target"])
            continue
        hit = next((n["id"] for n in nodes if n.get("label") == label and not n.get("source_file")), None)
        if hit is None:
            hit = csharp._make_id(label)
            if hit in ids:
                hit = csharp._make_id("csharp_type_ref", label)
                suffix = 2
                while hit in ids:
                    hit = csharp._make_id("csharp_type_ref", label, str(suffix))
                    suffix += 1
            nodes.append({"id": hit, "label": label, "source_file": ""})
            ids.add(hit)
        out.append(hit)
    return out


def _random_corpus(seed: int) -> tuple[list[dict], list[dict]]:
    rng = random.Random(seed)
    labels = [f"IType{i}" for i in range(12)]
    nodes = [{"id": "a_cs", "label": "A.cs", "source_file": "src/A.cs", "file_type": "code"},
             {"id": "a_owner", "label": "Owner", "source_file": "src/A.cs", "file_type": "code"}]
    for i in range(40):
        label = rng.choice(labels)
        placeholder = rng.random() < 0.6
        nodes.append({"id": f"n{i}_{label.lower()}", "label": label, "file_type": "code",
                      "source_file": "" if placeholder else f"lib/{label}.java"})  # real, but not C#: skipped
    edges = []
    for _ in range(80):
        label = rng.choice(labels + ["Missing1", "Missing2"])
        target = rng.choice(nodes[2:])
        if not target.get("source_file") or rng.random() < 0.5:
            edges.append({"source": "a_owner", "target": target["id"], "relation": "references",
                          "source_file": "src/A.cs", "metadata": {"ref_token": label}})
    return nodes, edges


def test_csharp_dangling_references_match_the_linear_search_they_replace():
    for seed in range(25):
        nodes, edges = _random_corpus(seed)
        relevant = [e for e in edges if not next(n for n in nodes if n["id"] == e["target"]).get("source_file")]
        expected = _oracle_targets(nodes, relevant)
        csharp._resolve_csharp_type_references([], [], nodes, edges)
        got = [e["target"] for e in relevant]
        assert got == expected, seed
        # a stub minted for one reference is found again by the next reference of the same label
        for label in ("Missing1", "Missing2"):
            stubs = [n for n in nodes if n.get("label") == label and not n.get("source_file")]
            assert len(stubs) <= 1


# -- member calls bind to the method's own receiver only ---------------------------------------------------------

_MEMBER_FIXTURES = {
    "settings.py": '''
class Base:
    def save(self):
        return 1

class Child(Base):
    def save(self):
        return super().save()

    def reset(self):
        return self.save()

class Holder:
    def __delattr__(self, name):
        super().__delattr__(name)

class Grandchild(Child):
    def save(self):
        return super(Child, self).save()

class Other:
    def save(self):
        return 2
''',
    "context.go": '''package gin

type Binding interface{ Bind(obj any) error }

type Context struct{}

func (c *Context) Bind(obj any) error { return c.MustBindWith(obj) }

func (c *Context) MustBindWith(obj any) error { return nil }

func (c *Context) ShouldBindWith(obj any, b Binding) error { return b.Bind(obj) }
''',
    "main.rs": '''
struct Worker;

impl Worker {
    fn run(&self) -> u8 { self.step() }
    fn step(&self) -> u8 { 1 }
    fn new() -> Worker { Worker }
}

fn run() -> u8 { 0 }

fn files_parallel(builder: Builder) -> u8 {
    let w = Worker::new();
    let v: Vec<u8> = Vec::new();
    builder.build_parallel().run()
}
''',
    "App.php": '''<?php
class App {
    private $middlewareDispatcher;
    public function handle($request) {
        return $this->middlewareDispatcher->handle($request);
    }
    public function run($request) {
        return $this->handle($request);
    }
}
''',
    "processor.rb": '''
class Processor
  def get_one
    work = capsule.fetcher.retrieve_work
    self.process_one
  end

  def retrieve_work
    nil
  end

  def process_one
    nil
  end
end
''',
    "router.ts": '''
export class Router {
  match(path: string) { return this.find(path) }
  find(path: string) { return path }
  dispatch(other: Matcher) { return other.match("x") }
}

export class Special extends Router {
  find(path: string) { return super.find(path) }
}
''',
}


def _call_pairs(tmp_path, fixtures: dict[str, str] | None = None) -> set[tuple[str, str, str]]:
    from verinoda.project_index.extract import extract

    paths = []
    for name, body in (_MEMBER_FIXTURES if fixtures is None else fixtures).items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        paths.append(path)
    out = extract(paths, cache_root=tmp_path / "cache", root=tmp_path, parallel=False)
    by_id = {n["id"]: n for n in out["nodes"]}

    def owner(nid: str) -> str:
        for e in out["edges"]:
            if e["relation"] == "method" and e["target"] == nid:
                return str(by_id.get(e["source"], {}).get("label", ""))
        return ""

    def name(nid: str) -> str:
        label = str(by_id.get(nid, {}).get("label", nid)).strip("()").lstrip(".")
        return f"{owner(nid)}.{label}" if owner(nid) else label

    return {(Path(e["source_file"]).name, name(e["source"]), name(e["target"]))
            for e in out["edges"] if e["relation"] == "calls"}


def test_member_calls_bind_to_the_own_receiver_only(tmp_path):
    pairs = _call_pairs(tmp_path)
    # Python: super() reaches the base defined in the file, never the caller or another class's method
    assert ("settings.py", "Child.save", "Base.save") in pairs
    assert not any(src == tgt for _, src, tgt in pairs), sorted(p for p in pairs if p[1] == p[2])
    assert not any(src == "Holder.__delattr__" for _, src, _ in pairs)
    assert ("settings.py", "Child.reset", "Child.save") in pairs          # self.m(): the own class, not Other
    assert ("settings.py", "Grandchild.save", "Base.save") in pairs      # super(Child, self) skips Child
    assert ("settings.py", "Grandchild.save", "Child.save") not in pairs
    assert not any(tgt == "Other.save" for _, _, tgt in pairs)
    # Go: `b.Bind()` on a parameter is not the method's own Context.Bind; `c.MustBindWith()` is
    assert ("context.go", "Context.Bind", "Context.MustBindWith") in pairs
    assert not any(src == "Context.ShouldBindWith" for f, src, _ in pairs if f == "context.go")
    # Rust: `self.step()` binds; `builder.build_parallel().run()` is not the free `run`; `Vec::new()` is not
    # Worker::new, `Worker::new()` is
    assert ("main.rs", "Worker.run", "Worker.step") in pairs
    assert ("main.rs", "files_parallel", "Worker.new") in pairs
    assert ("main.rs", "files_parallel", "run") not in pairs
    # PHP: `$this->handle()` binds; `$this->middlewareDispatcher->handle()` does not
    assert ("App.php", "App.run", "App.handle") in pairs
    # Ruby: `self.process_one` binds; `capsule.fetcher.retrieve_work` is not this file's retrieve_work
    assert ("processor.rb", "Processor.get_one", "Processor.process_one") in pairs
    assert ("processor.rb", "Processor.get_one", "Processor.retrieve_work") not in pairs
    # TS: `this.find()` binds to the own class; `super.find()` is not the caller itself
    assert ("router.ts", "Router.match", "Router.find") in pairs
    assert not any(src == "Special.find" for f, src, _ in pairs if f == "router.ts")


# -- vendored, minified and generated files are a file node only -------------------------------------------------

def test_vendored_minified_and_generated_files_are_recognised(tmp_path):
    from verinoda.project_index.extract import vendored_reason

    def write(rel: str, text: str) -> Path:
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    plain = "function add(a, b) {\n  return a + b;\n}\n"
    assert vendored_reason(write("vendor/github.com/x/y/y.go", "package y\n"), tmp_path) == "vendored"
    assert vendored_reason(write("src/third_party/zlib/inflate.c", "int f(void) { return 0; }\n"), tmp_path) == "vendored"
    assert vendored_reason(write("web/assets/chart.min.js", plain), tmp_path) == "minified"
    assert vendored_reason(write("web/app.js", "var a=1;" * 800), tmp_path) == "minified"      # one 6 KB line
    assert vendored_reason(write("api/types.pb.go", "// Code generated by protoc-gen-go. DO NOT EDIT.\npackage api\n"),
                           tmp_path) == "generated"
    assert vendored_reason(write("src/parser.c", "/* A Bison parser, made by GNU Bison 3.8.2.  */\nint yyparse;\n"),
                           tmp_path) == "generated"
    # product code: an ordinary file, a generator that only *writes* the marker in a string, a folder above the root
    assert vendored_reason(write("web/cart.js", plain), tmp_path) is None
    assert vendored_reason(write("gen/writer.go", 'package gen\n\nconst header = "// Code generated by x. DO NOT EDIT."\n'),
                           tmp_path) is None
    project = tmp_path / "vendor" / "project"
    assert vendored_reason(write("vendor/project/src/app.py", "def main():\n    return 1\n"), project) is None


def test_a_minified_file_keeps_its_file_node_unless_switched_on(tmp_path, monkeypatch):
    from verinoda.project_index.extract import extract

    lib = tmp_path / "web" / "lib.min.js"
    lib.parent.mkdir(parents=True)
    lib.write_text("function a(){return b()}\nfunction b(){return 1}\n", encoding="utf-8")
    app = tmp_path / "web" / "app.js"
    app.write_text("function main() { return helper(); }\nfunction helper() { return 2; }\n", encoding="utf-8")

    monkeypatch.delenv("VERINODA_GRAPH_VENDORED", raising=False)
    out = extract([lib, app], cache_root=tmp_path / "cache", root=tmp_path, parallel=False)
    lib_nodes = [n for n in out["nodes"] if str(n.get("source_file", "")).endswith("lib.min.js")]
    assert [n["label"] for n in lib_nodes] == ["lib.min.js"] and lib_nodes[0]["vendored"] == "minified"
    assert not any(str(e.get("source_file", "")).endswith("lib.min.js") for e in out["edges"])
    assert {"main()", "helper()"} <= {n["label"] for n in out["nodes"]}   # the product file is untouched

    monkeypatch.setenv("VERINODA_GRAPH_VENDORED", "1")
    out = extract([lib, app], cache_root=tmp_path / "cache", root=tmp_path, parallel=False)
    assert {"a()", "b()"} <= {n["label"] for n in out["nodes"]}


def test_the_config_switch_sets_the_extractor_variable(tmp_path, monkeypatch):
    import json

    from verinoda import index
    from verinoda.paths import ensure_atlas

    monkeypatch.delenv("VERINODA_GRAPH_VENDORED", raising=False)
    with index._vendored_switch(tmp_path):
        assert "VERINODA_GRAPH_VENDORED" not in os.environ
    (ensure_atlas(tmp_path) / "config.json").write_text(json.dumps({"index": {"vendored": True}}), encoding="utf-8")
    with index._vendored_switch(tmp_path):
        assert os.environ["VERINODA_GRAPH_VENDORED"] == "1"
    assert "VERINODA_GRAPH_VENDORED" not in os.environ


# -- C/C++: every header and inline-implementation suffix is C++ code ------------------------------------------

def test_cpp_header_suffixes_are_detected_and_extracted(tmp_path):
    from verinoda.project_index.detect import FileType, classify_file
    from verinoda.project_index.extract import _get_extractor, extract, extract_cpp

    body = "namespace demo {\ntemplate <typename T> T twice(T x) { return helper(x) + x; }\nint helper(int x) { return x; }\n}\n"
    for suffix in (".hh", ".hxx", ".ipp", ".inl", ".tpp"):
        path = tmp_path / f"widget{suffix}"
        path.write_text(body, encoding="utf-8")
        assert classify_file(path) == FileType.CODE, suffix
        assert _get_extractor(path) is extract_cpp, suffix
        out = extract([path], cache_root=tmp_path / "cache", root=tmp_path, parallel=False)
        labels = {n["label"] for n in out["nodes"]}
        assert {"twice()", "helper()"} <= labels, (suffix, labels)
        assert any(e["relation"] == "calls" for e in out["edges"]), suffix


# -- review of D65: fixes, one regression test each ------------------------------------------------------------

def _write(root: Path, rel: str, text: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_product_folders_named_like_vendor_keep_their_symbols(tmp_path, monkeypatch):
    # H1: a Vendor/ namespace, a Vendors/ folder, lib/deps/, src/extern/ and a Rails app/controllers/vendor/ are
    # product code; vendor/ at the root, beside a manifest or under a static-asset folder, _vendor/, third_party/,
    # deps/ beside mix.exs and a submodule under extern/ are third-party code
    from verinoda.project_index.extract import extract
    from verinoda.project_index.vendored import vendored_reason

    for rel in ("app/Http/Controllers/Vendor/PayoutController.php", "src/Vendors/VendorService.cs",
                "lib/deps/resolver.py", "src/extern/c_api.cpp", "app/controllers/vendor/dashboard_controller.rb"):
        assert vendored_reason(_write(tmp_path, rel, "x = 1\n"), tmp_path) is None, rel
    _write(tmp_path, "tools/go.mod", "module x\n")
    _write(tmp_path, "mix.exs", "defmodule X do end\n")
    _write(tmp_path, ".gitmodules", '[submodule "imgui"]\n\tpath = extern/imgui\n\turl = https://example.invalid/i\n')
    for rel in ("vendor/decNumber/decNumber.c", "web/static/admin/js/vendor/jquery/jquery.js", "src/pip/_vendor/x.py",
                "src/third_party/zlib/inflate.c", "tools/vendor/golang.org/x/y.go", "deps/plug/lib/plug.ex",
                "extern/imgui/imgui.cpp"):
        assert vendored_reason(_write(tmp_path, rel, "x = 1\n"), tmp_path) == "vendored", rel

    monkeypatch.delenv("VERINODA_GRAPH_VENDORED", raising=False)
    php = _write(tmp_path, "app/Http/Controllers/Vendor/PayoutController.php", """<?php
namespace App\\Http\\Controllers\\Vendor;
class PayoutController {
  public function settleVendorPayout($vendorId) { return $this->computeCommission($vendorId); }
  public function computeCommission($id) { return $id * 0.1; }
}
""")
    out = extract([php], cache_root=tmp_path / "cache", root=tmp_path, parallel=False)
    labels = {n["label"] for n in out["nodes"]}
    assert {".settleVendorPayout()", ".computeCommission()"} <= labels
    assert any(e["relation"] == "calls" for e in out["edges"])


def test_a_comment_about_generated_code_is_not_a_generator_header(tmp_path):
    # H2 (and L8): the marker opens the comment; the graph and the guards read the same rule
    from verinoda import guards
    from verinoda.project_index.vendored import generated_header, vendored_reason

    hand_written = {
        "codegen/emit.py": "# Emits the auto-generated code by walking the schema.\ndef emit():\n    return 1\n",
        "tools/check.py": "# Fails CI when an auto-generated file is out of date.\ndef check():\n    return 1\n",
        "gen/doc.go": '// Package gen writes Go files that begin with\n// "// Code generated by gen. DO NOT EDIT."\n'
                      "package gen\n",
        "src/util.js": "// @generated-by is not used here; see docs\nfunction f() { return 1; }\n",
        "src/notes.py": "# This file is auto-generated.\ndef f():\n    return 1\n",   # no warning against editing
    }
    for rel, text in hand_written.items():
        path = _write(tmp_path, rel, text)
        assert vendored_reason(path, tmp_path) is None, rel
        assert not guards.is_generated(tmp_path, rel), rel
    generated = {
        "api/types.pb.go": "// Code generated by protoc-gen-go. DO NOT EDIT.\npackage api\n",
        "api/x_pb2.py": "# -*- coding: utf-8 -*-\n# Generated by the protocol buffer compiler.  DO NOT EDIT!\n",
        "src/parser.c": "/* A Bison parser, made by GNU Bison 3.8.2.  */\nint yyparse;\n",
        "src/lexer.c": '#line 2 "lexer.c"\n#define  YY_INT_ALIGNED short int\n/* A lexical scanner generated by flex */\n',
        "web/Query.graphql.js": "/**\n * @generated SignedSource<<abc>>\n */\nmodule.exports = {};\n",
        "src/proto.rs": "// This file is @generated by prost-build.\npub struct A {}\n",
        "db/schema.rb": "# This file is auto-generated from the current state of the database. Instead\n"
                        "# of editing this file, please use the migrations feature of Active Record to\n"
                        "# incrementally modify your database, and then regenerate this schema definition.\n"
                        "ActiveRecord::Schema.define(version: 1) do\nend\n",
    }
    for rel, text in generated.items():
        path = _write(tmp_path, rel, text)
        assert vendored_reason(path, tmp_path) == "generated", rel
        assert guards.is_generated(tmp_path, rel), rel
    assert not generated_header('const header = "// Code generated by x. DO NOT EDIT."\n')


def test_a_long_data_line_or_a_bundle_script_name_is_not_minified(tmp_path):
    # M1: a lookup table in product code, build scripts named *-bundle.js / *-min.js, a compact package.json
    from verinoda.project_index.vendored import vendored_reason

    table = "const T = [" + ",".join(str(i) for i in range(2000)) + "];\n"
    table += "function lookup(i){ return T[i]; }\nfunction use(){ return lookup(3); }\n"
    package = '{"name":"x","dependencies":{' + ",".join(f'"dep{i}":"^1.0.{i}"' for i in range(300)) + "}}"
    assert len(package) > 4096
    for rel, text in (("web/table.js", table), ("scripts/make-bundle.js", "module.exports = 1;\n"),
                      ("src/create-bundle.mjs", "export default 1;\n"), ("src/web-min.js", "var a = 1;\n"),
                      ("package.json", package)):
        assert vendored_reason(_write(tmp_path, rel, text), tmp_path) is None, rel
    chart = "/*!\n * Chart.js v4.4.0\n * MIT\n */\n!function(t,e){" + "t.x=function(n){return e(n);};" * 400 + "}();\n"
    assert vendored_reason(_write(tmp_path, "web/chart.js", chart), tmp_path) == "minified"
    assert vendored_reason(_write(tmp_path, "web/chart.bundle.js", "var a = 1;\n"), tmp_path) == "minified"


def test_an_import_of_a_vendored_module_binds_to_it_not_to_a_same_named_product_function(tmp_path, monkeypatch):
    # M2: a vendored file keeps its definitions (marked), so the import still binds; no call leaves the file
    from verinoda.project_index.extract import extract

    monkeypatch.delenv("VERINODA_GRAPH_VENDORED", raising=False)
    paths = [
        _write(tmp_path, "vendor/yamlish/__init__.py", "def parse(text):\n    return helper(text)\n\n"
                                                       "def helper(text):\n    return text\n"),
        _write(tmp_path, "app/config.py", "from vendor.yamlish import parse\n\ndef load(text):\n    return parse(text)\n"),
        _write(tmp_path, "app/html.py", "def parse(markup):\n    return markup\n"),
    ]
    out = extract(paths, cache_root=tmp_path / "cache", root=tmp_path, parallel=False)
    by_id = {n["id"]: n for n in out["nodes"]}
    calls = [(by_id[e["source"]], by_id.get(e["target"], {}), e) for e in out["edges"] if e["relation"] == "calls"]
    assert [(t.get("source_file"), e["confidence"]) for s, t, e in calls if s["label"] == "load()"] == [
        ("vendor/yamlish/__init__.py", "EXTRACTED")]
    assert not any(str(s.get("source_file", "")).startswith("vendor/") for s, _t, _e in calls)
    vendored = [n for n in out["nodes"] if str(n.get("source_file", "")).startswith("vendor/")]
    assert {n["label"] for n in vendored} >= {"parse()", "helper()"}
    assert all(n.get("vendored") == "vendored" for n in vendored)


_RECEIVER_FIXTURES = {
    # M3: PHP receivers whose class the file states
    "Baz.php": '''<?php
class Foo { public function bar() { return 1; } }
class Baz {
  private Foo $foo;
  public function __construct(private Foo $promoted) {}
  public function run(Foo $x) { return $x->bar(); }
  public function go() { $y = new Foo(); return $y->bar(); }
  public function viaProp() { return $this->foo->bar(); }
  public function viaPromoted() { return $this->promoted->bar(); }
  public function external(Request $r) { return $r->bar(); }
}
''',
    # M4 and L7: Go embedding, fields, package and local variables, a typed parameter
    "server.go": '''package p

type Base struct{}

func (b *Base) Hello() int { return 1 }

type Handler struct{}

func (h *Handler) Serve() int { return 2 }

type Server struct {
	*Base
	h *Handler
}

var defaultServer = &Server{}

func (s *Server) Run() int { return s.Hello() + s.h.Serve() }

func Start() int { return defaultServer.Run() }

func NewServer() *Server { return &Server{} }

func Main() int { srv := NewServer(); return srv.Run() }

type metricHistory struct{}

func (m *metricHistory) add() {}

func record(h *metricHistory) { h.add() }

func Shadow(defaultServer *other.Server) int { return defaultServer.Run() }
''',
    # M6: `self.class.make` is the own class's class method
    "baz.rb": '''class Baz
  def helper
    self.class.make
  end

  def self.make
    new
  end
end
''',
    # L1 and L2: super() follows the C3 order and passes over `object`
    "mro.py": '''class A:
    def m(self):
        return 1

class B(A):
    pass

class C(A):
    def m(self):
        return 2

class D(B, C):
    def m(self):
        return super().m()

class Mixin(object):
    pass

class Keeper(object):
    def save(self):
        return 1

class Child(Mixin, Keeper):
    def save(self):
        return super().save()
''',
    # L3: the turbofish names the type
    "pool.rs": '''struct Pool<T> { v: T }

impl<T> Pool<T> {
    fn new() -> Self { unimplemented!() }
}

fn new() -> u8 { 0 }

fn make2() -> Pool<u8> { Pool::<u8>::new() }
''',
}


def test_receivers_whose_type_the_file_states_bind_to_that_type(tmp_path):
    pairs = _call_pairs(tmp_path, _RECEIVER_FIXTURES)
    # M3: a typed parameter, a `new` local, a typed and a promoted property; a type of another file binds nothing
    for caller in ("Baz.run", "Baz.go", "Baz.viaProp", "Baz.viaPromoted"):
        assert ("Baz.php", caller, "Foo.bar") in pairs, caller
    assert not any(src == "Baz.external" for f, src, _ in pairs if f == "Baz.php")
    # M4: a promoted method of an embedded struct, a field's type, a package variable, a constructor's result;
    # L7: a parameter of a type of the package
    for src, tgt in (("Server.Run", "Base.Hello"), ("Server.Run", "Handler.Serve"), ("Start", "Server.Run"),
                     ("Main", "Server.Run"), ("record", "metricHistory.add")):
        assert ("server.go", src, tgt) in pairs, (src, tgt)
    assert ("server.go", "Shadow", "Server.Run") not in pairs     # a parameter of another package's type shadows
    # M6
    assert ("baz.rb", "Baz.helper", "Baz.make") in pairs
    # L1: D's MRO is D, B, C, A: super().m() is C.m; L2: `(object)` does not end the search
    assert ("mro.py", "D.m", "C.m") in pairs and ("mro.py", "D.m", "A.m") not in pairs
    assert ("mro.py", "Child.save", "Keeper.save") in pairs
    # L3
    assert ("pool.rs", "make2", "Pool<T>.new") in pairs and ("pool.rs", "make2", "new") not in pairs


def test_the_php_self_delegation_stays_unbound(tmp_path):
    # M3: the narrowed rule still leaves `$this->dispatcher->handle()` inside App::handle unbound
    pairs = _call_pairs(tmp_path, {"App.php": _MEMBER_FIXTURES["App.php"]})
    assert ("App.php", "App.run", "App.handle") in pairs
    assert ("App.php", "App.handle", "App.handle") not in pairs


def test_product_files_named_like_tests_stay_in_a_guard_scope(tmp_path):
    # M5: a FIPS self test, a lab-test model and a folder that merely ends in Tests are product code
    from verinoda import guards

    files = ["app/Models/LabTest.php", "providers/fips/self_test.c", "app/Models/Patient.php",
             "src/HealthTests/Probe.cs", "tests/Unit/PatientTest.php"]
    kept, left_out = guards.scope_files(tmp_path, {"kind": "only_in"}, files)
    assert kept == files[:4]
    assert any("1 test file" in x for x in left_out)


def test_the_vendored_switch_parses_strictly_and_is_part_of_the_stamp(tmp_path, monkeypatch):
    # L9: "false" is off; the switch changes the extraction stamp, so the next update rebuilds the graph
    import json

    from verinoda import buildlock, index
    from verinoda.paths import ensure_atlas

    monkeypatch.delenv("VERINODA_GRAPH_VENDORED", raising=False)
    config = ensure_atlas(tmp_path) / "config.json"
    config.write_text(json.dumps({"index": {"vendored": "false"}}), encoding="utf-8")
    with index._vendored_switch(tmp_path):
        assert "VERINODA_GRAPH_VENDORED" not in os.environ
    off = buildlock.extraction_stamp(tmp_path)
    assert off == buildlock.extraction_stamp()
    config.write_text(json.dumps({"index": {"vendored": True}}), encoding="utf-8")
    assert buildlock.extraction_stamp(tmp_path) != off


def test_folders_above_the_root_or_behind_a_link_never_count(tmp_path, monkeypatch):
    # L10: a linked folder whose target lies under a deps/ folder outside the repository; L6: no resolve() per file
    import pathlib

    from verinoda.project_index.vendored import vendored_reason

    repo = tmp_path / "deps" / "repo"
    target = _write(tmp_path, "shared/x.py", "x = 1\n").parent
    (repo / "src").mkdir(parents=True)
    link = repo / "lib"
    try:
        if os.name == "nt":
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
        else:
            link.symlink_to(target, target_is_directory=True)
    except OSError:
        link = None
    if link is not None:
        assert vendored_reason(link / "x.py", repo) is None
    assert vendored_reason(target / "x.py", repo) is None        # outside the root: its folders do not count

    def no_resolve(self, strict=False):
        raise AssertionError("vendored_reason resolved a path")

    monkeypatch.setattr(pathlib.Path, "resolve", no_resolve)
    assert vendored_reason(_write(repo, "src/app.py", "x = 1\n"), repo) is None
    assert vendored_reason(_write(repo, "vendor/v.py", "x = 1\n"), repo) == "vendored"
