"""Access wideners and access transformers against the class files of the classpath (verinoda/accesscheck.py):
every entry with its line, a wrong one absent with the nearest real names, malformed lines, and unknown where
there is nothing to compare with."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jvmfixtures import PUBLIC, STATIC, base_classes, class_bytes, write_jar
from verinoda import accesscheck, cli, jvmclass

SYNTHETIC, FINAL, PRIVATE = 0x1000, 0x0010, 0x0002
LIB = {
    "net/minecraft/entity/Mob": class_bytes("net/minecraft/entity/Mob", methods=(
        ("<init>", "()V", PUBLIC), ("checkSpawnRules", "(Lnet/minecraft/world/Level;I)Z", PRIVATE),
        ("tick", "()V", PUBLIC), ("lambda$tick$0", "(I)V", PRIVATE | STATIC | SYNTHETIC)),
        fields=(("goalSelector", "Lnet/minecraft/entity/GoalSelector;", PRIVATE | FINAL),
                ("xp", "I", PRIVATE))),
    "net/minecraft/entity/Mob$Brain": class_bytes("net/minecraft/entity/Mob$Brain"),
    "net/minecraft/entity/GoalSelector": class_bytes("net/minecraft/entity/GoalSelector"),
    "net/minecraft/world/Level": class_bytes("net/minecraft/world/Level", methods=(
        ("getBlockState", "(I)I", PUBLIC), ("m_46468_", "(I)Z", PUBLIC))),
}

AW = """accessWidener v2 named
# a comment
accessible class net/minecraft/entity/Mob$Brain
accessible method net/minecraft/entity/Mob checkSpawnRules (Lnet/minecraft/world/Level;I)Z
accessible method net/minecraft/entity/Mob checkSpawnRule (Lnet/minecraft/world/Level;I)Z
accessible method net/minecraft/entity/Mob checkSpawnRules (Lnet/minecraft/world/Level;)Z
mutable field net/minecraft/entity/Mob goalSelector Lnet/minecraft/entity/GoalSelector;
accessible field net/minecraft/entity/Mob xp J
transitive-accessible method net/minecraft/entity/Mob lambda$tick$0 (I)V
accessible class net/minecraft/entity/Mobb
accessible class net.minecraft.entity.Mob
mutable method net/minecraft/entity/Mob tick ()V
accessible method net/minecraft/entity/Mob tick
widened class net/minecraft/entity/Mob
accessible method net/minecraft/entity/Mob tick ()X
accessible class java/lang/Thread
accessible class app/Own
"""

AT = """# NeoForge
public net.minecraft.world.Level getBlockState(I)I
public-f net.minecraft.entity.Mob goalSelector
public net.minecraft.entity.Mob goalSelectr
public net.minecraft.world.Level m_46469_(I)Z
public net.minecraft.entity.Mob$Brain
protected net.minecraft.entity.Mob *()
publik net.minecraft.entity.Mob
"""


def _repo(tmp_path: Path, *, config: bool = True) -> Path:
    repo = tmp_path / "proj"
    write_jar(repo / "libs" / "mc.jar", {**base_classes(), **LIB})
    res = repo / "src" / "main" / "resources"
    (res / "META-INF").mkdir(parents=True)
    (res / "gem.accesswidener").write_text(AW, encoding="utf-8")
    (res / "fabric.mod.json").write_text(json.dumps({"id": "gem", "accessWidener": "gem.accesswidener"}),
                                         encoding="utf-8")
    (res / "META-INF" / "accesstransformer.cfg").write_text(AT, encoding="utf-8")
    (repo / "src" / "main" / "java" / "app").mkdir(parents=True)
    (repo / "src" / "main" / "java" / "app" / "Own.java").write_text("package app; class Own {}", encoding="utf-8")
    (repo / ".verinoda").mkdir()
    if config:
        (repo / ".verinoda" / "config.json").write_text(json.dumps({"code_check": {"classpath": ["libs/*.jar"]}}),
                                                        encoding="utf-8")
    return repo


@pytest.fixture(autouse=True)
def _no_machine_jdk(monkeypatch):
    monkeypatch.setattr(jvmclass, "_jdk_homes", lambda: [])


AW_PATH = "src/main/resources/gem.accesswidener"
AT_PATH = "src/main/resources/META-INF/accesstransformer.cfg"


def _by_line(res: dict, path: str) -> dict[int, dict]:
    return {int(r["at"].rpartition(":")[2]): r for r in res["entries"] if r["at"].startswith(path + ":")}


def test_the_files_are_found_by_name_and_by_the_mod_manifest(tmp_path):
    repo = _repo(tmp_path)
    assert accesscheck.access_files(repo) == [(AT_PATH, "accesstransformer"), (AW_PATH, "accesswidener")]
    odd = repo / "src" / "main" / "resources" / "gem.aw"
    odd.write_text("accessWidener v1 named\n", encoding="utf-8")
    (repo / "src" / "main" / "resources" / "fabric.mod.json").write_text(
        json.dumps({"id": "gem", "accessWidener": "gem.aw"}), encoding="utf-8")
    from verinoda import snapshot

    snapshot._LISTED.clear()
    assert ("src/main/resources/gem.aw", "accesswidener") in accesscheck.access_files(repo)


def test_class_members_reads_synthetic_members_with_their_descriptors():
    got = jvmclass.class_members(LIB["net/minecraft/entity/Mob"])
    assert got["name"] == "net/minecraft/entity/Mob"
    lam = next(m for m in got["methods"] if m[0] == "lambda$tick$0")
    assert lam[1] == "(I)V" and lam[2] & SYNTHETIC
    assert ["xp", "I", PRIVATE] in got["fields"]
    # parse_class still reads the same file after the constant pool was split out
    assert "tick" in jvmclass.parse_class(LIB["net/minecraft/entity/Mob"])["methods"]


def test_widener_entries_against_the_bytecode(tmp_path):
    repo = _repo(tmp_path)
    res = accesscheck.lookup(repo)
    aw = _by_line(res, AW_PATH)
    assert aw[3]["verdict"] == "exists" and aw[3]["evidence"] == "mc.jar!net/minecraft/entity/Mob$Brain.class"
    assert aw[4]["verdict"] == "exists" and aw[4]["status"] == "statically_verified"
    # a misspelt method: absent, with its line and the nearest real one
    # (strong_inference: the classpath is what the last build resolved, maybe older than the build file)
    assert aw[5]["verdict"] == "absent" and aw[5]["status"] == "strong_inference"
    assert aw[5]["nearest"] == ["checkSpawnRules(Lnet/minecraft/world/Level;I)Z"]
    # the right name with another descriptor: the real descriptor is named
    assert aw[6]["verdict"] == "absent" and "(Lnet/minecraft/world/Level;I)Z" in aw[6]["why"]
    assert aw[7]["verdict"] == "exists"
    assert aw[8]["verdict"] == "absent" and aw[8]["nearest"] == ["xp I"]
    assert aw[9]["verdict"] == "exists"                      # a synthetic lambda is read too
    assert aw[10]["verdict"] == "absent" and aw[10]["status"] == "strong_inference"
    assert aw[10]["nearest"][0] == "net/minecraft/entity/Mob"
    for line, words in ((11, "'/'"), (12, "fields only"), (13, "has 4 words"),
                        (14, "unknown access"), (15, "not a method descriptor")):
        assert aw[line]["verdict"] == "malformed", line
        assert words in aw[line]["why"], (line, aw[line]["why"])
    assert aw[16]["verdict"] == "unknown" and "JDK" in aw[16]["why"]
    assert aw[17]["verdict"] == "unknown" and "project's own sources" in aw[17]["why"]


def test_transformer_entries_against_the_bytecode(tmp_path):
    repo = _repo(tmp_path)
    at = _by_line(accesscheck.lookup(repo), AT_PATH)
    assert at[2]["verdict"] == "exists" and at[2]["kind"] == "method"
    assert at[3]["verdict"] == "exists" and at[3]["kind"] == "field"
    assert at[4]["verdict"] == "absent" and at[4]["nearest"] == ["goalSelector Lnet/minecraft/entity/GoalSelector;"]
    # an SRG name the dev classpath does not carry is not called wrong
    assert at[5]["verdict"] == "unknown" and "SRG" in at[5]["why"]
    assert at[6]["verdict"] == "exists" and at[6]["class"] == "net/minecraft/entity/Mob$Brain"
    assert at[7]["verdict"] == "exists"                      # *(): every method, the class is checked
    assert at[8]["verdict"] == "malformed" and "publik" in at[8]["why"]


def test_without_a_complete_classpath_a_missing_class_is_unknown(tmp_path):
    repo = _repo(tmp_path, config=False)
    res = accesscheck.lookup(repo)
    assert res["builds"][0]["complete"] is False
    aw = _by_line(res, AW_PATH)
    assert aw[4]["verdict"] == "unknown" and "not complete" in aw[4]["why"]
    assert aw[11]["verdict"] == "malformed"                   # a syntax error needs no classpath
    assert any("no classpath" in n for n in res["notes"])


def test_another_namespace_and_a_missing_header(tmp_path):
    repo = _repo(tmp_path)
    (repo / "src" / "main" / "resources" / "gem.accesswidener").write_text(
        "accessWidener v2 intermediary\naccessible class net/minecraft/class_1308\n", encoding="utf-8")
    (repo / "src" / "main" / "resources" / "other.accesswidener").write_text(
        "accessible class net/minecraft/entity/Mob\n", encoding="utf-8")
    from verinoda import snapshot

    snapshot._LISTED.clear()
    res = accesscheck.lookup(repo)
    aw = _by_line(res, AW_PATH)
    assert aw[2]["verdict"] == "unknown" and "intermediary" in aw[2]["why"]
    other = _by_line(res, "src/main/resources/other.accesswidener")
    assert other[1]["verdict"] == "malformed" and "header" in other[1]["why"]


def test_header_rules_and_transformer_member_mistakes(tmp_path):
    repo = _repo(tmp_path)
    res_dir = repo / "src" / "main" / "resources"
    (res_dir / "gem.accesswidener").write_text(
        "# the header is not first\naccessWidener v2 named\naccessible class net/minecraft/entity/Mob\n",
        encoding="utf-8")
    (res_dir / "old.accesswidener").write_text(
        "accessWidener v1 named\ntransitive-accessible class net/minecraft/entity/Mob\n"
        "accessible class org/other/Thing\n", encoding="utf-8")
    (res_dir / "META-INF" / "accesstransformer.cfg").write_text(
        "public net.minecraft.entity.Mob <init>\npublic net.minecraft.entity.Mob tick\n", encoding="utf-8")
    from verinoda import snapshot

    snapshot._LISTED.clear()
    res = accesscheck.lookup(repo)
    aw = _by_line(res, AW_PATH)
    # the loader reads the header from the first line only: one malformed entry, the rules are not checked
    assert list(aw) == [1] and aw[1]["verdict"] == "malformed" and "line 2" in aw[1]["why"]
    old = _by_line(res, "src/main/resources/old.accesswidener")
    assert old[2]["verdict"] == "malformed" and "v2" in old[2]["why"]
    # a package no jar of the classpath has: the jar is missing, not the name wrong
    assert old[3]["verdict"] == "unknown" and "org/other/" in old[3]["why"]
    at = _by_line(res, AT_PATH)
    assert at[1]["verdict"] == "malformed" and "<init>(...)V" in at[1]["why"]
    assert at[2]["verdict"] == "absent" and at[2]["nearest"] == ["tick()V"]


def test_a_manifest_name_in_another_case_is_noted(tmp_path):
    repo = _repo(tmp_path)
    (repo / "src" / "main" / "resources" / "fabric.mod.json").write_text(
        json.dumps({"id": "gem", "accessWidener": "Gem.accesswidener"}), encoding="utf-8")
    from verinoda import snapshot

    snapshot._LISTED.clear()
    res = accesscheck.lookup(repo)
    assert any("Gem.accesswidener" in n and "case-sensitive" in n for n in res["notes"])


def test_cli(tmp_path, capsys):
    repo = _repo(tmp_path)
    assert cli.main(["access-check", "--repo", str(repo)]) == 3
    out = capsys.readouterr().out
    assert f"absent [strong_inference] {AW_PATH}:5" in out
    assert "nearest: checkSpawnRules(Lnet/minecraft/world/Level;I)Z" in out
    assert "classpath of .: config, complete, 1 jar(s)" in out
    assert cli.main(["access-check", "--repo", str(repo), "--json", AT_PATH]) == 3
    data = json.loads(capsys.readouterr().out)
    assert [f["path"] for f in data["files"]] == [AT_PATH] and data["counts"]["absent"] == 1
    clean = repo / "clean.accesswidener"
    clean.write_text("accessWidener v2 named\naccessible field net/minecraft/entity/Mob xp I\n", encoding="utf-8")
    assert cli.main(["access-check", "--repo", str(repo), str(clean)]) == 0
    capsys.readouterr()
    unknown = repo / "unknown.accesswidener"
    unknown.write_text("accessWidener v2 named\naccessible class java/lang/Thread\n", encoding="utf-8")
    assert cli.main(["access-check", "--repo", str(repo), str(unknown)]) == 4


def test_no_files(tmp_path, capsys):
    (tmp_path / "p").mkdir()
    assert cli.main(["access-check", "--repo", str(tmp_path / "p")]) == 2
    assert "no .accesswidener" in capsys.readouterr().out
