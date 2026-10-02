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


# -- Review round: wrong targets of the same name -----------------------------------------------------------------


def test_php_a_qualified_name_of_another_namespace_is_not_the_file_s_own_class(tmp_path):
    calls = _calls(_edges(tmp_path, {
        "a/Utils.php": r'''<?php
namespace A;
use B\Utils as BU;

class Utils {
    public static function make() { return \B\Utils::make(); }
    public static function wrap() { return BU::make(); }
    public static function again() { return Utils::make(); }
}
''',
        "b/Utils.php": r'''<?php
namespace B;

class Utils {
    public static function make() { return 1; }
}
''',
    }))
    assert ("a/Utils.php:Utils.make", "b/Utils.php:Utils.make") in calls
    assert ("a/Utils.php:Utils.wrap", "b/Utils.php:Utils.make") in calls
    assert ("a/Utils.php:Utils.make", "a/Utils.php:Utils.make") not in calls
    assert ("a/Utils.php:Utils.wrap", "a/Utils.php:Utils.make") not in calls
    assert ("a/Utils.php:Utils.again", "a/Utils.php:Utils.make") in calls   # its own class, unqualified


def test_php_self_without_an_own_method_binds_nothing(tmp_path):
    calls = _calls(_edges(tmp_path, {
        "c.php": r'''<?php
namespace C;

function helper() { return 1; }

class Foo extends \Vendor\Base {
    public function f() { return self::helper(); }
    public function g() { return static::other(); }
}
class Bar {
    public static function helper() { return 2; }
    public static function other() { return 3; }
}
''',
    }))
    assert not {c for c in calls if c[0].startswith("c.php:Foo.")}, calls


def test_rust_a_path_to_another_crate_s_type_is_not_the_file_s_own(tmp_path):
    calls = _calls(_edges(tmp_path, {
        "Cargo.toml": '[package]\nname = "demo"\nversion = "0.1.0"\n',
        "src/error.rs": '''
use std::io;
pub struct Error { code: i32 }
impl Error {
    pub fn kind(&self) -> i32 { self.code }
}
impl From<io::Error> for Error {
    fn from(e: io::Error) -> Self { e.kind(); Error { code: 1 } }
}
pub fn wrap(e: &std::io::Error) -> i32 { e.kind(); 0 }
pub fn mine(e: &Error) -> i32 { e.kind() }
pub fn ctor() { let e = io::Error::new(); e.kind(); }
''',
    }))
    assert ("src/error.rs:mine", "src/error.rs:Error.kind") in calls
    for caller in ("from", "wrap", "ctor"):
        assert not {c for c in calls if c[0] == f"src/error.rs:{caller}"}, calls


def test_rust_an_imported_alias_names_the_imported_type(tmp_path):
    calls = _calls(_edges(tmp_path, {
        "Cargo.toml": '[package]\nname = "demo"\nversion = "0.1.0"\n',
        "src/lib.rs": "pub mod a; pub mod b; pub mod c;\n",
        "src/a.rs": "pub struct Foo;\nimpl Foo { pub fn new() -> Self { Foo } pub fn go(&self) {} }\n",
        "src/b.rs": "pub struct Bar;\nimpl Bar { pub fn new() -> Self { Bar } pub fn go(&self) {} }\n",
        "src/c.rs": '''
use crate::a::Foo as Bar;
pub fn run() {
    let x = Bar::new();
    x.go();
}
pub fn run2(y: &Bar) { y.go(); }
''',
    }))
    assert {("src/c.rs:run", "src/a.rs:Foo.new"), ("src/c.rs:run", "src/a.rs:Foo.go"),
            ("src/c.rs:run2", "src/a.rs:Foo.go")} <= calls
    assert not {c for c in calls if c[1].startswith("src/b.rs:")}, calls


def test_rust_a_glob_import_binds_only_a_type_its_module_declares(tmp_path):
    calls = _calls(_edges(tmp_path, {
        "Cargo.toml": '[package]\nname = "demo"\nversion = "0.1.0"\n',
        "src/lib.rs": "pub mod cmd; pub mod proc_; pub mod job;\n",
        "src/cmd.rs": "use std::process::Command;\npub mod tests;\npub fn x() {}\n",
        "src/cmd/tests.rs": 'use super::*;\npub fn t() { let c = Command::new("ls"); c.status(); }\n',
        "src/proc_.rs": '''
pub struct Command;
impl Command { pub fn new(s: &str) -> Self { Command } pub fn status(&self) {} }
pub mod tests;
''',
        "src/proc_/tests.rs": 'use super::*;\npub fn t2() { let c = Command::new("ls"); c.status(); }\n',
        "src/job.rs": 'use crate::proc_::*;\npub fn t3() { let c = Command::new("x"); }\n',
    }))
    assert not {c for c in calls if c[0] == "src/cmd/tests.rs:t"}, calls
    assert {("src/proc_/tests.rs:t2", "src/proc_.rs:Command.new"),
            ("src/proc_/tests.rs:t2", "src/proc_.rs:Command.status"),
            ("src/job.rs:t3", "src/proc_.rs:Command.new")} <= calls


def test_rust_a_type_two_inline_modules_declare_binds_nothing_in_file(tmp_path):
    calls = _calls(_edges(tmp_path, {
        "Cargo.toml": '[package]\nname = "demo"\nversion = "0.1.0"\n',
        "src/lib.rs": '''
mod one { pub struct T; impl T { pub fn go(&self) {} } }
mod two { pub struct T; impl T { pub fn go(&self) {} } }
pub fn f(t: &two::T) { t.go(); }
''',
    }))
    assert not {c for c in calls if c[0] == "src/lib.rs:f"}, calls


def test_go_type_parameters_and_local_types_are_no_package_types(tmp_path):
    calls = _calls(_edges(tmp_path, {
        "q/types.go": "package q\n\ntype T struct{}\n\nfunc (t *T) Go() {}\n",
        "q/use.go": '''package q

type Inner struct{}

func (Inner) Go() {}

func Make[T any]() {
	x := new(T)
	x.Go()
}

func Assert[T any](v any) {
	y := v.(T)
	y.Go()
}

func Local() {
	type T struct{ Inner }
	z := T{}
	z.Go()
}

func Plain() {
	w := new(T)
	w.Go()
}
''',
    }))
    assert ("q/use.go:Plain", "q/types.go:T.Go") in calls
    for caller in ("Make", "Assert", "Local"):
        assert ("q/use.go:" + caller, "q/types.go:T.Go") not in calls, caller


def test_go_a_block_local_does_not_type_the_package_variable_outside_its_block(tmp_path):
    fixtures = {
        "p/types.go": '''package p

type A struct{}

func (a *A) Run() {}

type B struct{}

func (b *B) Run() {}

var y = &A{}
''',
        "p/use.go": '''package p

func f() {
	if true {
		y := &B{}
		y.Run()
	}
	y.Run()
}
''',
    }
    edges = _edges(tmp_path, fixtures)
    at = {(s, t): e.get("source_location") for (s, r, t), e in edges.items() if r == "calls"}
    assert at.get(("p/use.go:f", "p/types.go:A.Run")) == "L8"
    assert at.get(("p/use.go:f", "p/types.go:B.Run")) == "L6"


def test_go_a_block_local_in_the_same_file_as_the_package_variable(tmp_path):
    edges = _edges(tmp_path, {"p/all.go": '''package p

type A struct{}

func (a *A) Run() {}

type B struct{}

func (b *B) Run() {}

var y = &A{}

func f() {
	if true {
		y := &B{}
		_ = y
	}
	y.Run()
}
'''})
    assert ("p/all.go:f", "p/all.go:A.Run") in _calls(edges)
    assert ("p/all.go:f", "p/all.go:B.Run") not in _calls(edges)


def test_js_call_on_a_class_object_or_instance_is_not_a_call_of_it(tmp_path):
    edges = _edges(tmp_path, {
        "a.js": '''
class Rpc {
  static call(name) { return name; }
}
const api = {
  call(x) { return x; },
};
function handler() {}
function run() {
  Rpc.call('x');
  api.call(2);
  handler.call(null, 1);
}
module.exports = { run, handler, Rpc };
''',
        "b.js": '''
const { Rpc, handler } = require('./a');
function go() {
  Rpc.call('y');
  handler.call(this, 1);
}
module.exports = { go };
''',
        "c.ts": '''
export class Service {
  call(x: number) { return x; }
}
export const service = new Service();
''',
        "d.ts": '''
import { service } from './c';
export function go2() {
  service.call(1);
}
''',
        "e.js": '''
import transformData from './f.js';
export function dispatch(config) {
  return transformData.call(config, config.data);
}
''',
        "f.js": '''
export default function transformData(fns) { return fns; }
''',
    })
    calls = _calls(edges)
    assert ("a.js:run", "a.js:handler") in calls
    assert ("b.js:go", "a.js:handler") in calls
    assert ("e.js:dispatch", "f.js:transformData") in calls   # an imported function, any first argument
    for bad in (("a.js:run", "a.js:Rpc"), ("a.js:run", "a.js:api"), ("b.js:go", "a.js:Rpc"),
                ("d.ts:go2", "c.ts:service")):
        assert bad not in calls, bad
