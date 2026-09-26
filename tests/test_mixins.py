"""Mixin edges (docs/DESIGN.md D48): a SpongePowered injector is an ``injects`` edge from the handler to the target
class with the target method, the injection point and whether it can cancel; an accessor is an ``accesses`` edge.
`when`, `query` and `node` say it."""

from __future__ import annotations

import os

os.environ.setdefault("GRAPHIFY_OUT", ".verinoda/index")

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

from verinoda import index, jvm_mixins, retrieval, search_index, when, workflow  # noqa: E402
from verinoda.store import open_store  # noqa: E402

PKG = "src/main/java/com/example/guard/mixin"

FILES = {
    f"{PKG}/MobMixin.java": """package com.example.guard.mixin;

import net.minecraft.world.entity.Mob;
import net.minecraft.world.level.LevelAccessor;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.Redirect;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;

/** Nothing hostile spawns inside a warded area. */
@Mixin(Mob.class)
public abstract class MobMixin {
    private static final String SET_TARGET = "setTarget(Lnet/minecraft/world/entity/LivingEntity;)V";

    // @Inject(method = "tick", at = @At("HEAD"))  -- a comment, not an injector
    @Inject(method = "checkSpawnRules(Lnet/minecraft/world/level/LevelAccessor;"
                     + "Lnet/minecraft/world/entity/EntitySpawnReason;)Z",
            at = @At("HEAD"), cancellable = true)
    private void guard$noSpawnInWard(LevelAccessor level, Object reason, CallbackInfoReturnable<Boolean> cir) {
        if (Ward.inside(level)) {
            cir.setReturnValue(false);
        }
    }

    @Redirect(method = SET_TARGET,
              at = @At(value = "INVOKE", target = "Lnet/minecraft/world/entity/Mob;playSound(Ljava/lang/Object;)V"))
    private void guard$quietTarget(Mob mob, Object sound) {
    }
}
""",
    f"{PKG}/MobAccessor.java": """package com.example.guard.mixin;

import net.minecraft.world.entity.Mob;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.gen.Accessor;
import org.spongepowered.asm.mixin.gen.Invoker;

@Mixin(Mob.class)
public interface MobAccessor {
    @Accessor("xpReward")
    int guard$getXp();

    @Invoker
    void callPlayAmbientSound();
}
""",
    "src/main/java/com/example/guard/mixin/Ward.java": """package com.example.guard.mixin;

public final class Ward {
    public static boolean inside(Object level) {
        return false;
    }
}
""",
}


@pytest.fixture(scope="module")
def repo(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("mixins") / "mod"
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


def _edges(g, rel):
    return {(g.label(u).strip(".()"), g.label(v)): d for u, v, d in g.edges({rel})}


def test_injectors_are_edges_to_the_target_class(repo):
    g = index.load(repo)
    inj = _edges(g, jvm_mixins.MIXIN_RELATION)
    d = inj[("guard$noSpawnInWard", "net.minecraft.world.entity.Mob")]
    assert d["kind"] == "Inject" and d["target_methods"] == ["checkSpawnRules"]
    assert d["at"] == "HEAD" and d["cancellable"] is True
    assert d["confidence"] == "EXTRACTED" and d["_origin"] == jvm_mixins.MIXIN_ORIGIN
    assert d["context"] == "@Inject into Mob.checkSpawnRules at HEAD - at its start; can cancel it"
    assert d["source_location"] == "L17"  # the annotation, not the commented-out one above it
    r = inj[("guard$quietTarget", "net.minecraft.world.entity.Mob")]
    assert r["kind"] == "Redirect" and r["target_methods"] == ["setTarget"]  # through the String constant
    assert r["at"] == "INVOKE" and r["at_target"] == "Mob.playSound" and "cancellable" not in r
    assert len(inj) == 2


def test_accessors(repo):
    g = index.load(repo)
    acc = _edges(g, jvm_mixins.ACCESS_RELATION)
    assert acc[("guard$getXp", "net.minecraft.world.entity.Mob")]["target_member"] == "xpReward"
    assert acc[("callPlayAmbientSound", "net.minecraft.world.entity.Mob")]["target_member"] == "playAmbientSound"


def test_when_says_the_handler_runs_inside_the_target(repo):
    res = when.run(index.load(repo), "MobMixin.guard$noSpawnInWard")
    assert res["status"] == "found"
    ev = next(h for p in res["paths"] for h in p if h.get("event"))
    assert ev["event"] == "inside Mob.checkSpawnRules, at its start; can cancel it"
    assert ev["at"].endswith("MobMixin.java:17")


def test_query_names_the_injection(repo):
    g = index.load(repo)
    text = retrieval.render_text(retrieval.retrieve(g, "what stops mobs from spawning"))
    assert "MobMixin" in text
    assert "mixin: @Inject into Mob.checkSpawnRules at HEAD" in text


@pytest.mark.parametrize("ref, owner, name", [
    ("checkSpawnRules(Lnet/minecraft/world/level/LevelAccessor;)Z", None, "checkSpawnRules"),
    ("Lnet/minecraft/world/entity/Mob;setTarget(Lnet/minecraft/world/entity/LivingEntity;)V",
     "net.minecraft.world.entity.Mob", "setTarget"),
    ("Lnet/minecraft/world/entity/Entity;xo:D", "net.minecraft.world.entity.Entity", "xo"),
    ("method_5979", None, "method_5979"),
])
def test_readable_member(ref, owner, name):
    assert jvm_mixins.readable_member(ref) == (owner, name)


def test_strip_comments_keeps_strings_and_lines():
    src = 'a = "// not a comment"; // gone\n/* x\n y */ b = "/*";'
    out = jvm_mixins.strip_comments(src)
    assert out.count("\n") == 2 and '"// not a comment"' in out and "gone" not in out and '"/*"' in out


def test_analyze_answers_what_blocks_with_the_cancellable_injection(repo):
    from verinoda import analysis

    st = open_store(repo)
    try:
        res = analysis.analyze(st, repo, "what blocks mob spawning?")
    finally:
        st.close()
    claims = {c["id"]: c for c in res["claims"]}
    texts = [claims[c]["text"] for s in res["subquestions"] for c in s["answer_claim_ids"]]
    assert any("runs inside `Mob.checkSpawnRules` at its start; it can cancel it" in t for t in texts), texts
    assert not any("Mob.setTarget" in t for t in texts)  # the question's words name spawning, not targeting
