package net.ashvale.emberforge.registry;

import java.util.function.Supplier;

import net.ashvale.emberforge.EmberForge;
import net.ashvale.emberforge.menu.EmberForgeMenu;
import net.minecraft.core.registries.Registries;
import net.minecraft.world.inventory.MenuType;
import net.neoforged.neoforge.common.extensions.IMenuTypeExtension;
import net.neoforged.neoforge.registries.DeferredRegister;

public final class ModMenus {
    public static final DeferredRegister<MenuType<?>> MENUS = DeferredRegister.create(Registries.MENU, EmberForge.MOD_ID);

    public static final Supplier<MenuType<EmberForgeMenu>> EMBER_FORGE =
            MENUS.register("ember_forge", () -> IMenuTypeExtension.create(EmberForgeMenu::new));

    private ModMenus() {
    }
}
