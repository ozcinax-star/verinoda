package com.example.glowmod.client;

import com.example.glowmod.network.SummonWispPayload;
import net.fabricmc.api.ClientModInitializer;
import net.fabricmc.fabric.api.client.event.lifecycle.v1.ClientTickEvents;
import net.fabricmc.fabric.api.client.keybinding.v1.KeyBindingHelper;
import net.fabricmc.fabric.api.client.networking.v1.ClientPlayNetworking;
import net.minecraft.client.MinecraftClient;
import net.minecraft.client.option.KeyBinding;
import net.minecraft.client.util.InputUtil;
import net.minecraft.util.hit.BlockHitResult;
import net.minecraft.util.hit.HitResult;
import org.lwjgl.glfw.GLFW;

public class GlowModClient implements ClientModInitializer {
    private static KeyBinding summonKey;

    @Override
    public void onInitializeClient() {
        summonKey = KeyBindingHelper.registerKeyBinding(new KeyBinding(
                "key.glowmod.summon_wisp", InputUtil.Type.KEYSYM, GLFW.GLFW_KEY_G, "category.glowmod"));
        ClientTickEvents.END_CLIENT_TICK.register(GlowModClient::onEndTick);
    }

    private static void onEndTick(MinecraftClient client) {
        while (summonKey.wasPressed()) {
            if (client.crosshairTarget instanceof BlockHitResult hit && hit.getType() == HitResult.Type.BLOCK) {
                ClientPlayNetworking.send(new SummonWispPayload(hit.getBlockPos()));
            }
        }
    }
}
