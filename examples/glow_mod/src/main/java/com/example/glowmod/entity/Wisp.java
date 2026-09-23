package com.example.glowmod.entity;

import com.example.glowmod.util.Datapack;
import net.minecraft.entity.EntityType;
import net.minecraft.entity.attribute.DefaultAttributeContainer;
import net.minecraft.entity.attribute.EntityAttributes;
import net.minecraft.entity.damage.DamageSource;
import net.minecraft.entity.mob.MobEntity;
import net.minecraft.entity.mob.PathAwareEntity;
import net.minecraft.server.world.ServerWorld;
import net.minecraft.util.math.BlockPos;
import net.minecraft.world.World;

public class Wisp extends PathAwareEntity {
    public Wisp(EntityType<? extends Wisp> type, World world) {
        super(type, world);
        setNoGravity(true);
    }

    public static DefaultAttributeContainer.Builder createWispAttributes() {
        return MobEntity.createMobAttributes()
                .add(EntityAttributes.GENERIC_MAX_HEALTH, 6.0)
                .add(EntityAttributes.GENERIC_FLYING_SPEED, 0.6)
                .add(EntityAttributes.GENERIC_MOVEMENT_SPEED, 0.3);
    }

    /** Puts a new wisp in the middle of the block at {@code pos}; null when the type refuses to create one. */
    public static Wisp spawn(ServerWorld world, BlockPos pos) {
        Wisp wisp = ModEntities.WISP.create(world);
        if (wisp == null) {
            return null;
        }
        wisp.refreshPositionAndAngles(pos.getX() + 0.5, pos.getY(), pos.getZ() + 0.5, 0.0F, 0.0F);
        world.spawnEntity(wisp);
        return wisp;
    }

    @Override
    public void onDeath(DamageSource damageSource) {
        super.onDeath(damageSource);
        if (getWorld() instanceof ServerWorld serverWorld) {
            Datapack.run(serverWorld, getPos(), "wisp_death");
        }
    }
}
