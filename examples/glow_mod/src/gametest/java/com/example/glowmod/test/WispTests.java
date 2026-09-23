package com.example.glowmod.test;

import com.example.glowmod.entity.ModEntities;
import com.example.glowmod.entity.Wisp;
import com.example.glowmod.item.ModItems;
import net.fabricmc.fabric.api.gametest.v1.FabricGameTest;
import net.minecraft.test.GameTest;
import net.minecraft.test.TestContext;
import net.minecraft.util.math.BlockPos;

public class WispTests implements FabricGameTest {
    private static final BlockPos AIR = new BlockPos(1, 2, 1);

    @GameTest(templateName = EMPTY_STRUCTURE)
    public void wispAppearsWhereAsked(TestContext context) {
        Wisp wisp = Wisp.spawn(context.getWorld(), context.getAbsolutePos(AIR));
        context.assertTrue(wisp != null, "no wisp was created");
        context.expectEntityAt(ModEntities.WISP, AIR);
        context.complete();
    }

    @GameTest(templateName = EMPTY_STRUCTURE)
    public void deadWispLeavesItsHeart(TestContext context) {
        Wisp wisp = context.spawnEntity(ModEntities.WISP, AIR);
        wisp.kill();
        context.expectItemAt(ModItems.WISP_HEART, AIR, 2.0);
        context.complete();
    }
}
