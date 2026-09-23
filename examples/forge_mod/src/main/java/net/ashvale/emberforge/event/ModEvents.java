package net.ashvale.emberforge.event;

import net.ashvale.emberforge.EmberForge;
import net.ashvale.emberforge.block.EmberForgeBlock;
import net.ashvale.emberforge.command.ForgeCommands;
import net.ashvale.emberforge.registry.ModBlocks;
import net.minecraft.world.entity.player.Player;
import net.minecraft.world.level.Level;
import net.minecraft.world.level.block.state.BlockState;
import net.neoforged.bus.api.SubscribeEvent;
import net.neoforged.fml.common.EventBusSubscriber;
import net.neoforged.neoforge.event.RegisterCommandsEvent;
import net.neoforged.neoforge.event.entity.player.PlayerInteractEvent;

@EventBusSubscriber(modid = EmberForge.MOD_ID)
public final class ModEvents {
    private ModEvents() {
    }

    @SubscribeEvent
    public static void onRegisterCommands(RegisterCommandsEvent event) {
        ForgeCommands.register(event.getDispatcher());
    }

    /** Punching a lit forge with an empty hand burns. */
    @SubscribeEvent
    public static void onLeftClickForge(PlayerInteractEvent.LeftClickBlock event) {
        Level level = event.getLevel();
        Player player = event.getEntity();
        BlockState state = level.getBlockState(event.getPos());
        if (!level.isClientSide && state.is(ModBlocks.EMBER_FORGE.get()) && state.getValue(EmberForgeBlock.LIT)
                && player.getMainHandItem().isEmpty()) {
            player.hurt(level.damageSources().hotFloor(), 1.0F);
        }
    }
}
