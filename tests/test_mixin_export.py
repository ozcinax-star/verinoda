"""What a Mixin really changed, from Mixin's debug export (verinoda/mixinexport.py): the exported class's
``@MixinMerged`` methods and renamed handlers against the original class file on the classpath, each of the
project's injectors applied, merged, not applied or unknown with the exported class file as evidence; no export
is one unknown with how to turn it on; a Mixin claim of ``analyze`` cites the exported class."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from jvmfixtures import PUBLIC, base_classes, class_bytes, write_jar
from verinoda import cli, jvmclass, mixincheck, mixinexport

LE = "net/minecraft/entity/LivingEntity"
PE = "net/minecraft/entity/player/PlayerEntity"
SW = "net/minecraft/server/world/ServerWorld"
MIXIN = "com.example.mod.mixin.LivingMixin"
MERGED = mixinexport.MERGED
HANDLER = "handler$zza000$mymod$onDamage"
REDIRECT = "redirect$zzb000$mymod$quiet"
LOCALVAR = "localvar$zzc000$mymod$scale"     # merged, and called from nowhere
CB = "Lorg/spongepowered/asm/mixin/injection/callback/CallbackInfo;"


def merged(mixin: str = MIXIN, priority: int = 1000) -> list:
    return [(MERGED, {"mixin": mixin, "priority": priority, "sessionId": "5f1c"})]


ORIGINAL = {
    LE: class_bytes(LE, methods=(
        ("damage", "(F)Z", PUBLIC, [("getfield", LE, "health", "F")]),
        ("tick", "()V", PUBLIC, [("invokevirtual", LE, "baseTick", "()V")]),
        ("baseTick", "()V", PUBLIC, [("invokevirtual", LE, "playSound", "()V")]),
        ("playSound", "()V", PUBLIC, [])),
        fields=(("health", "F", PUBLIC),)),
    PE: class_bytes(PE, methods=(("jump", "()V", PUBLIC, []),)),
    SW: class_bytes(SW, methods=(("tickTime", "()V", PUBLIC, []),)),
}
# the class as the game ran it: a handler merged and called from damage, an @Overwrite of tick, a redirect
# handler merged and called from baseTick in place of playSound, a field and an interface added
EXPORTED = {
    LE: class_bytes(LE, ifaces=("com/example/mod/Marker",), methods=(
        ("damage", "(F)Z", PUBLIC, [("getfield", LE, "health", "F"), ("invokevirtual", LE, HANDLER, f"({CB})V")]),
        ("tick", "()V", PUBLIC, []),
        ("baseTick", "()V", PUBLIC, [("invokevirtual", LE, REDIRECT, "()V")]),
        ("playSound", "()V", PUBLIC, []),
        (HANDLER, f"({CB})V", PUBLIC, []),
        (REDIRECT, "()V", PUBLIC, []),
        (LOCALVAR, "(F)F", PUBLIC, []),
        ("lonely", "()V", PUBLIC, [])),
        fields=(("health", "F", PUBLIC), ("mymod$counter", "I", PUBLIC)),
        method_annotations={HANDLER: merged(), REDIRECT: merged(), LOCALVAR: merged(), "tick": merged(priority=900),
                            "lonely": merged("com.other.OtherMixin")}),
    SW: class_bytes(SW, methods=(("tickTime", "()V", PUBLIC, []),)),
}

SOURCE = """package com.example.mod.mixin;

import net.minecraft.entity.LivingEntity;
import net.minecraft.entity.player.PlayerEntity;
import net.minecraft.server.world.ServerWorld;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.Overwrite;
import org.spongepowered.asm.mixin.injection.*;

@Mixin(LivingEntity.class)
public abstract class LivingMixin {
    @Inject(method = "damage", at = @At("HEAD"))
    private void onDamage(CallbackInfo ci) {}

    @Overwrite
    public void tick() {}

    @Redirect(method = "baseTick",
              at = @At(value = "INVOKE", target = "Lnet/minecraft/entity/LivingEntity;playSound()V"))
    private void quiet(LivingEntity self) {}

    @Inject(method = "damage", at = @At("TAIL"))
    private void neverMerged(CallbackInfo ci) {}

    @ModifyVariable(method = "damage", at = @At("HEAD"))
    private float scale(float f) { return f; }
}

@Mixin(PlayerEntity.class)
abstract class PlayerMixin {
    @Inject(method = "jump", at = @At("HEAD"))
    private void onJump(CallbackInfo ci) {}
}

@Mixin(ServerWorld.class)
abstract class WorldMixin {
    @Inject(method = "tickTime", at = @At("HEAD"))
    private void onTickTime(CallbackInfo ci) {}
}
"""
SRC = "src/main/java/com/example/mod/mixin/LivingMixin.java"
EXPORT = "run/.mixin.out"


def _repo(tmp_path: Path, *, export: str | None = EXPORT, classpath: bool = True) -> Path:
    repo = tmp_path / "proj"
    (repo / SRC).parent.mkdir(parents=True)
    (repo / SRC).write_text(SOURCE, encoding="utf-8")
    (repo / "settings.gradle").write_text("", encoding="utf-8")
    (repo / ".verinoda").mkdir()
    if classpath:
        write_jar(repo / "libs" / "mc.jar", {**base_classes(), **ORIGINAL})
        (repo / ".verinoda" / "config.json").write_text(
            json.dumps({"code_check": {"classpath": ["libs/*.jar"]}}), encoding="utf-8")
    if export:
        _write_export(repo / export)
        old = (repo / SRC).stat().st_mtime - 3600   # the export was written after the source
        os.utime(repo / SRC, (old, old))
    return repo


def _write_export(d: Path) -> None:
    for name, data in EXPORTED.items():
        p = d / "class" / (name + ".class")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def _rows(res: dict) -> dict[tuple[str, str], dict]:
    return {(r["mixin"].rsplit(".", 1)[-1], r["member"]): r for r in res["export"]["rows"]}


def test_class_code_reads_the_interfaces():
    assert jvmclass.class_code(EXPORTED[LE])["ifaces"] == ["com/example/mod/Marker"]
    assert jvmclass.class_code(ORIGINAL[LE])["ifaces"] == []


def test_each_injector_against_the_export(tmp_path):
    repo = _repo(tmp_path)
    res = mixincheck.lookup(repo)
    sec = res["export"]
    assert sec["status"] == "found" and sec["dirs"] == [EXPORT]
    rows = _rows(res)
    cls = f"{EXPORT}/class/{LE}.class"
    r = rows[("LivingMixin", "onDamage")]
    assert r["verdict"] == "applied" and r["status"] == "observed" and r["evidence"] == cls
    assert r["handler"] == f"{HANDLER}({CB})V" and r["called_from"] == ["damage(F)Z"]
    assert r["merged"] == {"mixin": MIXIN, "priority": 1000}
    assert r["at"] == f"{SRC}:12" and '@MixinMerged(mixin = "com.example.mod.mixin.LivingMixin"' in r["why"]
    o = rows[("LivingMixin", "tick")]
    assert o["verdict"] == "applied" and o["merged"]["priority"] == 900 and "replaced the original" in o["why"]
    q = rows[("LivingMixin", "quiet")]
    assert q["verdict"] == "applied" and q["called_from"] == ["baseTick()V"]
    n = rows[("LivingMixin", "neverMerged")]
    assert n["verdict"] == "not_applied" and n["status"] == "strong_inference"
    assert "none of them the handler" in n["why"]
    p = rows[("PlayerMixin", "onJump")]
    assert p["verdict"] == "unknown" and p["status"] == "unknown" and "was not loaded" in p["why"] and p["next"]
    w = rows[("WorldMixin", "onTickTime")]
    assert w["verdict"] == "not_applied" and "carries @MixinMerged naming" in w["why"]
    s = rows[("LivingMixin", "scale")]
    assert s["verdict"] == "merged" and s["status"] == "observed" and s["handler"] == f"{LOCALVAR}(F)F"
    assert "but no method of it calls the handler" in s["why"]
    assert sec["counts"] == {"applied": 3, "merged": 1, "not_applied": 2, "unknown": 1}
    # the name checks are unchanged
    assert res["counts"]["absent"] == 0


def test_what_the_export_holds_beyond_the_original(tmp_path):
    repo = _repo(tmp_path)
    k = {c["class"]: c for c in mixincheck.lookup(repo)["export"]["classes"]}["net.minecraft.entity.LivingEntity"]
    assert k["original"] == f"mc.jar!{LE}.class" and k["export"] == f"{EXPORT}/class/{LE}.class"
    mine = {m["method"]: m for m in k["by_mixin"][MIXIN]}
    assert mine[f"{HANDLER}({CB})V"]["how"] == "added" and mine[f"{HANDLER}({CB})V"]["injector"] == "Inject"
    assert mine["tick()V"]["how"] == "replaced" and mine[f"{REDIRECT}()V"]["injector"] == "Redirect"
    assert [m["method"] for m in k["by_mixin"]["com.other.OtherMixin"]] == ["lonely()V"]
    assert k["fields_added"] == ["mymod$counter:I"] and k["interfaces_added"] == ["com.example.mod.Marker"]
    changed = {c["method"]: c for c in k["methods_changed"]}
    assert changed["damage(F)Z"]["calls_added"] == [f"LivingEntity.{HANDLER}({CB})V ({MIXIN})"]
    assert changed["baseTick()V"]["calls_removed"] == ["LivingEntity.playSound()V"]
    assert k["calls"][f"{HANDLER}({CB})V"] == ["damage(F)Z"]


def test_without_an_export_one_unknown_with_the_next_step(tmp_path, capsys):
    repo = _repo(tmp_path, export=None)
    sec = mixincheck.lookup(repo)["export"]
    assert sec["status"] == "unknown" and sec["rows"] == [] and "-Dmixin.debug.export=true" in sec["next"]
    assert cli.main(["mixin-check", "--repo", str(repo)]) == 0   # opt-in: the exit code is the name check's
    out = capsys.readouterr().out
    assert "export: no Mixin debug export (.mixin.out) found" in out and "next: add -Dmixin.debug.export" in out


def test_without_a_classpath_the_merged_methods_still_name_their_mixin(tmp_path):
    repo = _repo(tmp_path, classpath=False)
    res = mixincheck.lookup(repo)
    rows = _rows(res)
    assert rows[("LivingMixin", "onDamage")]["verdict"] == "applied"
    k = res["export"]["classes"][0]
    assert k["original"] is None and "fields_added" not in k
    assert {m["how"] for m in k["by_mixin"][MIXIN]} == {"merged"}


def test_a_source_newer_than_the_export(tmp_path):
    repo = _repo(tmp_path)
    old = (repo / EXPORT / "class" / (LE + ".class")).stat().st_mtime - 7200
    for name in EXPORTED:
        os.utime(repo / EXPORT / "class" / (name + ".class"), (old, old))
    rows = _rows(mixincheck.lookup(repo))
    n = rows[("LivingMixin", "neverMerged")]
    assert n["verdict"] == "not_applied" and n["status"] == "unknown" and n["next"] == mixinexport.NEXT_RERUN
    a = rows[("LivingMixin", "onDamage")]
    assert a["status"] == "observed" and "the export shows the earlier version" in a["stale"]


def test_export_given_elsewhere_the_audit_and_the_cli(tmp_path, capsys):
    repo = _repo(tmp_path, export=None)
    elsewhere = tmp_path / "instance" / ".mixin.out"
    _write_export(elsewhere)
    (elsewhere / "audit").mkdir()
    (elsewhere / "audit" / mixinexport.AUDIT_CSV).write_text(
        "Class,Method,Signature,Interface\n"
        f"{LE},getId,()I,com.example.mod.Marker\n"
        "too,short\n", encoding="utf-8")
    assert cli.main(["mixin-check", "--repo", str(repo), "--json", "--export", str(elsewhere)]) == 0
    sec = json.loads(capsys.readouterr().out)["export"]
    assert sec["status"] == "found" and sec["dirs"] == [elsewhere.resolve().as_posix()]
    assert sec["counts"]["applied"] == 3
    assert sec["audit"] == [{
        "class": "net.minecraft.entity.LivingEntity", "method": "getId()I", "interface": "com.example.mod.Marker",
        "status": "observed", "evidence": f"{elsewhere.resolve().as_posix()}/audit/{mixinexport.AUDIT_CSV}:2",
        "why": "after its Mixins were applied, net.minecraft.entity.LivingEntity lacks getId()I of "
               "com.example.mod.Marker"}]
    # the folder holding .mixin.out, and its class folder, name the same export
    for given in (elsewhere.parent, elsewhere / "class"):
        found, notes = mixinexport.find_exports(repo, [repo], [str(given)])
        assert [e.dir for e in found] == [elsewhere] and notes == []
    assert cli.main(["mixin-check", "--repo", str(repo)]) == 0
    assert "applied [observed]" not in capsys.readouterr().out
    assert cli.main(["mixin-check", "--repo", str(repo), "--export", str(tmp_path / "instance")]) == 0
    out = capsys.readouterr().out
    assert f"applied [observed] {SRC}:12  @Inject {MIXIN}.onDamage" in out
    assert "not_applied [strong_inference]" in out and "audit [observed]" in out
    with pytest.raises(SystemExit, match="not a folder"):
        cli.main(["mixin-check", "--repo", str(repo), "--export", str(tmp_path / "nowhere")])
    with pytest.raises(SystemExit, match="without --conflicts"):
        cli.main(["mixin-check", "--repo", str(repo), "--conflicts", "--export", str(elsewhere)])


def test_an_unreadable_exported_class_is_unknown(tmp_path):
    repo = _repo(tmp_path)
    (repo / EXPORT / "class" / (LE + ".class")).write_bytes(b"not a class")
    rows = _rows(mixincheck.lookup(repo))
    r = rows[("LivingMixin", "onDamage")]
    assert r["verdict"] == "unknown" and "not a class file this reader reads" in r["why"]


@pytest.mark.parametrize("name, kind", [
    ("handler$zza000$onTick", "Inject"), ("redirect$zza000$mod$x", "Redirect"), ("localvar$zza000$v", "ModifyVariable"),
    ("wrapOperation$zza000$w", "WrapOperation"), ("tick", None), ("guard$noSpawn", None)])
def test_handler_kind(name, kind):
    assert mixinexport.handler_kind(name) == kind


def test_handler_by_member_with_and_without_a_mod_id():
    mine = {("handler$zza000$mymod$x", "()V"): {}, ("handler$zzb000$mymod$mod$x", "()V"): {},
            ("handler$zzc000$guard$y", "()V"): {}, ("plain", "()V"): {}}
    assert mixinexport._handler("x", mine)[0] == "handler$zza000$mymod$x"
    assert mixinexport._handler("mod$x", mine)[0] == "handler$zzb000$mymod$mod$x"
    assert mixinexport._handler("guard$y", mine)[0] == "handler$zzc000$guard$y"
    assert mixinexport._handler("plain", mine)[0] == "plain" and mixinexport._handler("z", mine) is None
    # "guard$y" is a handler of its own: its merged method is not "y"'s with the mod id "guard"
    assert mixinexport._handler("y", mine)[0] == "handler$zzc000$guard$y"
    assert mixinexport._handler("y", mine, {"guard$y"}) is None


# -- a Mixin claim cites the exported class ---------------------------------------------------------------

MOB = "net/minecraft/world/entity/Mob"
GUARD = "handler$zza000$guard$noSpawnInWard"


@pytest.fixture(scope="module")
def guard_repo(tmp_path_factory) -> Path:
    from test_mixins import FILES

    from verinoda import search_index, workflow
    from verinoda.store import open_store

    root = tmp_path_factory.mktemp("mixexport") / "mod"
    for rel, text in FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(text.encode("utf-8"))
    workflow.init(root)
    st = open_store(root)
    try:
        workflow.scan(st, root)
    finally:
        st.close()
    search_index._HANDLES.clear()
    return root


def _analyze(repo: Path) -> list[dict]:
    from verinoda import analysis
    from verinoda.store import open_store

    st = open_store(repo)
    try:
        res = analysis.analyze(st, repo, "what blocks mob spawning?")
    finally:
        st.close()
    return [c for c in res["claims"] if "runs inside `Mob.checkSpawnRules`" in c["text"]]


def test_a_mixin_claim_cites_the_exported_class(guard_repo):
    import shutil

    desc = "(Lnet/minecraft/world/level/LevelAccessor;Ljava/lang/Object;Lorg/x/Cir;)V"
    p = guard_repo / "run" / ".mixin.out" / "class" / (MOB + ".class")
    p.parent.mkdir(parents=True)
    p.write_bytes(class_bytes(MOB, methods=(
        ("checkSpawnRules", "(Lnet/minecraft/world/level/LevelAccessor;)Z", PUBLIC,
         [("invokevirtual", MOB, GUARD, desc)]),
        (GUARD, desc, PUBLIC, [])),
        method_annotations={GUARD: merged("com.example.guard.mixin.MobMixin")}))
    try:
        claims = _analyze(guard_repo)
        assert claims, "no Mixin claim"
        ev = [e for c in claims for e in c["evidence"] if e.startswith("supports:experiment:")]
        assert ev and f"run/.mixin.out/class/{MOB}.class: {GUARD}{desc}" in ev[0], claims
        assert 'mixin = "com.example.guard.mixin.MobMixin"' in ev[0] and "called from checkSpawnRules" in ev[0]
        unc = [u for c in claims for u in c["uncertainties"]]
        assert any(u.startswith("observed in Mixin's debug export") for u in unc), unc
        assert not any("bytecode is not checked" in u for u in unc)
    finally:
        shutil.rmtree(guard_repo / "run")
