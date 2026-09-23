package net.ashvale.emberforge.registry;

import java.util.function.Supplier;

import net.ashvale.emberforge.EmberForge;
import net.minecraft.core.registries.Registries;
import net.minecraft.network.chat.Component;
import net.minecraft.world.item.BlockItem;
import net.minecraft.world.item.CreativeModeTab;
import net.minecraft.world.item.Item;
import net.minecraft.world.item.ItemStack;
import net.neoforged.neoforge.registries.DeferredItem;
import net.neoforged.neoforge.registries.DeferredRegister;

public final class ModItems {
    public static final DeferredRegister.Items ITEMS = DeferredRegister.createItems(EmberForge.MOD_ID);
    public static final DeferredRegister<CreativeModeTab> CREATIVE_TABS =
            DeferredRegister.create(Registries.CREATIVE_MODE_TAB, EmberForge.MOD_ID);

    public static final DeferredItem<BlockItem> EMBER_FORGE = ITEMS.registerSimpleBlockItem(ModBlocks.EMBER_FORGE);
    public static final DeferredItem<BlockItem> CINDER_ORE = ITEMS.registerSimpleBlockItem(ModBlocks.CINDER_ORE);
    public static final DeferredItem<Item> CINDER_INGOT = ITEMS.registerSimpleItem("cinder_ingot");
    public static final DeferredItem<Item> FORGE_HAMMER = ITEMS.register("forge_hammer",
            () -> new Item(new Item.Properties().stacksTo(1).durability(250)));

    public static final Supplier<CreativeModeTab> MAIN_TAB = CREATIVE_TABS.register("main",
            () -> CreativeModeTab.builder()
                    .title(Component.translatable("itemGroup.emberforge"))
                    .icon(() -> new ItemStack(EMBER_FORGE.get()))
                    .displayItems((params, output) -> {
                        output.accept(EMBER_FORGE.get());
                        output.accept(CINDER_ORE.get());
                        output.accept(CINDER_INGOT.get());
                        output.accept(FORGE_HAMMER.get());
                    })
                    .build());

    private ModItems() {
    }
}
