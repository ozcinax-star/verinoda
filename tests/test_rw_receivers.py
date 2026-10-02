"""Receivers of a stated type in the vendored extractor (verinoda/project_index, docs/UPSTREAM.md "Modified"):
Go receivers typed across the files of a package, Rust `Type::m()`, locals, parameters and trait objects, PHP
`Foo::m()`, and JavaScript `fn.call/apply/bind`. Only a type the source states binds; a trait or interface binds
to its own declaration of the method, never to one implementation."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

from pathlib import Path  # noqa: E402


def _edges(tmp_path, fixtures: dict[str, str]) -> dict[tuple[str, str, str], dict]:
    """(caller, relation, target) -> edge, each end named `file:Owner.name` (or `file:name`)."""
    from verinoda.project_index.extract import extract

    paths = []
    for name, body in fixtures.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        if not name.endswith(".toml"):
            paths.append(path)
    out = extract(paths, cache_root=tmp_path / "cache", root=tmp_path, parallel=False)
    by_id = {n["id"]: n for n in out["nodes"]}
    owner = {e["target"]: e["source"] for e in out["edges"] if e["relation"] == "method"}

    def name(nid: str) -> str:
        n = by_id.get(nid, {})
        label = str(n.get("label", nid)).strip("()").lstrip(".")
        if nid in owner:
            label = f"{str(by_id.get(owner[nid], {}).get('label', '')).split('<', 1)[0]}.{label}"
        rel = Path(str(n.get("source_file") or "")).as_posix()
        rel = rel.split(tmp_path.as_posix() + "/", 1)[-1]
        return f"{rel}:{label}"

    return {(name(e["source"]), e["relation"], name(e["target"])): e
            for e in out["edges"] if e["relation"] in ("calls", "indirect_call")}


def _calls(edges) -> set[tuple[str, str]]:
    return {(s, t) for s, r, t in edges if r == "calls"}


# -- Go ---------------------------------------------------------------------------------------------------------

_GO = {
    "gin/tree.go": '''package gin

type node struct{ path string }

func (n *node) getValue(p string) string { return p }
func (n *node) addRoute(p string)        {}

type other struct{}

func (o *other) getValue(p string) string { return p }

type methodTree struct {
	method string
	root   *node
}

type methodTrees []methodTree

func (trees methodTrees) get(m string) *node { return nil }

func newNode() *node { return &node{} }

type Router interface {
	Handle()
}

type A struct{}

func (A) Handle() {}

type B struct{}

func (B) Handle() {}
''',
    "gin/gin.go": '''package gin

type Engine struct {
	trees methodTrees
	r     Router
}

func (e *Engine) serve(p string) {
	t := e.trees
	root := t[0].root
	root.getValue(p)
}

func (e *Engine) add(p string) {
	root := e.trees.get("GET")
	root.addRoute(p)
}

func (e *Engine) dispatch() {
	e.r.Handle()
}

func build() {
	n := newNode()
	n.addRoute("/")
	var m node
	m.getValue("x")
	for _, tree := range methodTrees{} {
		tree.root.getValue("y")
	}
}

func shadow() {
	x := &node{}
	if true {
		x := &other{}
		x.getValue("a")
	}
	x.getValue("b")
}
''',
    "sub/tree.go": '''package sub

type node struct{}

func (n *node) getValue(p string) string { return p }

func local() {
	n := &node{}
	n.getValue("z")
}
''',
}


def test_go_receivers_are_typed_across_the_files_of_a_package(tmp_path):
    edges = _edges(tmp_path, _GO)
    calls = _calls(edges)
    for caller, target in (("serve", "node.getValue"), ("add", "methodTrees.get"), ("add", "node.addRoute"),
                           ("build", "node.addRoute"), ("build", "node.getValue")):
        key = (f"gin/gin.go:Engine.{caller}" if caller in ("serve", "add") else f"gin/gin.go:{caller}",
               "calls", f"gin/tree.go:{target}")
        assert key in edges, key
        assert edges[key]["confidence"] == "INFERRED" and edges[key]["confidence_score"] == 0.85
    # a package of another directory with a type of the same name is another type
    assert ("sub/tree.go:local", "sub/tree.go:node.getValue") in calls
    assert not any(t.startswith("sub/") for s, t in calls if s.startswith("gin/"))


def test_go_an_interface_receiver_binds_to_the_interface_method_only(tmp_path):
    edges = _edges(tmp_path, _GO)
    key = ("gin/gin.go:Engine.dispatch", "calls", "gin/tree.go:Router.Handle")
    assert key in edges and edges[key]["confidence_score"] == 0.75
    assert not any(s == "gin/gin.go:Engine.dispatch" and t in ("gin/tree.go:A.Handle", "gin/tree.go:B.Handle")
                   for s, t in _calls(edges))


def test_go_a_name_bound_to_two_types_binds_nothing(tmp_path):
    calls = _calls(_edges(tmp_path, _GO))
    assert not any(s == "gin/gin.go:shadow" for s, _ in calls), sorted(c for c in calls if "shadow" in c[0])


# -- Rust -------------------------------------------------------------------------------------------------------

_RUST = {
    "Cargo.toml": '[package]\nname = "my-crate"\nversion = "0.1.0"\n',
    "src/controller.rs": '''pub struct Controller<'a> { n: &'a u8 }

impl Controller<'_> {
    pub fn new<'a>(n: &'a u8) -> Controller<'a> { Controller { n } }
    pub fn make() -> Option<u8> { None }
    pub fn run(&self) -> bool { true }
}

pub trait Printer {
    fn print_header(&mut self);
}

pub struct Simple;
impl Printer for Simple { fn print_header(&mut self) {} }

pub struct Inter;
impl Printer for Inter { fn print_header(&mut self) {} }

pub struct Command;
impl Command {
    pub fn new(s: &str) -> Self { Command }
    pub fn run(&self) -> bool { true }
}
''',
    "src/other.rs": '''pub struct Other;
impl Other {
    pub fn new() -> Self { Other }
}
''',
    "src/main.rs": '''use crate::controller::{Controller, Printer};
use crate::other::Other;

fn run_controller(n: &u8) -> bool {
    let c = Controller::new(n);
    c.run()
}

fn typed(c: &Controller) -> bool {
    c.run()
}

fn header(p: &mut dyn Printer, q: Box<dyn Printer>) {
    p.print_header();
}

fn shadowed(n: &u8) {
    let c = Controller::new(n);
    let c = Other::new();
    c.run();
}

fn not_a_constructor() {
    let c = Controller::make();
    c.run();
}
''',
    "src/ext.rs": '''use std::process::Command;

fn external() {
    let c = Command::new("ls");
    c.run();
    std::process::Command::new("ls");
}
''',
    "src/bin/tool.rs": '''use my_crate::controller::Controller;

fn main() {
    let n = 1;
    Controller::new(&n).run();
}
''',
}


def test_rust_type_paths_bind_to_the_type_s_method_of_this_crate(tmp_path):
    edges = _edges(tmp_path, _RUST)
    key = ("src/main.rs:run_controller", "calls", "src/controller.rs:Controller.new")
    assert key in edges and edges[key]["confidence"] == "EXTRACTED"
    # through the crate's own name in Cargo.toml (`my-crate` is `my_crate`)
    assert ("src/bin/tool.rs:main", "src/controller.rs:Controller.new") in _calls(edges)
    # `std::process::Command` is not the crate's `Command`, imported or written with its path
    assert not any(s == "src/ext.rs:external" for s, _ in _calls(edges))


def test_rust_locals_and_parameters_of_a_stated_type(tmp_path):
    edges = _edges(tmp_path, _RUST)
    for caller in ("run_controller", "typed"):
        key = (f"src/main.rs:{caller}", "calls", "src/controller.rs:Controller.run")
        assert key in edges, caller
        assert edges[key]["confidence"] == "INFERRED" and edges[key]["confidence_score"] == 0.85
    calls = _calls(edges)
    # shadowed by another type, or a function that does not return Self: no type
    assert ("src/main.rs:shadowed", "src/controller.rs:Controller.run") not in calls
    assert ("src/main.rs:not_a_constructor", "src/controller.rs:Controller.run") not in calls


def test_rust_a_trait_object_binds_to_the_trait_declaration_only(tmp_path):
    edges = _edges(tmp_path, _RUST)
    key = ("src/main.rs:header", "calls", "src/controller.rs:Printer.print_header")
    assert key in edges and edges[key]["confidence_score"] == 0.75
    assert not any(s == "src/main.rs:header" and t != "src/controller.rs:Printer.print_header"
                   for s, t in _calls(edges))


def test_rust_in_file_receivers_and_trait_objects(tmp_path):
    calls = _calls(_edges(tmp_path, {"lib.rs": '''pub trait Draw { fn draw(&self); }
pub struct Pen;
impl Draw for Pen { fn draw(&self) {} }
impl Pen { pub fn new() -> Self { Pen } pub fn ink(&self) {} }

fn paint(d: &dyn Draw, pens: Vec<Pen>) {
    d.draw();
    let p = Pen::new();
    p.ink();
    let q = Pen {};
    q.ink();
    for p2 in pens { p2.ink(); }
}

fn generic<T: Draw>(t: &T) { t.draw(); }

pub struct Holder { p: u8 }

fn destructured(h: Holder) {
    let p = Pen::new();
    let Holder { p } = h;
    p.ink();
}
'''}))
    assert ("lib.rs:paint", "lib.rs:Draw.draw") in calls
    assert ("lib.rs:paint", "lib.rs:Pen.draw") not in calls
    assert ("lib.rs:paint", "lib.rs:Pen.ink") in calls
    assert not any(s == "lib.rs:generic" for s, _ in calls)
    # rebound by a pattern: no type
    assert ("lib.rs:destructured", "lib.rs:Pen.ink") not in calls


# -- PHP --------------------------------------------------------------------------------------------------------

_PHP = {
    "src/Utils.php": '''<?php
namespace App;

class Utils
{
    public static function chooseHandler() { return 1; }
}
''',
    "src/Sub.php": '''<?php
namespace App;

class Sub extends Utils
{
}
''',
    "src/Stack.php": '''<?php
namespace App;

use Other\\Lib\\Utils as OU;

class Stack
{
    public static function create()
    {
        Utils::chooseHandler();
        OU::chooseHandler();
        self::helper();
        Sub::chooseHandler();
    }

    private static function helper() { return 2; }
}

class Base
{
    public function m() { return 1; }
}

class Child extends Base
{
    public function m() { return parent::m(); }
}
''',
    "tests/StackTest.php": '''<?php
namespace App\\Tests;

use Other\\Lib\\Utils;

class StackTest
{
    public function testIt() { Utils::chooseHandler(); }
}
''',
}


def test_php_static_calls_bind_to_the_method_of_the_class_their_namespace_names(tmp_path):
    edges = _edges(tmp_path, _PHP)
    key = ("src/Stack.php:Stack.create", "calls", "src/Utils.php:Utils.chooseHandler")
    assert key in edges and edges[key]["confidence"] == "EXTRACTED"
    calls = _calls(edges)
    assert ("src/Stack.php:Stack.create", "src/Stack.php:Stack.helper") in calls
    assert ("src/Stack.php:Child.m", "src/Stack.php:Base.m") in calls
    # the class itself is no longer the target, and an imported `Other\\Lib\\Utils` is another class
    assert ("src/Stack.php:Stack.create", "src/Utils.php:Utils") not in calls
    assert not any(s == "tests/StackTest.php:StackTest.testIt" for s, _ in calls)


def test_php_an_inherited_static_method_is_inferred(tmp_path):
    edges = _edges(tmp_path, _PHP)
    # `Sub::chooseHandler()` is Utils's, through `extends` (deduplicated with the direct call: one edge)
    key = ("src/Stack.php:Stack.create", "calls", "src/Utils.php:Utils.chooseHandler")
    assert key in edges
    edges2 = _edges(tmp_path / "two", {k: v.replace("        Utils::chooseHandler();\n", "")
                                       for k, v in _PHP.items()})
    key2 = ("src/Stack.php:Stack.create", "calls", "src/Utils.php:Utils.chooseHandler")
    assert key2 in edges2 and edges2[key2]["confidence"] == "INFERRED"


# -- JavaScript -------------------------------------------------------------------------------------------------

_JS = {
    "dispatch.js": "export default function dispatchRequest(config) { return config; }\n",
    "Axios.js": '''import dispatchRequest from './dispatch.js';

function request(config) {
  const chain = [dispatchRequest.bind(this)];
  return dispatchRequest.call(this, config);
}

function viaApply(args) {
  return dispatchRequest.apply(this, args);
}

function shadow(dispatchRequest) {
  return dispatchRequest.call(this);
}

function local() {
  function helper(x) { return x; }
  return helper.call(null, 1);
}
''',
}


def test_js_call_apply_and_bind_call_the_function(tmp_path):
    edges = _edges(tmp_path, _JS)
    assert ("Axios.js:request", "calls", "dispatch.js:dispatchRequest") in edges
    assert ("Axios.js:viaApply", "calls", "dispatch.js:dispatchRequest") in edges
    assert ("Axios.js:local", "calls", "Axios.js:helper") in edges   # a nested function, in the file
    # a parameter named like the function is not it
    assert not any(s == "Axios.js:shadow" for s, _, _ in edges)


def test_an_incremental_build_keeps_the_receiver_edges(tmp_path):
    # the unchanged files' nodes reach the passes as context (no line, persisted markers only)
    import json

    from verinoda import index

    for name, body in {**_GO, **_RUST, **_PHP}.items():
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")

    def calls() -> set[tuple[str, str]]:
        data = json.loads(index.graph_path(tmp_path).read_text(encoding="utf-8"))
        label = {n["id"]: f"{Path(str(n.get('source_file'))).name}:{n.get('label')}" for n in data["nodes"]}
        return {(label.get(e["source"]), label.get(e["target"])) for e in data.get("links", data.get("edges", []))
                if e.get("relation") == "calls"}

    assert index.build(tmp_path)["ok"]
    full = calls()
    expected = {("gin.go:.serve()", "tree.go:.getValue()"), ("main.rs:run_controller()", "controller.rs:.run()"),
                ("main.rs:header()", "controller.rs:.print_header()"), ("Stack.php:.create()", "Utils.php:.chooseHandler()")}
    assert expected <= full, expected - full
    callers = [tmp_path / "gin/gin.go", tmp_path / "src/main.rs", tmp_path / "src/Stack.php"]
    for c in callers:
        c.write_text(c.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    assert index.build(tmp_path, changed=callers)["ok"]
    assert calls() == full


def test_js_bind_alone_is_an_indirect_call(tmp_path):
    edges = _edges(tmp_path, {"dispatch.js": _JS["dispatch.js"], "a.js": '''import dispatchRequest from './dispatch.js';

function chain() {
  return [dispatchRequest.bind(this), undefined];
}
'''})
    assert ("a.js:chain", "indirect_call", "dispatch.js:dispatchRequest") in edges
    assert ("a.js:chain", "calls", "dispatch.js:dispatchRequest") not in edges
