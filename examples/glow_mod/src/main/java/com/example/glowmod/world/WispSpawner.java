package com.example.glowmod.world;

import com.example.glowmod.config.GlowConfig;
import com.example.glowmod.entity.Wisp;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerChunkEvents;
import net.minecraft.registry.tag.BiomeTags;
import net.minecraft.server.world.ServerWorld;
import net.minecraft.util.math.BlockPos;
import net.minecraft.util.math.ChunkPos;
import net.minecraft.world.Heightmap;
import net.minecraft.world.chunk.WorldChunk;

/** Natural wisps: a freshly loaded swamp chunk at night sometimes gets one above its surface. */
public final class WispSpawner {
    private WispSpawner() {
    }

    public static void register() {
        ServerChunkEvents.CHUNK_LOAD.register(WispSpawner::onChunkLoad);
    }

    private static void onChunkLoad(ServerWorld world, WorldChunk chunk) {
        if (world.isDay() || world.random.nextDouble() >= GlowConfig.get().naturalChance()) {
            return;
        }
        ChunkPos chunkPos = chunk.getPos();
        int x = chunkPos.getStartX() + world.random.nextInt(16);
        int z = chunkPos.getStartZ() + world.random.nextInt(16);
        BlockPos top = world.getTopPosition(Heightmap.Type.MOTION_BLOCKING_NO_LEAVES, new BlockPos(x, 0, z));
        if (world.getBiome(top).isIn(BiomeTags.ALLOWS_SURFACE_SLIME_SPAWNS)) {
            Wisp.spawn(world, top.up());
        }
    }
}
