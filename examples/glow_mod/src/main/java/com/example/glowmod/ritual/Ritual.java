package com.example.glowmod.ritual;

import com.example.glowmod.config.GlowConfig;
import com.example.glowmod.entity.Wisp;
import com.example.glowmod.repair.RepairScheduler;
import com.example.glowmod.util.Datapack;
import net.minecraft.block.BlockState;
import net.minecraft.block.Blocks;
import net.minecraft.registry.tag.BlockTags;
import net.minecraft.server.world.ServerWorld;
import net.minecraft.util.math.BlockPos;
import net.minecraft.util.math.Vec3d;

/** The lantern ritual: the data pack plays the effects, the mod clears the circle and calls the wisps. */
public final class Ritual {
    private static final int RADIUS = 3;

    private Ritual() {
    }

    public static void baslat(ServerWorld world, BlockPos altar) {
        Datapack.runQualified(world, Vec3d.ofBottomCenter(altar), "glowmod:ritual/start");
        alaniAc(world, altar);
        int count = GlowConfig.get().ritualWispCount();
        for (int i = 0; i < count; i++) {
            double angle = 2 * Math.PI * i / count;
            BlockPos at = altar.add((int) Math.round(Math.cos(angle) * RADIUS), 1,
                    (int) Math.round(Math.sin(angle) * RADIUS));
            Wisp.spawn(world, at);
        }
    }

    /** Burns away grass and flowers around the altar; RepairScheduler puts them back later. */
    private static void alaniAc(ServerWorld world, BlockPos altar) {
        for (BlockPos pos : BlockPos.iterate(altar.add(-RADIUS, 1, -RADIUS), altar.add(RADIUS, 2, RADIUS))) {
            BlockState state = world.getBlockState(pos);
            if (state.isIn(BlockTags.REPLACEABLE_BY_TREES)) {
                RepairScheduler.kaydet(world, pos.toImmutable(), state);
                world.setBlockState(pos, Blocks.AIR.getDefaultState());
            }
        }
    }
}
