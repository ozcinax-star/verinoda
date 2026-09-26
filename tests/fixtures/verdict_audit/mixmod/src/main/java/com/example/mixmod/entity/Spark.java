package com.example.mixmod.entity;

import net.minecraft.entity.EntityType;
import net.minecraft.entity.mob.PathAwareEntity;
import net.minecraft.server.world.ServerWorld;
import net.minecraft.util.math.BlockPos;
import net.minecraft.world.World;

/** A small floating light that follows players at night. */
public class Spark extends PathAwareEntity {
    public Spark(EntityType<? extends Spark> type, World world) {
        super(type, world);
        setNoGravity(true);
    }

    public static Spark spawn(ServerWorld world, BlockPos pos, EntityType<Spark> type) {
        Spark spark = type.create(world);
        if (spark == null) {
            return null;
        }
        spark.refreshPositionAndAngles(pos.getX() + 0.5, pos.getY(), pos.getZ() + 0.5, 0.0F, 0.0F);
        world.spawnEntity(spark);
        return spark;
    }
}
