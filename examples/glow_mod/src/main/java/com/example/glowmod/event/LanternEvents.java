package com.example.glowmod.event;

import com.example.glowmod.entity.Wisp;
import com.example.glowmod.item.ModItems;
import net.fabricmc.fabric.api.event.player.UseBlockCallback;
import net.minecraft.block.Blocks;
import net.minecraft.server.world.ServerWorld;
import net.minecraft.util.ActionResult;
import net.minecraft.util.math.BlockPos;

public final class LanternEvents {
    private static final int STAFF_COOLDOWN_TICKS = 100;

    private LanternEvents() {
    }

    /** Lantern staff + right click on a soul lantern calls a wisp out of the lantern. */
    public static void etkinlestir() {
        UseBlockCallback.EVENT.register((player, world, hand, hit) -> {
            if (world.isClient() || !player.getStackInHand(hand).isOf(ModItems.LANTERN_STAFF)) {
                return ActionResult.PASS;
            }
            BlockPos pos = hit.getBlockPos();
            if (!world.getBlockState(pos).isOf(Blocks.SOUL_LANTERN)) {
                return ActionResult.PASS;
            }
            if (player.getItemCooldownManager().isCoolingDown(ModItems.LANTERN_STAFF)) {
                return ActionResult.FAIL;
            }
            Wisp.spawn((ServerWorld) world, pos.up());
            player.getItemCooldownManager().set(ModItems.LANTERN_STAFF, STAFF_COOLDOWN_TICKS);
            return ActionResult.SUCCESS;
        });
    }
}
