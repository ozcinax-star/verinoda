package net.ashvale.emberforge;

import com.mojang.logging.LogUtils;
import net.ashvale.emberforge.config.EmberConfig;
import net.ashvale.emberforge.registry.ModBlocks;
import net.ashvale.emberforge.registry.ModItems;
import net.ashvale.emberforge.registry.ModMenus;
import net.ashvale.emberforge.registry.ModRecipes;
import net.minecraft.resources.ResourceLocation;
import net.neoforged.bus.api.IEventBus;
import net.neoforged.fml.ModContainer;
import net.neoforged.fml.common.Mod;
import net.neoforged.fml.config.ModConfig;
import org.slf4j.Logger;

@Mod(EmberForge.MOD_ID)
public final class EmberForge {
    public static final String MOD_ID = "emberforge";
    public static final Logger LOGGER = LogUtils.getLogger();

    public EmberForge(IEventBus modBus, ModContainer container) {
        ModBlocks.BLOCKS.register(modBus);
        ModBlocks.BLOCK_ENTITIES.register(modBus);
        ModItems.ITEMS.register(modBus);
        ModItems.CREATIVE_TABS.register(modBus);
        ModMenus.MENUS.register(modBus);
        ModRecipes.RECIPE_TYPES.register(modBus);
        ModRecipes.RECIPE_SERIALIZERS.register(modBus);

        container.registerConfig(ModConfig.Type.COMMON, EmberConfig.SPEC);
        LOGGER.info("Emberforge is heating up");
    }

    public static ResourceLocation id(String path) {
        return ResourceLocation.fromNamespaceAndPath(MOD_ID, path);
    }
}
