"""Local changes to the vendored graph extractor (verinoda/project_index, docs/UPSTREAM.md "Modified"):
the C# placeholder index, member calls that no longer bind to a same-named function of the file, vendored,
minified and generated files kept to a file node, more C/C++ header suffixes and ``#include`` evidence."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

import random  # noqa: E402

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
