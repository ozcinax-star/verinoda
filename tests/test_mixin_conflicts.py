"""Mixins of several mods on the same target method (verinoda/mixinconflicts.py): the project's Mixins read from
its sources, other mods' from the class files of their jars (written here, with their configs and manifests);
each pair a conflict, order-dependent or compatible with both Mixins, and a Mixin failure in a log named with its
mod through the config that lists it."""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

import pytest

from jvmfixtures import PUBLIC, class_bytes
from verinoda import cli, jvmclass, mixinconflicts

MIXIN = "Lorg/spongepowered/asm/mixin/Mixin;"
OVERWRITE = "Lorg/spongepowered/asm/mixin/Overwrite;"
INJ = "Lorg/spongepowered/asm/mixin/injection/"
AT = INJ + "At;"
LE = "net/minecraft/entity/LivingEntity"
DAMAGE = "damage(Lnet/minecraft/entity/damage/DamageSource;F)Z"
SET_TARGET = "Lnet/minecraft/entity/mob/MobEntity;setTarget(Lnet/minecraft/entity/LivingEntity;)V"

SOURCE = """package com.example.mymod.mixin;

import net.minecraft.entity.LivingEntity;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

@Mixin(value = LivingEntity.class, priority = 900)
public abstract class TickMixin {
    private static final String DAMAGE = "damage(Lnet/minecraft/entity/damage/DamageSource;F)Z";

    @Inject(method = "tick", at = @At(value = "INVOKE", target = "Lnet/minecraft/entity/LivingEntity;tickMovement()V"))
    private void mymod$tick(CallbackInfo ci) {}

    @Inject(method = DAMAGE, at = @At("HEAD"), cancellable = true)
    private void mymod$damage(CallbackInfo ci) {}

    @Inject(method = "jump", at = @At("TAIL"))
    private void mymod$jump(CallbackInfo ci) {}
}
"""
SOURCE_PATH = "src/main/java/com/example/mymod/mixin/TickMixin.java"


@pytest.fixture(autouse=True)
def _no_machine_jdk(monkeypatch):
    monkeypatch.setattr(jvmclass, "_jdk_homes", lambda: [])


def at(value: str, target: str | None = None, ordinal: int | None = None) -> tuple:
    vals: dict = {"value": value}
    if target:
        vals["target"] = target
    if ordinal is not None:
        vals["ordinal"] = ordinal
    return (AT, vals)


def mixin_class(name: str, target: str, methods: dict[str, tuple[str, list]], priority: int | None = None) -> bytes:
    """A compiled Mixin: ``methods`` maps a handler name to (its descriptor, its annotations)."""
    vals: dict = {"value": [("class", f"L{target};")]}
    if priority is not None:
        vals["priority"] = priority
    return class_bytes(name, methods=tuple((m, d, PUBLIC) for m, (d, _a) in methods.items()),
                       annotations=((MIXIN, vals),),
                       method_annotations={m: a for m, (_d, a) in methods.items()})


def jar_bytes(mod_id: str, mixins: dict[str, bytes], *, package: str, loader: str = "fabric",
              refmap: dict | None = None, nested: dict[str, bytes] | None = None,
              plugin: str | None = None) -> bytes:
    cfg_name = f"{mod_id}.mixins.json"
    cfg: dict = {"package": package, "mixins": [n.rsplit("/", 1)[-1] for n in mixins]}
    if plugin:
        cfg["plugin"] = plugin
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        if loader == "fabric":
            z.writestr("fabric.mod.json", json.dumps({"id": mod_id, "version": "1.0", "mixins": [cfg_name]},
                                                     indent=2))
        else:
            z.writestr("META-INF/mods.toml", f'modLoader="javafml"\n[[mods]]\nmodId="{mod_id}"\n'
                                             f'[[mixins]]\nconfig="{cfg_name}"\n')
        if refmap is not None:
            cfg["refmap"] = f"{mod_id}-refmap.json"
            z.writestr(cfg["refmap"], json.dumps({"mappings": refmap}))
        z.writestr(cfg_name, json.dumps(cfg))
        for name, data in mixins.items():
            z.writestr(name + ".class", data)
        for name, data in (nested or {}).items():
            z.writestr(f"META-INF/jars/{name}", data)
    return buf.getvalue()


def write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def project(tmp_path: Path) -> Path:
    repo = tmp_path / "proj"
    (repo / SOURCE_PATH).parent.mkdir(parents=True)
    (repo / SOURCE_PATH).write_text(SOURCE, encoding="utf-8")
    res = repo / "src" / "main" / "resources"
    res.mkdir(parents=True)
    (res / "fabric.mod.json").write_text(json.dumps({"id": "mymod", "mixins": ["mymod.mixins.json"]}, indent=2),
                                         encoding="utf-8")
    (res / "mymod.mixins.json").write_text(json.dumps({"package": "com.example.mymod.mixin",
                                                       "mixins": ["TickMixin"]}), encoding="utf-8")
    (repo / ".verinoda").mkdir()
    return repo


def other_mod() -> bytes:
    """``othermod``: an @Overwrite of LivingEntity.tick and a plain @Inject at the head of damage."""
    name = "com/other/mixin/OtherMixin"
    return jar_bytes("othermod", {name: mixin_class(name, LE, {
        "tick": ("()V", [(OVERWRITE, {})]),
        "other$damage": ("(Lorg/spongepowered/asm/mixin/injection/callback/CallbackInfoReturnable;)V",
                         [(INJ + "Inject;", {"method": [DAMAGE], "at": [at("HEAD")]})]),
        "other$jump": ("()V", [(INJ + "Inject;", {"method": ["jump"], "at": [at("HEAD")]})]),
    })}, package="com.other.mixin")


def by_target(rows: list[dict], target: str) -> list[dict]:
    return [r for r in rows if r["target"].startswith(target)]


def test_class_annotations_reads_every_value_kind():
    b = class_bytes("a/B", methods=(("m", "()V", PUBLIC),), annotations=((MIXIN, {
        "value": [("class", f"L{LE};")], "priority": 1500, "remap": False, "f": 0.5,
        "e": ("enum", "La/E;", "X"), "nested": (AT, {"value": "HEAD"})}),),
        method_annotations={"m": [(INJ + "Inject;", {"method": ["tick", "jump"], "cancellable": True})]})
    got = jvmclass.class_annotations(b)
    assert got["name"] == "a/B"
    assert got["annotations"] == [{"type": MIXIN, "values": {
        "value": [{"class": f"L{LE};"}], "priority": 1500, "remap": False, "f": 0.5, "e": "X",
        "nested": {"type": AT, "values": {"value": "HEAD"}}}}]
    assert got["methods"] == [["m", "()V", [{"type": INJ + "Inject;",
                                              "values": {"method": ["tick", "jump"], "cancellable": True}}]]]
    assert jvmclass.class_annotations(b"not a class") is None


def test_overwrite_against_an_inject_is_a_conflict_with_both_mixins(tmp_path):
    repo = project(tmp_path)
    write(repo / "run" / "mods" / "othermod-1.0.jar", other_mod())
    res = mixinconflicts.lookup(repo)
    assert res["status"] == "found" and res["project_mixins"] == 1 and res["other_mods"] == 1
    [tick] = by_target(res["conflicts"], "net.minecraft.entity.LivingEntity.tick")
    assert tick["severity"] == "conflict" and tick["status"] == "strong_inference"
    assert tick["target"] == "net.minecraft.entity.LivingEntity.tick()V"
    assert tick["same_target"] == "strong_inference" and "every overload" in tick["same_target_why"]
    assert "@Overwrite replaces the body" in tick["why"] and "may not be in the new body" in tick["why"]
    mine, theirs = sorted(tick["mixins"], key=lambda s: s["mod"])
    assert mine["mod"] == "mymod" and mine["evidence"] == f"{SOURCE_PATH}:13"
    assert mine["mixin"] == "com.example.mymod.mixin.TickMixin" and mine["member"] == "mymod$tick"
    assert mine["priority"] == 900 and mine["priority_from"] == "@Mixin"
    assert mine["mod_evidence"] == "src/main/resources/fabric.mod.json:2" and mine["config"] == "mymod.mixins.json"
    assert theirs["mod"] == "othermod" and theirs["kind"] == "@Overwrite" and theirs["priority"] == 1000
    assert theirs["evidence"] == "othermod-1.0.jar!/com/other/mixin/OtherMixin.class"
    assert theirs["mod_evidence"] == "othermod-1.0.jar!/fabric.mod.json:2"
    assert "mods folder" in theirs["source"]
    # a cancellable @Inject at HEAD beside another mod's HEAD inject: both apply, the order decides
    [dmg] = by_target(res["conflicts"], "net.minecraft.entity.LivingEntity.damage")
    assert dmg["severity"] == "order_dependent" and "mymod can cancel" in dmg["why"]
    assert dmg["same_target"] == "statically_verified"
    # HEAD and TAIL injects into jump coexist: one shared row, not a conflict
    [jump] = res["shared"]
    assert jump["target"] == "net.minecraft.entity.LivingEntity.jump" and jump["mods"] == ["mymod", "othermod"]
    assert jump["severity"] == "compatible" and jump["status"] == "strong_inference"
    assert res["counts"] == {"conflict": 1, "order_dependent": 1, "compatible": 1, "failures": 0, "unknown": 0}
    assert mixinconflicts.exit_code(res) == 3


def redirect_mod(mod_id: str, target: str = SET_TARGET, *, loader: str = "fabric", kind: str = "Redirect",
                 extra: dict | None = None, priority: int | None = None, plugin: str | None = None) -> bytes:
    name = f"com/{mod_id}/mixin/{mod_id.capitalize()}Mixin"
    vals = {"method": [DAMAGE], "at": at("INVOKE", target), **(extra or {})}
    return jar_bytes(mod_id, {name: mixin_class(name, LE, {f"{mod_id}$redirect": ("()V", [(INJ + f"{kind};", vals)])},
                                                priority=priority)},
                     package=f"com.{mod_id}.mixin", loader=loader, plugin=plugin)


def test_two_redirects_of_one_call_conflict_and_a_modify_arg_on_it_is_order_dependent(tmp_path):
    repo = project(tmp_path)
    mods = tmp_path / "mods"
    write(mods / "alpha.jar", redirect_mod("alpha", priority=1100))
    write(mods / "beta.jar", redirect_mod("beta", plugin="com.beta.BetaPlugin"))
    write(mods / "gamma.jar", redirect_mod("gamma", kind="ModifyArg", extra={"index": 0}))
    write(mods / "delta.jar", redirect_mod("delta", "Lnet/minecraft/entity/Entity;discard()V"))   # another call
    wrap = "Lcom/llamalad7/mixinextras/injector/wrapoperation/WrapOperation;"
    write(mods / "wrapper.jar", jar_bytes("wrapper", {"com/w/WMixin": mixin_class("com/w/WMixin", LE, {
        "w$wrap": ("()V", [(wrap, {"method": [DAMAGE], "at": at("INVOKE", SET_TARGET)})])})}, package="com.w"))
    write(mods / "forgy.jar", redirect_mod("forgy", loader="forge"))   # never loads beside Fabric mods
    res = mixinconflicts.lookup(repo, with_paths=[str(mods)])
    rows = by_target(res["conflicts"], "net.minecraft.entity.LivingEntity.damage")
    pairs = {tuple(sorted(s["mod"] for s in r["mixins"])): r for r in rows}
    red = pairs[("alpha", "beta")]
    assert red["severity"] == "conflict" and "'@Redirect conflict'" in red["why"]
    assert "alpha 1100" in red["why"] and "beta 1000" in red["why"]
    assert red["same_target"] == "statically_verified"
    assert {s["evidence"] for s in red["mixins"]} == {"alpha.jar!/com/alpha/mixin/AlphaMixin.class",
                                                      "beta.jar!/com/beta/mixin/BetaMixin.class"}
    assert all(s["at"] == [f"@At(INVOKE, {SET_TARGET})"] for s in red["mixins"])
    assert pairs[("alpha", "gamma")]["severity"] == "order_dependent"
    assert "@Redirect replaces the" in pairs[("alpha", "gamma")]["why"]
    # delta redirects another call, forgy is a Forge mod, mymod's @Inject at HEAD touches no call: no clash
    # and MixinExtras' @WrapOperation chains onto the redirected call
    assert set(pairs) == {("alpha", "beta"), ("alpha", "gamma"), ("beta", "gamma")}
    assert "com.beta.BetaPlugin" in red["plugin_note"] and "may skip this one" in red["plugin_note"]
    assert "plugin_note" not in pairs[("alpha", "gamma")]


def test_compatible_injects_are_one_shared_row_and_exit_0(tmp_path, capsys):
    repo = tmp_path / "proj"
    repo.mkdir()
    name_a, name_b = "com/a/mixin/AMixin", "com/b/mixin/BMixin"
    write(repo / "mods" / "a.jar", jar_bytes("a", {name_a: mixin_class(name_a, LE, {
        "a$tick": ("()V", [(INJ + "Inject;", {"method": ["tick()V"], "at": [at("HEAD")]})])})}, package="com.a.mixin"))
    write(repo / "mods" / "b.jar", jar_bytes("b", {name_b: mixin_class(name_b, LE, {
        "b$tick": ("()V", [(INJ + "Inject;", {"method": ["tick()V"], "at": [at("RETURN")]}),
                           ("Lcom/llamalad7/mixinextras/injector/wrapoperation/WrapOperation;",
                            {"method": ["tick()V"], "at": at("INVOKE", SET_TARGET)})])})},
        package="com.b.mixin"))
    res = mixinconflicts.lookup(repo)
    assert res["conflicts"] == [] and len(res["shared"]) == 1
    row = res["shared"][0]
    assert row["target"] == "net.minecraft.entity.LivingEntity.tick" and row["mods"] == ["a", "b"]
    assert {s["kind"] for s in row["mixins"]} == {"@Inject", "@WrapOperation"}
    assert mixinconflicts.exit_code(res) == 0
    assert cli.main(["mixin-check", "--repo", str(repo), "--conflicts"]) == 0
    out = capsys.readouterr().out
    assert "0 conflict(s), 0 order-dependent, 1 shared target(s)" in out
    assert "compatible [strong_inference] net.minecraft.entity.LivingEntity.tick  (a, b)" in out


def test_refmap_maps_production_names_and_nested_jars_are_mods(tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    inter = "net/minecraft/class_1309"
    sel = "Lnet/minecraft/class_1309;method_5773()V"
    name_a, name_b = "com/a/mixin/AMixin", "com/b/mixin/BMixin"
    a = jar_bytes("a", {name_a: mixin_class(name_a, inter, {"method_5773": ("()V", [(OVERWRITE, {})])})},
                  package="com.a.mixin")
    inner = jar_bytes("fabric-b-v1", {name_b: mixin_class(name_b, inter, {
        "b$t": ("()V", [(INJ + "Inject;", {"method": ["tick"], "at": [at("HEAD")]})])})}, package="com.b.mixin",
        refmap={name_b: {"tick": sel}})
    write(repo / "mods" / "a.jar", a)
    write(repo / "mods" / "api.jar", jar_bytes("api", {}, package="x", nested={"fabric-b-v1.jar": inner}))
    res = mixinconflicts.lookup(repo)
    [row] = res["conflicts"]
    assert row["target"] == "net.minecraft.class_1309.method_5773()V" and row["severity"] == "conflict"
    b_side = next(s for s in row["mixins"] if s["mod"] == "fabric-b-v1")
    assert b_side["selector"] == "tick" and b_side["selector_refmap"] == sel
    assert b_side["evidence"] == "api.jar!/META-INF/jars/fabric-b-v1.jar!/com/b/mixin/BMixin.class"


LOG = """[12:00:01] [main/INFO]: Loading 3 mods
[12:00:02] [main/WARN] (mixin): @Redirect conflict. Skipping beta.mixins.json:BetaMixin->@Redirect::beta$redirect()V with priority 1000, already redirected by alpha.mixins.json:AlphaMixin->@Redirect::alpha$redirect()V with priority 1100
[12:00:03] [main/ERROR] (mixin): Mixin [gamma.mixins.json:GammaMixin] from phase [DEFAULT] in config [gamma.mixins.json] FAILED during APPLY
org.spongepowered.asm.mixin.injection.throwables.InvalidInjectionException: Critical injection failure: @ModifyArg annotation on gamma$redirect could not find any targets matching 'damage' in net/minecraft/entity/LivingEntity. Using refmap gamma-refmap.json [PREINJECT Applicator Phase -> gamma.mixins.json:GammaMixin from mod gamma -> Prepare Injections]
[12:00:04] [main/ERROR] (mixin): Mixin apply for mod lost failed lost.mixins.json:LostMixin from mod lost -> net.minecraft.class_1
[12:00:05] [main/ERROR] (mixin): Mixin [ghost.mixins.json:GhostMixin] from phase [DEFAULT] in config [ghost.mixins.json] FAILED during APPLY
"""


def test_a_failed_injection_in_the_log_is_named_with_its_mod(tmp_path, capsys):
    repo = project(tmp_path)
    mods = tmp_path / "mods"
    write(mods / "alpha.jar", redirect_mod("alpha", priority=1100))
    write(mods / "beta.jar", redirect_mod("beta"))
    write(mods / "gamma.jar", redirect_mod("gamma", kind="ModifyArg", extra={"index": 0}))
    log = tmp_path / "latest.log"
    log.write_text(LOG, encoding="utf-8")
    res = mixinconflicts.lookup(repo, with_paths=[str(mods)], log=log)
    fails = {f["config"]: f for f in res["failures"]}
    beta = fails["beta.mixins.json"]
    assert beta["what"] == "redirect_conflict" and beta["status"] == "observed" and beta["log_line"] == 2
    assert beta["mixin"] == "com.beta.mixin.BetaMixin" and beta["mod"] == "beta"
    assert beta["mod_status"] == "statically_verified" and "beta.jar!/fabric.mod.json:2" in beta["mod_evidence"]
    assert beta["evidence"].startswith("latest.log:2: @Redirect conflict. Skipping beta.mixins.json:BetaMixin")
    assert beta["other"]["mod"] == "alpha" and beta["other"]["mixin"] == "com.alpha.mixin.AlphaMixin"
    assert beta["injector"] == "@Redirect" and beta["handler"] == "beta$redirect"
    assert any(set(c["mods"]) == {"alpha", "beta"} and c["severity"] == "conflict" for c in beta["likely_cause"])
    gamma = fails["gamma.mixins.json"]
    assert gamma["log_line"] == 3 and gamma["lines"] == 2 and gamma["mod"] == "gamma"
    assert gamma["injector"] == "@ModifyArg" and gamma["handler"] == "gamma$redirect"
    assert gamma["target"] == "net.minecraft.entity.LivingEntity" and gamma["selector"] == "damage"
    lost = fails["lost.mixins.json"]   # in no jar read: the mod the log line names
    assert lost["mod"] == "lost" and lost["mod_status"] == "observed" and lost["mod_evidence"].startswith("latest.log:5")
    ghost = fails["ghost.mixins.json"]
    assert ghost["mod"] is None and ghost["mod_status"] == "unknown" and "--with" in ghost["next"]
    assert res["counts"]["failures"] == 4 and res["counts"]["unknown"] == 1
    assert cli.main(["mixin-check", "--repo", str(repo), "--log", str(log), "--with", str(mods)]) == 3
    out = capsys.readouterr().out
    assert "failed [observed] com.beta.mixin.BetaMixin @Redirect::beta$redirect of beta [statically_verified]" in out
    assert "likely cause [strong_inference]: conflict on" in out


def test_without_mod_jars_conflicts_with_other_mods_are_unknown(tmp_path, capsys):
    repo = project(tmp_path)
    res = mixinconflicts.lookup(repo)
    assert res["status"] == "found" and res["other_mods"] == 0 and res["conflicts"] == [] and res["shared"] == []
    assert any(n.startswith("no mod jar with Mixins found") for n in res["notes"])
    assert mixinconflicts.exit_code(res) == 4
    assert cli.main(["mixin-check", "--repo", str(repo), "--conflicts", "--json"]) == 4
    assert json.loads(capsys.readouterr().out)["mods"][0]["mod"] == "mymod"
    empty = tmp_path / "empty"
    empty.mkdir()
    assert cli.main(["mixin-check", "--repo", str(empty), "--conflicts"]) == 2
    assert "no @Mixin class in the project's sources or in any mod jar read" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        cli.main(["mixin-check", "--repo", str(empty), "--log", str(empty / "missing.log")])


# -- review round: forms the first version misread ----------------------------------------------------------

WILDCARD = """package com.example.mymod.mixin;

import net.minecraft.entity.*;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.Overwrite;

@Mixin(LivingEntity.class)
public abstract class WildMixin {
    @Overwrite
    public void tick() {}
}
"""


def test_wildcard_imports_typeless_slots_and_a_shared_config_name(tmp_path):
    repo = tmp_path / "proj"
    path = repo / "src" / "main" / "java" / "com" / "example" / "mymod" / "mixin" / "WildMixin.java"
    path.parent.mkdir(parents=True)
    path.write_text(WILDCARD, encoding="utf-8")
    # an @Mixin target known only through a wildcard import still meets the jar's LivingEntity
    write(repo / "mods" / "othermod.jar", other_mod())
    # @ModifyArg without an index picks the argument by the handler's type: two of them are not one slot
    write(repo / "mods" / "p.jar", redirect_mod("p", kind="ModifyArg"))
    write(repo / "mods" / "q.jar", redirect_mod("q", kind="ModifyArg"))
    # two mods whose configs have the same name: the log line alone does not tell which
    name = "com/x/mixin/XMixin"
    for mod_id in ("x1", "x2"):
        z = io.BytesIO()
        with zipfile.ZipFile(z, "w") as jar:
            jar.writestr("fabric.mod.json", json.dumps({"id": mod_id, "mixins": ["common.mixins.json"]}))
            jar.writestr("common.mixins.json", json.dumps({"package": "com.x.mixin", "mixins": ["XMixin"]}))
            jar.writestr(name + ".class", mixin_class(name, "net/minecraft/Other", {}))
        write(repo / "mods" / f"{mod_id}.jar", z.getvalue())
    log = tmp_path / "latest.log"
    log.write_text("Mixin [common.mixins.json:XMixin] from phase [DEFAULT] in config [common.mixins.json] FAILED "
                   "during APPLY\n", encoding="utf-8")
    res = mixinconflicts.lookup(repo, log=log)
    [tick] = by_target(res["conflicts"], "net.minecraft.entity.LivingEntity.tick")
    assert {s["mod"] for s in tick["mixins"]} == {"project", "othermod"}
    assert "only one survives" in tick["why"]
    assert not any({s["mod"] for s in r["mixins"]} == {"p", "q"} for r in res["conflicts"])
    [fail] = res["failures"]
    assert fail["mod"] is None and fail["mod_status"] == "unknown" and fail["candidates"] == ["x1", "x2"]


# -- second review round ------------------------------------------------------------------------------------

# Mixin 0.8's MixinInfo prints "config:Class from mod id"
REAL_LOG = """[12:00:02] [main/WARN] (mixin): @Redirect conflict. Skipping beta.mixins.json:BetaMixin from mod beta->@Redirect::beta$redirect()V with priority 1000, already redirected by alpha.mixins.json:AlphaMixin from mod alpha->@Redirect::alpha$redirect()V with priority 1100
[12:00:03] [main/ERROR] (mixin): Mixin [ghost.mixins.json:GhostMixin from mod ghost] from phase [DEFAULT] in config [ghost.mixins.json] FAILED during APPLY
[12:00:04] [main/WARN] (mixin): Method overwrite conflict for tick in ow.mixins.json:OwMixin from mod ow, previously written by com.other.OtherMixin. Skipping method.
[12:00:05] [main/ERROR] (mixin): Mixin apply for mod fabric-x-v1 failed fabric-x-v1.mixins.json:XMixin from mod fabric-x-v1 -> net.minecraft.class_1: java.lang.RuntimeException
"""


def test_real_mixin_log_lines_name_the_mixin_and_its_mod(tmp_path):
    rows = {r["config"]: r for r in mixinconflicts.read_log(REAL_LOG.splitlines())}
    assert [rows[c]["what"] for c in ("beta.mixins.json", "ghost.mixins.json", "ow.mixins.json")] == [
        "redirect_conflict", "apply_failed", "overwrite_conflict"]
    assert rows["beta.mixins.json"]["log_mod"] == "beta" and rows["beta.mixins.json"]["handler"] == "beta$redirect"
    assert rows["beta.mixins.json"]["other"]["log_mod"] == "alpha"
    assert rows["ghost.mixins.json"]["mixin_written"] == "GhostMixin" and rows["ghost.mixins.json"]["log_mod"] == "ghost"
    assert rows["ow.mixins.json"]["other"] == {"mixin_written": "com.other.OtherMixin"}
    assert rows["fabric-x-v1.mixins.json"]["log_mod"] == "fabric-x-v1"
    repo = tmp_path / "proj"
    write(repo / "mods" / "alpha.jar", redirect_mod("alpha", priority=1100))
    write(repo / "mods" / "beta.jar", redirect_mod("beta"))
    log = tmp_path / "latest.log"
    log.write_text(REAL_LOG, encoding="utf-8")
    res = mixinconflicts.lookup(repo, log=log)
    fails = {f["config"]: f for f in res["failures"]}
    beta = fails["beta.mixins.json"]
    assert beta["mod"] == "beta" and beta["mod_status"] == "statically_verified"
    assert beta["other"]["mod"] == "alpha" and beta["other"]["mod_status"] == "statically_verified"
    assert beta["likely_cause"][0]["severity"] == "conflict"
    ghost = fails["ghost.mixins.json"]   # in no jar read: the mod the line names
    assert ghost["mod"] == "ghost" and ghost["mod_status"] == "observed" and ghost["mod_evidence"] == \
        "latest.log:2 names the mod"


def raw_jar(files: dict[str, bytes | str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for n, data in files.items():
            z.writestr(n, data)
    return buf.getvalue()


def test_lenient_manifests_and_unknown_loaders(tmp_path):
    repo = tmp_path / "proj"
    name = "com/a/mixin/AMixin"
    tick = {"tick": ("()V", [(OVERWRITE, {})])}
    # Fabric Loader reads a raw newline inside a string; so does this reader
    fmj = '{\n  "id": "amod",\n  "description": "line one\nline two",\n  "mixins": ["amod.mixins.json"]\n}'
    write(repo / "mods" / "amod-1.0.jar", raw_jar({
        "fabric.mod.json": fmj, "amod.mixins.json": json.dumps({"package": "com.a.mixin", "mixins": ["AMixin"]}),
        name + ".class": mixin_class(name, LE, tick)}))
    write(repo / "mods" / "forgy.jar", jar_bytes("forgy", {"com/f/F": mixin_class("com/f/F", LE, tick)},
                                                 package="com.f", loader="forge"))
    res = mixinconflicts.lookup(repo)
    assert ("amod", "fabric") in [(m["mod"], m["loader"]) for m in res["mods"]]
    assert res["conflicts"] == []     # a Fabric and a Forge mod never load together
    # a jar whose loader is not known (only MANIFEST.MF names its config) pairs, and the row says so
    write(repo / "mods" / "plain.jar", raw_jar({
        "META-INF/MANIFEST.MF": "Manifest-Version: 1.0\nMixinConfigs: plain.mixins.json\n",
        "plain.mixins.json": json.dumps({"package": "com.p", "mixins": ["P"]}),
        "com/p/P.class": mixin_class("com/p/P", LE, tick)}))
    res = mixinconflicts.lookup(repo)
    rows = [r for r in res["conflicts"] if "plain" in {s["mod"] for s in r["mixins"]}]
    assert rows and all("the loader of plain is not known" in r["loader_note"] for r in rows)


def test_malformed_jars_become_notes_never_a_crash(tmp_path, monkeypatch):
    repo = tmp_path / "proj"
    name = "com/a/mixin/AMixin"
    ok_class = mixin_class(name, LE, {"tick": ("()V", [(OVERWRITE, {})])})
    # a damaged class entry (its deflated bytes flipped)
    good = jar_bytes("a", {name: ok_class}, package="com.a.mixin")
    data = bytearray(good)
    info = zipfile.ZipFile(io.BytesIO(good)).getinfo(name + ".class")
    data[info.header_offset + 30 + len(info.filename) + len(info.extra) + 5] ^= 0xFF
    write(repo / "mods" / "a.jar", bytes(data))
    # refmaps of the wrong shape, a config whose class list is a number, a class file that is not one
    for i, maps in enumerate(([1], "x", {name: [1]})):
        write(repo / "mods" / f"r{i}.jar", raw_jar({
            "fabric.mod.json": json.dumps({"id": f"r{i}", "mixins": ["r.mixins.json"]}),
            "r.mixins.json": json.dumps({"package": "com.a.mixin", "mixins": ["AMixin"], "refmap": "r.json"}),
            "r.json": json.dumps({"mappings": maps}), name + ".class": ok_class}))
    write(repo / "mods" / "five.jar", raw_jar({
        "fabric.mod.json": json.dumps({"id": "five", "mixins": ["five.mixins.json"]}),
        "five.mixins.json": json.dumps({"package": "com.f", "mixins": 5, "client": "com.f.Str"})}))
    write(repo / "mods" / "junk.jar", raw_jar({
        "fabric.mod.json": json.dumps({"id": "junk", "mixins": ["junk.mixins.json"]}),
        "junk.mixins.json": json.dumps({"package": "com.j", "mixins": ["J"]}), "com/j/J.class": b"\xca\xfe\xba\xbe"}))
    res = mixinconflicts.lookup(repo)
    notes = "\n".join(res["notes"])
    assert "a.jar!/com/a/mixin/AMixin.class is not readable: BadZipFile" in notes
    assert "r0.jar!/r.json is not a refmap this reader reads" in notes and "r1.jar!/r.json" in notes
    assert "junk.jar!/com/j/J.class is not a class file this reader reads" in notes
    assert {m["mod"]: m["mixins"] for m in res["mods"]}["five"] == 0
    assert any({s["mod"] for s in r["mixins"]} == {"r0", "r1"} for r in res["conflicts"])
    # an entry larger than the cap is skipped with a note, not read
    monkeypatch.setattr(mixinconflicts, "MAX_ENTRY", 10)
    assert "over the 10 read here" in "\n".join(mixinconflicts.lookup(repo)["notes"])
    # annotations nested past the cap make the class file unread, never a RecursionError
    deep: list = []
    cur = deep
    for _ in range(400):
        nxt: list = []
        cur.append(nxt)
        cur = nxt
    assert jvmclass.class_annotations(class_bytes("a/B", annotations=((MIXIN, {"value": deep}),))) is None


def variable_mod(mod_id: str, **vals) -> bytes:
    name = f"com/{mod_id}/M"
    return jar_bytes(mod_id, {name: mixin_class(name, LE, {f"{mod_id}$v": ("(F)F", [(INJ + "ModifyVariable;", {
        "method": [DAMAGE], "at": at("HEAD"), **vals})])})}, package=f"com.{mod_id}")


def test_modify_variable_slots_zero_and_name_arrays(tmp_path):
    repo = tmp_path / "proj"
    write(repo / "mods" / "o1.jar", variable_mod("o1", ordinal=0))
    write(repo / "mods" / "o2.jar", variable_mod("o2", ordinal=0))
    write(repo / "mods" / "n1.jar", variable_mod("n1", name=["amount"]))   # a class file's String[]
    write(repo / "mods" / "n2.jar", variable_mod("n2", name="amount"))
    res = mixinconflicts.lookup(repo)
    pairs = {tuple(sorted(s["mod"] for s in r["mixins"])): r["severity"] for r in res["conflicts"]}
    assert pairs == {("o1", "o2"): "order_dependent", ("n1", "n2"): "order_dependent"}


def test_the_newest_copy_of_a_mod_is_read(tmp_path):
    repo = tmp_path / "proj"

    def lib(version: str, overwrite: bool) -> bytes:
        meths = {"tick": ("()V", [(OVERWRITE, {})])} if overwrite else \
            {"lib$t": ("()V", [(INJ + "Inject;", {"method": ["jump"], "at": [at("HEAD")]})])}
        return raw_jar({"fabric.mod.json": json.dumps({"id": "lib", "version": version, "mixins": ["lib.mixins.json"]}),
                        "lib.mixins.json": json.dumps({"package": "com.lib", "mixins": ["L"]}),
                        "com/lib/L.class": mixin_class("com/lib/L", LE, meths)})
    write(repo / "mods" / "aaa.jar", jar_bytes("aaa", {}, package="x", nested={"lib-2.0.jar": lib("2.0", True)}))
    write(repo / "mods" / "zzz.jar", jar_bytes("zzz", {}, package="x", nested={"lib-1.0.jar": lib("1.0", False)}))
    # the same version but for its build metadata, which does not rank: a copy, not another version
    write(repo / "mods" / "zzzz.jar", jar_bytes("zzzz", {}, package="x",
                                                nested={"lib-2.0.jar": lib("2.0+build.7", True)}))
    write(repo / "mods" / "other.jar", jar_bytes("other", {"com/o/O": mixin_class("com/o/O", LE, {
        "tick": ("()V", [(OVERWRITE, {})])})}, package="com.o"))
    res = mixinconflicts.lookup(repo)
    [row] = res["conflicts"]
    assert {s["mod"] for s in row["mixins"]} == {"lib", "other"}
    assert next(m for m in res["mods"] if m["mod"] == "lib")["version"] == "2.0"
    notes = "\n".join(res["notes"])
    assert "mod lib: 2.0 from aaa.jar!/META-INF/jars/lib-2.0.jar!/fabric.mod.json is read" in notes
    assert "1.0 from zzz.jar!/META-INF/jars/lib-1.0.jar!/fabric.mod.json is skipped" in notes
    assert "1 nested jar(s) hold a mod already read" in notes and "2.0+build.7" not in notes


def test_namespace_note_only_for_the_games_classes(tmp_path):
    repo = tmp_path / "proj"
    inter = "net/minecraft/class_1309"
    for mid in ("i1", "i2"):
        n = f"com/{mid}/M"
        write(repo / "mods" / f"{mid}.jar", jar_bytes(mid, {n: mixin_class(n, inter, {
            "method_5773": ("()V", [(OVERWRITE, {})])})}, package=f"com.{mid}"))
    n = "com/c/M"   # a Mixin into another mod's class, named as that mod names it
    write(repo / "mods" / "c.jar", jar_bytes("c", {n: mixin_class(n, "com/other/Thing", {
        "tick": ("()V", [(OVERWRITE, {})])})}, package="com.c"))
    res = mixinconflicts.lookup(repo)
    assert not any("namespaces" in n for n in res["notes"])
    write(repo / "mods" / "named.jar", other_mod())   # LivingEntity in named names
    res = mixinconflicts.lookup(repo)
    assert any("intermediary by i1, i2; named by othermod" in n for n in res["notes"])


def test_logs_that_are_gzipped_binary_or_name_no_known_mod(tmp_path):
    import gzip

    repo = tmp_path / "proj"
    write(repo / "mods" / "alpha.jar", redirect_mod("alpha"))
    write(repo / "mods" / "beta.jar", redirect_mod("beta", "Lnet/minecraft/entity/Entity;discard()V"))
    gz = tmp_path / "2026-10-01-1.log.gz"
    gz.write_bytes(gzip.compress(b"Mixin apply for mod lost failed lost.mixins.json:LostMixin from mod lost -> "
                                 b"net.minecraft.class_1\n"))
    res = mixinconflicts.lookup(repo, log=gz)
    assert res["failures"][0]["mod"] == "lost" and mixinconflicts.exit_code(res) == 3
    binary = tmp_path / "crash.bin"
    binary.write_bytes(b"\x00\x01\x02 not a log")
    res = mixinconflicts.lookup(repo, log=binary)
    assert res["counts"]["unknown"] == 1 and any("is not text" in n for n in res["notes"])
    assert mixinconflicts.exit_code(res) == 4
    # a failure whose mod nothing names: exit 4, as documented (a clash or a named failure would be 3)
    unknown = tmp_path / "latest.log"
    unknown.write_text("Mixin [ghost.mixins.json:GhostMixin] from phase [DEFAULT] in config [ghost.mixins.json] "
                       "FAILED during APPLY\n", encoding="utf-8")
    res = mixinconflicts.lookup(repo, log=unknown)
    assert res["failures"][0]["mod_status"] == "unknown" and mixinconflicts.exit_code(res) == 4


def test_desc_targets_unparsed_classes_and_inner_class_names(tmp_path):
    repo = tmp_path / "proj"
    n = "com/d/D"
    write(repo / "mods" / "d.jar", jar_bytes("d", {n: mixin_class(n, LE, {"d$x": ("()V", [(INJ + "Inject;", {
        "target": [("Lcom/llamalad7/mixinextras/sugar/Desc;", {"value": "tick"})], "at": [at("HEAD")]})])})},
        package="com.d"))
    inner = "com/w/Outer$WMixin"
    write(repo / "mods" / "w.jar", raw_jar({
        "fabric.mod.json": json.dumps({"id": "w", "mixins": ["w.mixins.json"]}),
        "w.mixins.json": json.dumps({"package": "com.w", "mixins": ["Outer$WMixin"]}),
        inner + ".class": mixin_class(inner, LE, {"tick": ("()V", [(OVERWRITE, {})])})}))
    write(repo / "mods" / "othermod.jar", other_mod())
    log = tmp_path / "latest.log"
    log.write_text("Mixin [w.mixins.json:Outer$WMixin from mod w] from phase [DEFAULT] in config [w.mixins.json] "
                   "FAILED during APPLY\n", encoding="utf-8")
    res = mixinconflicts.lookup(repo, log=log)
    assert any("given by @Desc" in r["why"] and r["mixin"] == "com.d.D" for r in res["not_compared"])
    [fail] = res["failures"]
    assert fail["mixin"] == "com.w.Outer.WMixin" and fail["mod"] == "w"
    assert sorted(fail["likely_cause"][0]["mods"]) == ["othermod", "w"]
