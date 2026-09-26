package com.example.mixmod.mixin;

import com.example.mixmod.block.EmberLamp;
import net.minecraft.server.world.ServerWorld;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.injection.At;
import org.spongepowered.asm.mixin.injection.Inject;
import org.spongepowered.asm.mixin.injection.callback.CallbackInfo;

@Mixin(ServerWorld.class)
public abstract class ServerWorldMixin {
    @Inject(method = "tick", at = @At("TAIL"))
    private void mixmod$afterTick(CallbackInfo ci) {
        EmberLamp.coolAll((ServerWorld) (Object) this);
    }
}
