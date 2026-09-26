"""The import check for TypeScript and JavaScript (verinoda/codecheck_ts.py, docs/DESIGN.md D46): files,
packages in node_modules (types, exports maps, @types, CommonJS), declared modules and tsconfig paths."""

from __future__ import annotations

import json
from pathlib import Path

from verinoda import codecheck


def _w(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "web"
    nm = repo / "node_modules"
    # an ES package with an exports map and a types condition, re-exporting from a .cjs beside its .d.cts
    _w(nm / "ui" / "package.json", json.dumps({"name": "ui", "exports": {".": {"types": "./index.d.ts",
                                                                                 "import": "./index.js"}}}))
    _w(nm / "ui" / "index.d.ts", 'export * from "./parts.cjs";\nexport declare function Button(): void;\n'
                                 "export default function ui(): void;\n")
    _w(nm / "ui" / "parts.cjs", "module.exports = {};\n")
    _w(nm / "ui" / "parts.d.cts", "export declare const Card: number;\nexport interface Props {}\n")
    # a JavaScript package typed by @types, with `export =` of a namespace
    _w(nm / "strutil" / "package.json", json.dumps({"name": "strutil", "main": "util.js"}))
    _w(nm / "strutil" / "util.js", "module.exports = require('./impl');\n")
    _w(nm / "@types" / "strutil" / "index.d.ts", "declare namespace util { function pad(s: string): string; }\n"
                                               "export = util;\n")
    # a CommonJS package without types
    _w(nm / "cjs" / "package.json", json.dumps({"name": "cjs", "main": "main.js"}))
    _w(nm / "cjs" / "main.js", "module.exports = { alpha, beta: 2 };\n")
    # a declared module, as @types/node declares its built-ins
    _w(nm / "@types" / "node" / "fs.d.ts", 'declare module "node:fs" {\n  function readFileSync(p: string): string;\n'
                                           '}\ndeclare module "fs" {\n  export * from "node:fs";\n}\n')
    _w(repo / "tsconfig.json", '{\n  // paths\n  "compilerOptions": {"baseUrl": ".", "paths": {"@app/*": ["src/*"]}},\n}\n')
    _w(repo / "src" / "lib" / "format.ts", "export function price(n: number) { return n; }\nexport const EUR = 1;\n")
    _w(repo / "src" / "lib" / "index.ts", 'export * from "./format";\n')
    _w(repo / "src" / "main.ts",
       'import { Button, Card, Buton } from "ui";\n'          # 1
       'import ui from "ui";\n'                                # 2
       'import { pad, padd } from "strutil";\n'                  # 3
       'import { alpha, gamma } from "cjs";\n'                # 4
       'import { readFileSync, readFileSinc } from "fs";\n'   # 5
       'import { price, prize } from "./lib/format";\n'       # 6
       'import { EUR } from "./lib";\n'                       # 7
       'import { price as p2 } from "@app/lib/format";\n'     # 8
       'import { x } from "./lib/missing";\n'                 # 9
       'import left from "left-pad";\n'                       # 10
       'import thing from "@/alias/thing";\n'                 # 11
       'import def from "./lib/format";\n'                    # 12
       'export { price as cost } from "./lib/format";\n'      # 13
       'const { EUR: e2 } = require("./lib/format");\n')      # 14
    return repo


def _sites(res: dict) -> dict[tuple[int, str], str]:
    return {(s["line"], s["name"]): s["verdict"] for s in res["sites"]}


def test_names_imported_from_files_and_packages(tmp_path):
    res = codecheck.check(_repo(tmp_path), ["src/main.ts"], env="none", include_exists=True)
    s = _sites(res)
    assert s[(1, "Button")] == "exists" and s[(1, "Card")] == "exists"   # export * from ./parts.cjs -> .d.cts
    assert s[(1, "Buton")] == "absent"
    assert s[(2, "default")] == "exists"
    assert s[(3, "pad")] == "exists" and s[(3, "padd")] == "absent"      # @types: export = a namespace
    assert s[(4, "alpha")] == "exists" and s[(4, "gamma")] == "unknown"  # CommonJS: never absent
    assert s[(5, "readFileSync")] == "exists" and s[(5, "readFileSinc")] == "absent"  # declared modules
    assert s[(6, "price")] == "exists" and s[(6, "prize")] == "absent"
    assert s[(7, "EUR")] == "exists" and s[(8, "price")] == "exists"     # export *, tsconfig paths
    assert s[(9, "./lib/missing")] == "absent"
    assert s[(10, "left-pad")] == "not_installed"
    assert s[(11, "@/alias/thing")] == "unknown"
    assert s[(12, "default")] == "absent"                                 # the project's own module: no default
    assert s[(13, "price")] == "exists" and s[(14, "EUR")] == "exists"
    by = {(x["line"], x["name"]): x for x in res["sites"]}
    assert by[(1, "Buton")]["nearest"][0]["name"] == "Button" and by[(1, "Buton")]["language"] == "TypeScript"
    assert res["exit"] == 3


def test_without_node_modules_packages_are_unknown(tmp_path):
    repo = _repo(tmp_path)
    import shutil

    shutil.rmtree(repo / "node_modules")
    s = _sites(codecheck.check(repo, ["src/main.ts"], env="none", include_exists=True))
    assert s[(1, "ui")] == "unknown" and s[(10, "left-pad")] == "unknown"
    assert s[(6, "prize")] == "absent"   # the project's own files are still read


def test_a_javascript_snippet(tmp_path):
    repo = _repo(tmp_path)
    res = codecheck.check(repo, snippet='import { price, pricee } from "./lib/format.js";\n', as_path="src/new.mjs",
                          env="none")
    assert [x["name"] for x in res["sites"] if x["verdict"] == "absent"] == ["pricee"]
    assert res["sites"][0]["language"] == "JavaScript"
