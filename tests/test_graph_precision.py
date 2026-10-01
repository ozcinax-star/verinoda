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
  public function __construct(private Foo $promoted, Foo $foo) { $this->foo = $foo; }
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

func Loop(xs []*other.Server) {
	for _, defaultServer := range xs {
		defaultServer.Run()
	}
	go func(defaultServer *other.Server) { defaultServer.Run() }(nil)
}
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
    # M3: a typed parameter, a `new` local, a typed property (also assigned from an injected parameter) and a
    # promoted one; a type of another file binds nothing
    for caller in ("Baz.run", "Baz.go", "Baz.viaProp", "Baz.viaPromoted"):
        assert ("Baz.php", caller, "Foo.bar") in pairs, caller
    assert not any(src == "Baz.external" for f, src, _ in pairs if f == "Baz.php")
    # M4: a promoted method of an embedded struct, a field's type, a package variable, a constructor's result;
    # L7: a parameter of a type of the package
    for src, tgt in (("Server.Run", "Base.Hello"), ("Server.Run", "Handler.Serve"), ("Start", "Server.Run"),
                     ("Main", "Server.Run"), ("record", "metricHistory.add")):
        assert ("server.go", src, tgt) in pairs, (src, tgt)
    # a parameter, a range variable or a closure parameter of another package's type shadows the package variable
    assert ("server.go", "Shadow", "Server.Run") not in pairs and ("server.go", "Loop", "Server.Run") not in pairs
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


# -- JS/TS: functions assigned to an object's property, and functions bound inside a function ---------------------

_JS_FIXTURES = {
    # Express 4/5 lib/response.js and lib/application.js are written this way
    "response.js": '''\'use strict\'
var send = require('send');
var res = Object.create(null);
module.exports = res;

res.status = function status(code) {
  this.statusCode = code;
  return this;
};

res.send = function send(body) {
  return this.status(200);
};

res.json = function json(obj) {
  var body = stringify(obj);
  return this.send(body);
};

res.sendFile = function sendFile(path) {
  send(path);
  return sendfile(this, path);
};

res.type = function contentType(type) {
  return res.status(type);
};

res.render = function render(view) {
  var app = this.app;
  app.render(view);
};

res.links = (links) => res.send(links);

res.set =
res.header = function header(field) {
  return this.links(field);
};

res.limit = 10;
res.handlers = [stringify];
window.helper = function helper() { return stringify(1); };

function sendfile(res, file) {
  return res.json(file);
}

function stringify(value) {
  return JSON.stringify(value);
}
''',
    "application.js": '''var app = exports = module.exports = {};

app.init = function init() {
  this.enable('x');
};

app.enable = function enable(setting, val) {
  return tryRender(setting, val);
};

function tryRender(view, options) {
  return view;
}

function Foo() {}

Foo.create = function () {
  return new Foo();
};

Foo.prototype.run = function () {
  return Foo.create();
};
''',
    "sdk.ts": '''export class LoginService {
  public static loginAccessToken(options: unknown) {
    return options
  }
}
''',
    "useAuth.ts": '''import { LoginService } from "./sdk"

const isLoggedIn = () => true

const useAuth = () => {
  const login = async (data: string) => {
    const response = await fetchToken({ body: data })
    return response
  }
  const logout = () => {
    track("logout")
  }
  function reset() {
    logout()
  }
  return { login, logout, reset, ok: isLoggedIn() }
}

function other() {
  logout()
}

function logout() {
  return 0
}

function track(name: string) {
  return name
}

async function fetchToken(options: unknown) {
  return LoginService.loginAccessToken(options)
}

export default useAuth
''',
    "signals.js": '''function config() { return 1; }

function composeSignals(signals, config) {
  const controller = new AbortController();
  const abort = (err) => {
    controller.abort(err);
  };
  const unsubscribe = () => {
    signals.forEach((s) => s.unsubscribe(abort));
    use(config);
  };
  return unsubscribe;
}
''',
}


def _js_graph(tmp_path) -> dict:
    from verinoda.project_index.extract import extract

    paths = []
    for name, body in _JS_FIXTURES.items():
        path = tmp_path / name
        path.write_text(body, encoding="utf-8")
        paths.append(path)
    return extract(paths, cache_root=tmp_path / "cache", root=tmp_path, parallel=False)


def test_functions_assigned_to_a_module_object_are_its_methods(tmp_path):
    out = _js_graph(tmp_path)
    by_id = {n["id"]: n for n in out["nodes"]}
    methods = {(by_id[e["source"]]["label"], by_id[e["target"]]["label"], e["source_location"])
               for e in out["edges"] if e["relation"] == "method" and e["source"] in by_id}
    # the property name is the method's name (`res.type = function contentType`), owned by `res`, at its line;
    # both names of an alias chain (`res.set = res.header = function header`)
    for name, line in (("status", 6), ("send", 11), ("json", 15), ("sendFile", 20), ("type", 25),
                       ("render", 29), ("links", 34), ("set", 36), ("header", 37)):
        assert ("res", f".{name}()", f"L{line}") in methods, name
    for owner, name in (("app", "init"), ("app", "enable"), ("Foo()", "create"), ("Foo()", "run")):
        assert any(o == owner and m == f".{name}()" for o, m, _ in methods), (owner, name)
    labels = {(Path(n["source_file"]).name, n["label"]) for n in out["nodes"] if n.get("source_file")}
    # not a function, or a receiver the file does not bind: no symbol
    assert not any(label in (".limit()", ".handlers()", ".helper()", "helper()") for _, label in labels)
    assert ("response.js", "res") in labels
    contains = {(by_id[e["source"]]["label"], by_id[e["target"]]["label"])
                for e in out["edges"] if e["relation"] == "contains" and e["source"] in by_id}
    assert ("response.js", "res") in contains


def test_calls_from_and_to_assigned_methods_resolve(tmp_path):
    pairs = _call_pairs(tmp_path, _JS_FIXTURES)
    # calls inside the methods are theirs; `this.m()` and `res.m()` reach the owner's method
    for src, tgt in (("res.json", "stringify"), ("res.json", "res.send"), ("res.send", "res.status"),
                     ("res.sendFile", "sendfile"), ("res.type", "res.status"), ("res.links", "res.send"),
                     ("res.set", "res.links"), ("res.header", "res.links")):
        assert ("response.js", src, tgt) in pairs, (src, tgt)
    assert ("application.js", "app.init", "app.enable") in pairs
    assert ("application.js", "app.enable", "tryRender") in pairs
    assert ("application.js", "Foo().run", "Foo().create") in pairs
    # a bare `send()` (the `send` package) is not res.send; `app.render()` on another object is not res.render;
    # `res.json()` on a parameter named `res` is not the module object's method
    assert ("response.js", "res.sendFile", "res.send") not in pairs
    assert ("response.js", "res.render", "res.render") not in pairs
    assert ("response.js", "sendfile", "res.json") not in pairs


def test_a_function_bound_inside_a_function_is_its_own_symbol(tmp_path):
    out = _js_graph(tmp_path)
    by_id = {n["id"]: n for n in out["nodes"]}
    contains = {(by_id[e["source"]]["label"], by_id[e["target"]]["label"], e["source_location"])
                for e in out["edges"] if e["relation"] == "contains" and e["source"] in by_id}
    assert ("useAuth()", "login()", "L6") in contains
    assert ("useAuth()", "logout()", "L10") in contains
    assert ("useAuth()", "reset()", "L13") in contains
    pairs = _call_pairs(tmp_path, _JS_FIXTURES)
    # the call inside `login` is login's, not useAuth's
    assert ("useAuth.ts", "login", "fetchToken") in pairs
    assert ("useAuth.ts", "useAuth", "fetchToken") not in pairs
    assert ("useAuth.ts", "logout", "track") in pairs and ("useAuth.ts", "useAuth", "track") not in pairs
    # a bare call binds to the definition visible from the caller: useAuth's `logout` inside it, the module's outside
    def logout_under(parent_label: str) -> str:
        return next(e["target"] for e in out["edges"] if e["relation"] == "contains"
                    and by_id.get(e["source"], {}).get("label") == parent_label
                    and by_id.get(e["target"], {}).get("label") == "logout()")

    nested_logout, module_logout = logout_under("useAuth()"), logout_under("useAuth.ts")
    assert nested_logout != module_logout
    reset = next(nid for nid, n in by_id.items() if n["label"] == "reset()")
    other = next(nid for nid, n in by_id.items() if n["label"] == "other()")
    calls = {(e["source"], e["target"]) for e in out["edges"] if e["relation"] == "calls"}
    assert (reset, nested_logout) in calls and (reset, module_logout) not in calls
    assert (other, module_logout) in calls and (other, nested_logout) not in calls


def test_a_nested_function_is_not_a_property_and_sees_the_enclosing_locals(tmp_path):
    out = _js_graph(tmp_path)
    by_id = {n["id"]: n for n in out["nodes"]}
    names = {nid: n["label"] for nid, n in by_id.items() if n.get("source_file", "").endswith("signals.js")}
    abort = next(nid for nid, label in names.items() if label == "abort()")
    unsubscribe = next(nid for nid, label in names.items() if label == "unsubscribe()")
    config = next(nid for nid, label in names.items() if label == "config()")
    edges = {(e["source"], e["target"], e["relation"]) for e in out["edges"]}
    # `controller.abort()` is not the nested `abort` calling itself
    assert (abort, abort, "calls") not in edges
    # the nested `abort` passed by name is an indirect call; `config` is the enclosing parameter, not config()
    assert (unsubscribe, abort, "indirect_call") in edges
    assert not any(s == unsubscribe and t == config for s, t, _ in edges)


def _labelled_edges(tmp_path, fixtures: dict[str, str]) -> set[tuple[str, str, str, str]]:
    """(caller file, caller label, relation, target file:label) for calls and indirect_call."""
    from verinoda.project_index.extract import extract

    paths = []
    for name, body in fixtures.items():
        path = tmp_path / name
        path.write_text(body, encoding="utf-8")
        paths.append(path)
    out = extract(paths, cache_root=tmp_path / "cache", root=tmp_path, parallel=False)
    by_id = {n["id"]: n for n in out["nodes"]}

    def where(nid: str) -> str:
        n = by_id.get(nid, {})
        return f"{Path(str(n.get('source_file') or '')).name}:{n.get('label', nid)}"

    return {(Path(e["source_file"]).name, str(by_id.get(e["source"], {}).get("label")), e["relation"],
             where(e["target"]))
            for e in out["edges"] if e["relation"] in ("calls", "indirect_call")}


def test_a_later_assigned_method_does_not_hide_a_module_function_of_the_same_name(tmp_path):
    edges = _labelled_edges(tmp_path, {"one.js": '''function send(x) { return x; }
var res = {};
res.send = function send2(b) { return b; };
function other() { send(1); }
function useRes() { res.send(2); }
function pass() { run(send); }
'''})
    assert ("one.js", "other()", "calls", "one.js:send()") in edges
    assert ("one.js", "useRes()", "calls", "one.js:.send()") in edges
    assert ("one.js", "pass()", "indirect_call", "one.js:send()") in edges
    assert ("one.js", "other()", "calls", "one.js:.send()") not in edges
    assert not any(t == "one.js:.send()" for _, src, _, t in edges if src == "pass()")


def test_nested_functions_are_not_bound_from_another_file_by_a_bare_name(tmp_path):
    edges = _labelled_edges(tmp_path, {
        "hooks.ts": '''export const useForm = () => {
  const onSubmit = () => 1;
  const reset = () => 2;
  return { onSubmit, reset };
};
''',
        "Form.ts": '''import { useForm } from "./hooks"

function Form(props) {
  const reset = props.reset;
  reset();
}

function Other({ onSubmit }) {
  onSubmit();
  useForm();
}

function Passes(cb) {
  run(reset);
}
''',
    })
    assert ("Form.ts", "Other()", "calls", "hooks.ts:useForm()") in edges
    # `reset` / `onSubmit` here are the caller's own local and parameter, and `run(reset)` names
    # nothing in scope: none of them is useForm's nested function
    assert not any(t in ("hooks.ts:reset()", "hooks.ts:onSubmit()") for _, _, _, t in edges), edges


def test_assigned_methods_are_not_bound_from_another_file_by_a_bare_name(tmp_path):
    edges = _labelled_edges(tmp_path, {
        "lib.js": '''var res = Object.create(null);
module.exports = res;
res.sendThing = function sendThing(b) { return b; };
res.use = function use(fn) { return fn; };
function View() {}
View.prototype.resolve = function resolve(dir) { return dir; };
''',
        "main.js": '''const lib = require('./lib');
function a(x) { sendThing(1); }
function b(use) { assert(use); }
function c() { lib.sendThing(2); }
function d() { return resolve('views'); }
''',
    })
    # nor is a `View.prototype.resolve` method reached by a bare `resolve()` (Express's application.js)
    assert not any(src in ("a()", "b()", "d()") and t.startswith("lib.js:") for _, src, _, t in edges), edges


def test_scoped_js_symbols_carry_the_no_bare_name_marker(tmp_path):
    from verinoda.project_index.extract import extract

    path = tmp_path / "m.js"
    path.write_text('''var res = {};
res.send = function send(b) { return b; };
function outer() { const inner = () => 1; return inner(); }
function plain() { return 1; }
function Foo() {}
Foo.prototype.run = function () { return 1; };
''', encoding="utf-8")
    out = extract([path], cache_root=tmp_path / "cache", root=tmp_path, parallel=False)
    marked = {n["label"] for n in out["nodes"] if n.get("_no_bare_name")}
    assert marked == {".send()", "inner()", ".run()"}
