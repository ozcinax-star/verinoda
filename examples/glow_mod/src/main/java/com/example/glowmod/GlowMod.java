package com.example.glowmod;

import com.example.glowmod.command.GlowCommands;
import com.example.glowmod.config.GlowConfig;
import com.example.glowmod.entity.ModEntities;
import com.example.glowmod.event.LanternEvents;
import com.example.glowmod.item.ModItems;
import com.example.glowmod.network.ServerNetworking;
import com.example.glowmod.repair.RepairScheduler;
import com.example.glowmod.world.WispSpawner;
import net.fabricmc.api.ModInitializer;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

public class GlowMod implements ModInitializer {
    public static final String MOD_ID = "glowmod";
    public static final Logger LOGGER = LoggerFactory.getLogger(MOD_ID);

    @Override
    public void onInitialize() {
        GlowConfig.load();
        ModItems.initialize();
        ModEntities.register();
        GlowCommands.register();
        LanternEvents.etkinlestir();
        WispSpawner.register();
        ServerNetworking.register();
        RepairScheduler.register();
        LOGGER.info("Glow Mod ready");
    }
}
