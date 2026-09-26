package com.example.mixmod.mixin;

import net.minecraft.entity.player.PlayerEntity;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfoReturnable;

@Mixin(PlayerEntity.class)
public abstract class PlayerEntityMixin {
    @Inject(method = "canHarvest", at = @At("HEAD"), cancellable = true)
    private void mixmod$harvestSparks(CallbackInfoReturnable<Boolean> cir) {
        cir.setReturnValue(true);
    }
}
