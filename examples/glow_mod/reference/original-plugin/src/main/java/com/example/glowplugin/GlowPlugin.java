package com.example.glowplugin;

import org.bukkit.plugin.java.JavaPlugin;

public final class GlowPlugin extends JavaPlugin {
    @Override
    public void onEnable() {
        saveDefaultConfig();
        getServer().getPluginManager().registerEvents(new WispListener(this), this);
        getLogger().info("GlowPlugin enabled");
    }

    public int blocksPerTick() {
        return getConfig().getInt("repair.blocks-per-tick", 4);
    }
}
