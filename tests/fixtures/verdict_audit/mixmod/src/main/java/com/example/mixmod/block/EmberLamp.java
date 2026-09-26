package com.example.mixmod.block;

import java.util.ArrayList;
import java.util.List;
import net.minecraft.server.world.ServerWorld;
import net.minecraft.util.math.BlockPos;

/** A lamp block that cools down a little every server tick after it was lit. */
public final class EmberLamp {
    private static final List<EmberLamp> LIT = new ArrayList<>();

    private final BlockPos pos;
    private int heat;

    public EmberLamp(BlockPos pos, int heat) {
        this.pos = pos;
        this.heat = heat;
        LIT.add(this);
    }

    public void tick(ServerWorld world) {
        heat = Math.max(0, heat - 1);
        if (heat == 0) {
            LIT.remove(this);
        }
    }

    public static void coolAll(ServerWorld world) {
        for (EmberLamp lamp : new ArrayList<>(LIT)) {
            lamp.tick(world);
        }
    }

    public BlockPos pos() {
        return pos;
    }
}
