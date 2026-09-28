"""Local changes to the vendored graph extractor (verinoda/project_index, docs/UPSTREAM.md "Modified"):
the C# placeholder index, member calls that no longer bind to a same-named function of the file, vendored,
minified and generated files kept to a file node, more C/C++ header suffixes and ``#include`` evidence."""

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


def _call_pairs(tmp_path) -> set[tuple[str, str, str]]:
    from verinoda.project_index.extract import extract

    paths = []
    for name, body in _MEMBER_FIXTURES.items():
        path = tmp_path / name
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
