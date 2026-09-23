package com.example.glowmod.network;

import com.example.glowmod.GlowMod;
import com.example.glowmod.config.GlowConfig;
import com.example.glowmod.entity.Wisp;
import com.example.glowmod.item.ModItems;
import net.fabricmc.fabric.api.networking.v1.PayloadTypeRegistry;
import net.fabricmc.fabric.api.networking.v1.ServerPlayNetworking;
import net.minecraft.server.network.ServerPlayerEntity;

public final class ServerNetworking {
    private ServerNetworking() {
    }

    public static void register() {
        PayloadTypeRegistry.playC2S().register(SummonWispPayload.ID, SummonWispPayload.CODEC);
        ServerPlayNetworking.registerGlobalReceiver(SummonWispPayload.ID, ServerNetworking::onSummonWisp);
    }

    private static void onSummonWisp(SummonWispPayload payload, ServerPlayNetworking.Context context) {
        ServerPlayerEntity player = context.player();
        if (!player.getMainHandStack().isOf(ModItems.LANTERN_STAFF)) {
            return;
        }
        if (!player.getBlockPos().isWithinDistance(payload.pos(), GlowConfig.get().maxSummonDistance())) {
            GlowMod.LOGGER.warn("{} asked for a wisp too far away at {}", player.getName().getString(), payload.pos());
            return;
        }
        Wisp.spawn(player.getServerWorld(), payload.pos().up());
    }
}
