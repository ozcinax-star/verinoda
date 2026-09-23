package com.example.glowmod.item;

import com.example.glowmod.GlowMod;
import net.fabricmc.fabric.api.itemgroup.v1.ItemGroupEvents;
import net.minecraft.item.Item;
import net.minecraft.item.ItemGroups;
import net.minecraft.registry.Registries;
import net.minecraft.registry.Registry;
import net.minecraft.util.Identifier;
import net.minecraft.util.Rarity;

public final class ModItems {
    public static final Item LANTERN_STAFF = register("lantern_staff", new Item(new Item.Settings().maxCount(1)));
    public static final Item EMBER_SHARD = register("ember_shard", new Item(new Item.Settings()));
    public static final Item WISP_HEART = register("wisp_heart", new Item(new Item.Settings().rarity(Rarity.UNCOMMON)));

    private ModItems() {
    }

    private static Item register(String name, Item item) {
        return Registry.register(Registries.ITEM, Identifier.of(GlowMod.MOD_ID, name), item);
    }

    /** Loads the class (so the fields above register) and puts the items into the creative tabs. */
    public static void initialize() {
        ItemGroupEvents.modifyEntriesEvent(ItemGroups.TOOLS).register(entries -> entries.add(LANTERN_STAFF));
        ItemGroupEvents.modifyEntriesEvent(ItemGroups.INGREDIENTS).register(entries -> {
            entries.add(EMBER_SHARD);
            entries.add(WISP_HEART);
        });
    }
}
