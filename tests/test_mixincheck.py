"""Mixin injection points against the target class's bytecode (verinoda/mixincheck.py): method selectors with
and without descriptors, @At targets against what the selected methods' bytecode references, @Shadow fields and
methods by type; a wrong one absent with the nearest real one, unknown where there is no class file."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from jvmfixtures import PUBLIC, base_classes, class_bytes, write_jar
from verinoda import cli, jvmclass, mixincheck

FIXTURE = Path(__file__).parent / "fixtures" / "verdict_audit" / "mixmod"
PRIVATE, ABSTRACT = 0x0002, 0x0400
LE = "net/minecraft/entity/LivingEntity"
DS = "Lnet/minecraft/entity/damage/DamageSource;"
DAMAGE_CODE = [
    "tableswitch",
    ("invokevirtual", LE, "isInvulnerableTo", f"({DS})Z"),
    ("getfield", LE, "health", "F"),
    "wide",
    ("new", "net/minecraft/entity/ItemEntity", "", ""),
    ("invokespecial", "net/minecraft/entity/ItemEntity", "<init>", "(I)V"),
    "lookupswitch",
    ("invokeinterface", "net/minecraft/entity/Tracker", "track", "()V"),
]
LIB = {
    "net/minecraft/entity/Entity": class_bytes("net/minecraft/entity/Entity", methods=(
        ("baseTick", "()V", PUBLIC),), fields=(("age", "I", PUBLIC),)),
    LE: class_bytes(LE, super_="net/minecraft/entity/Entity", methods=(
        ("damage", f"({DS}F)Z", PUBLIC, DAMAGE_CODE),
        ("isInvulnerableTo", f"({DS})Z", PUBLIC, []),
        ("getMaxHealth", "()F", PUBLIC | ABSTRACT)),
        fields=(("health", "F", PRIVATE), ("hurtTime", "I", PRIVATE))),
    "net/minecraft/entity/player/PlayerEntity": class_bytes("net/minecraft/entity/player/PlayerEntity", methods=(
        ("canHarvest", "(Lnet/minecraft/block/BlockState;)Z", PUBLIC, []),)),
    "net/minecraft/server/world/ServerWorld": class_bytes("net/minecraft/server/world/ServerWorld", methods=(
        ("tick", "(Ljava/util/function/BooleanSupplier;)V", PUBLIC, []),)),
    "net/minecraft/client/gui/hud/InGameHud": class_bytes("net/minecraft/client/gui/hud/InGameHud", methods=(
        ("render", "(Lnet/minecraft/client/gui/DrawContext;F)V", PUBLIC, []),)),
}

CHECKED = """package com.example.mixmod.mixin;

import com.example.mixmod.block.EmberLamp;
import net.minecraft.entity.LivingEntity;
import net.minecraft.entity.damage.DamageSource;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.Shadow;
import org.spongepowered.asm.mixin.injection.*;

@Mixin(LivingEntity.class)
public abstract class CheckedMixin {
    private static final String DAMAGE = "damage(Lnet/minecraft/entity/damage/DamageSource;F)Z";
    @Shadow private float health;
    @Shadow private float helth;
    @Shadow private long hurtTime;
    @Shadow public abstract boolean isInvulnerableTo(DamageSource source);
    @Shadow public abstract boolean isInvulnerableTo(int source);
    @Shadow private int age;

    @Inject(method = DAMAGE, at = @At(value = "INVOKE", target = "Lnet/minecraft/entity/LivingEntity;isInvulnerableTo(Lnet/minecraft/entity/damage/DamageSource;)Z"))
    private void a() {}

    @Redirect(method = "damage", at = @At(value = "INVOKE",
            target = "Lnet/minecraft/entity/LivingEntity;isInvulnerableToo(Lnet/minecraft/entity/damage/DamageSource;)Z"))
    private boolean b() { return false; }

    @ModifyArg(method = "damage(Lnet/minecraft/entity/damage/DamageSource;I)Z", at = @At(value = "FIELD", target = "Lnet/minecraft/entity/LivingEntity;health:F"))
    private float c(float f) { return f; }

    @Inject(method = "damagee", at = @At("HEAD"))
    private void d() {}

    @Redirect(method = "damage", at = @At(value = "FIELD", target = "Lnet/minecraft/entity/LivingEntity;health:I"))
    private int e() { return 0; }

    @Inject(method = "damage", at = {@At(value = "NEW", target = "net/minecraft/entity/ItemEntity"),
                                     @At(value = "NEW", target = "(I)Lnet/minecraft/entity/ItemEntity;"),
                                     @At(value = "NEW", target = "(J)Lnet/minecraft/entity/ItemEntity;")})
    private void f() {}

    @Inject(method = "damage", at = @At(value = "INVOKE", target = "Lnet/minecraft/entity/Tracker;track()V"))
    private void g() {}

    @Inject(method = "baseTick", at = @At("HEAD"))
    private void h() {}

    @Inject(method = "method_5643", at = @At("HEAD"))
    private void i() {}

    @Inject(method = "getMaxHealth", at = @At(value = "INVOKE", target = "Lx/Y;z()V"))
    private void j() {}
}

@Mixin(EmberLamp.class)
abstract class LampMixin {
    @Inject(method = "cool", at = @At("HEAD"))
    private void k() {}
}
"""
CHECKED_PATH = "src/main/java/com/example/mixmod/mixin/CheckedMixin.java"


def _repo(tmp_path: Path, *, config: bool = True, checked: bool = True) -> Path:
    repo = tmp_path / "proj"
    shutil.copytree(FIXTURE, repo)
    write_jar(repo / "libs" / "mc.jar", {**base_classes(), **LIB})
    if checked:
        (repo / CHECKED_PATH).write_text(CHECKED, encoding="utf-8")
    (repo / ".verinoda").mkdir()
    if config:
        (repo / ".verinoda" / "config.json").write_text(json.dumps({"code_check": {"classpath": ["libs/*.jar"]}}),
                                                        encoding="utf-8")
    return repo


@pytest.fixture(autouse=True)
def _no_machine_jdk(monkeypatch):
    monkeypatch.setattr(jvmclass, "_jdk_homes", lambda: [])


def _rows(res: dict, line: int) -> list[dict]:
    return [r for r in res["entries"] if r["at"] == f"{CHECKED_PATH}:{line}"]


def test_class_code_reads_what_the_bytecode_references_past_switches_and_wide():
    got = jvmclass.class_code(LIB[LE])
    assert got["name"] == LE and got["super"] == "net/minecraft/entity/Entity"
    methods = {m[0]: m for m in got["methods"]}
    assert methods["damage"][3] == [
        ["M", LE, "isInvulnerableTo", f"({DS})Z"], ["F", LE, "health", "F"],
        ["N", "net/minecraft/entity/ItemEntity", "", ""],
        ["M", "net/minecraft/entity/ItemEntity", "<init>", "(I)V"],
        ["M", "net/minecraft/entity/Tracker", "track", "()V"]]
    assert methods["isInvulnerableTo"][3] == []
    assert methods["getMaxHealth"][3] is None          # abstract: no Code attribute
    assert [f[:2] for f in got["fields"]] == [["health", "F"], ["hurtTime", "I"]]
    assert jvmclass.class_code(b"not a class") is None


def test_selectors_at_targets_and_shadows_against_the_bytecode(tmp_path):
    res = mixincheck.check(_repo(tmp_path), config={"code_check": {"classpath": ["libs/*.jar"]}})
    by_kind = lambda line, kind: next(r for r in _rows(res, line) if r["kind"] == kind)
    ok = by_kind(13, "shadow")
    assert ok["verdict"] == "exists" and ok["status"] == "statically_verified"
    assert ok["evidence"] == f"mc.jar!{LE}.class"
    typo = by_kind(14, "shadow")
    assert typo["verdict"] == "absent" and typo["status"] == "statically_verified"
    assert typo["nearest"] == ["health"] and typo["nearest_status"] == "strong_inference"
    wrong_type = by_kind(15, "shadow")
    assert wrong_type["verdict"] == "absent" and wrong_type["nearest"] == ["hurtTime: int"]
    assert by_kind(16, "shadow")["verdict"] == "exists"
    sig = by_kind(17, "shadow")
    assert sig["verdict"] == "absent" and sig["nearest"] == ["isInvulnerableTo(DamageSource) -> boolean"]
    inherited = by_kind(18, "shadow")
    assert inherited["verdict"] == "unknown" and "net.minecraft.entity.Entity" in inherited["why"]
    # a selector through a constant with its descriptor, and an @At INVOKE target the bytecode references
    assert by_kind(20, "method")["verdict"] == "exists" and by_kind(20, "at")["verdict"] == "exists"
    # a wrong @At target: absent, with the real call as the nearest
    bad_at = by_kind(24, "at")
    assert bad_at["verdict"] == "absent" and bad_at["status"] == "statically_verified"
    assert bad_at["nearest"][0] == ("Lnet/minecraft/entity/LivingEntity;isInvulnerableTo"
                                    "(Lnet/minecraft/entity/damage/DamageSource;)Z")
    assert "LivingEntity.damage" in bad_at["why"] and "mc.jar!" in bad_at["why"]
    # a wrong descriptor: absent with the real one; its @At is then not looked for
    desc = by_kind(27, "method")
    assert desc["verdict"] == "absent" and desc["nearest"] == [f"damage({DS}F)Z"]
    assert by_kind(27, "at")["verdict"] == "unknown"
    name = by_kind(30, "method")
    assert name["verdict"] == "absent" and name["nearest"][0] == "damage"
    assert by_kind(33, "at")["verdict"] == "absent"                  # health is a float, not an int
    assert by_kind(33, "at")["nearest"] == ["Lnet/minecraft/entity/LivingEntity;health:F"]
    news = [r["verdict"] for r in _rows(res, 36) + _rows(res, 37) + _rows(res, 38) if r["kind"] == "at"]
    assert news == ["exists", "exists", "absent"]
    assert by_kind(41, "at")["verdict"] == "exists"                   # after a lookupswitch
    assert by_kind(44, "method")["verdict"] == "unknown"              # inherited from Entity
    assert "intermediary" in by_kind(47, "method")["why"]
    abstract = by_kind(50, "at")
    assert abstract["verdict"] == "unknown" and "no bytecode" in abstract["why"]
    lamp = by_kind(56, "method")
    assert lamp["verdict"] == "unknown" and "project's own sources" in lamp["why"] and lamp["next"]
    assert "evidence" not in lamp
    # the fixture's own Mixins are right
    fixture = [r for r in res["entries"] if "CheckedMixin" not in r["at"]]
    assert {r["written"]: r["verdict"] for r in fixture} == {"damage": "exists", "canHarvest": "exists",
                                                             "tick": "exists", "render": "exists"}


def test_without_a_classpath_every_name_is_unknown_with_the_next_step(tmp_path):
    res = mixincheck.check(_repo(tmp_path, config=False))
    assert res["entries"] and {r["verdict"] for r in res["entries"]} == {"unknown"}
    row = next(r for r in res["entries"] if r["written"] == "damagee")
    assert "not complete" in row["why"] and row["next"] == mixincheck.NEXT_CLASSPATH


def test_nearest_is_by_edit_distance():
    assert mixincheck.nearest("tickk", ["tick", "tock", "render", "ticks"]) == ["tick", "ticks", "tock"]
    assert mixincheck.nearest("abc", ["something else entirely"]) == []
    assert mixincheck.parse_member("Lnet/a/B;c(I)V") == ("net/a/B", "c", "(I)V")
    assert mixincheck.parse_member("Lnet/a/B;f:I") == ("net/a/B", "f", "I")
    assert mixincheck.parse_member("net.a.B.c(I)V") == ("net/a/B", "c", "(I)V")
    assert mixincheck.parse_selector("tick*") == ("tick", None)
    assert mixincheck.parse_selector("/tick.*/") is None


def test_cli(tmp_path, capsys):
    repo = _repo(tmp_path)
    assert cli.main(["mixin-check", "--repo", str(repo)]) == 3
    out = capsys.readouterr().out
    assert f"absent [statically_verified] {CHECKED_PATH}:24" in out
    assert "nearest (a suggestion, strong_inference): Lnet/minecraft/entity/LivingEntity;isInvulnerableTo(" in out
    fixture_file = "src/main/java/com/example/mixmod/mixin/ServerWorldMixin.java"
    assert cli.main(["mixin-check", "--repo", str(repo), "--json", fixture_file]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["status"] == "found" and data["counts"] == {"exists": 1, "absent": 0, "unknown": 0}
    empty = tmp_path / "empty"
    empty.mkdir()
    assert cli.main(["mixin-check", "--repo", str(empty)]) == 2
    assert "no @Mixin" in capsys.readouterr().out
