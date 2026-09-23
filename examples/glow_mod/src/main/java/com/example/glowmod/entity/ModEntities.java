package com.example.glowmod.entity;

import com.example.glowmod.GlowMod;
import net.fabricmc.fabric.api.object.builder.v1.entity.FabricDefaultAttributeRegistry;
import net.minecraft.entity.EntityType;
import net.minecraft.entity.SpawnGroup;
import net.minecraft.registry.Registries;
import net.minecraft.registry.Registry;
import net.minecraft.util.Identifier;

public final class ModEntities {
    public static final EntityType<Wisp> WISP = Registry.register(
            Registries.ENTITY_TYPE,
            Identifier.of(GlowMod.MOD_ID, "wisp"),
            EntityType.Builder.create(Wisp::new, SpawnGroup.AMBIENT).dimensions(0.5F, 0.5F).build("wisp"));

    private ModEntities() {
    }

    public static void register() {
        FabricDefaultAttributeRegistry.register(WISP, Wisp.createWispAttributes());
    }
}
