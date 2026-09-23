package net.ashvale.emberforge.registry;

import net.ashvale.emberforge.EmberForge;
import net.minecraft.core.registries.Registries;
import net.minecraft.resources.ResourceLocation;
import net.minecraft.tags.TagKey;
import net.minecraft.world.item.Item;
import net.minecraft.world.level.block.Block;

public final class ModTags {
    /** Items the forge can burn for heat. */
    public static final TagKey<Item> FORGE_FUELS = itemTag("forge_fuels");
    /** Blocks that keep a forge standing on them warm. */
    public static final TagKey<Block> FORGE_HEAT_SOURCES = blockTag("forge_heat_sources");

    private static TagKey<Item> itemTag(String name) {
        return TagKey.create(Registries.ITEM, ResourceLocation.fromNamespaceAndPath(EmberForge.MOD_ID, name));
    }

    private static TagKey<Block> blockTag(String name) {
        return TagKey.create(Registries.BLOCK, ResourceLocation.fromNamespaceAndPath(EmberForge.MOD_ID, name));
    }

    private ModTags() {
    }
}
