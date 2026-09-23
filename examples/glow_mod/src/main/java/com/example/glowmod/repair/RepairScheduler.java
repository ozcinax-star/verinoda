package com.example.glowmod.repair;

import com.example.glowmod.config.GlowConfig;
import java.util.ArrayDeque;
import java.util.Deque;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerLifecycleEvents;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerTickEvents;
import net.minecraft.block.BlockState;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.world.ServerWorld;
import net.minecraft.util.math.BlockPos;

/** Puts back blocks the ritual removed, a few per tick, after a short delay. */
public final class RepairScheduler {
    private record Pending(ServerWorld world, BlockPos pos, BlockState state, long dueTick) {
    }

    private static final Deque<Pending> QUEUE = new ArrayDeque<>();

    private RepairScheduler() {
    }

    public static void register() {
        ServerTickEvents.END_SERVER_TICK.register(RepairScheduler::tick);
        ServerLifecycleEvents.SERVER_STOPPING.register(RepairScheduler::temizle);
    }

    public static void kaydet(ServerWorld world, BlockPos pos, BlockState state) {
        long due = world.getServer().getTicks() + GlowConfig.get().repairDelayTicks();
        QUEUE.addLast(new Pending(world, pos, state, due));
    }

    private static void tick(MinecraftServer server) {
        int budget = GlowConfig.get().blocksPerTick();
        while (budget-- > 0 && !QUEUE.isEmpty() && QUEUE.peekFirst().dueTick() <= server.getTicks()) {
            Pending p = QUEUE.pollFirst();
            p.world().setBlockState(p.pos(), p.state());
        }
    }

    /** The server is stopping: every block still waiting is put back at once, then the queue is emptied. */
    private static void temizle(MinecraftServer server) {
        for (Pending p : QUEUE) {
            p.world().setBlockState(p.pos(), p.state());
        }
        QUEUE.clear();
    }
}
