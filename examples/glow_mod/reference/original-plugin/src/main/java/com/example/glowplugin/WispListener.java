package com.example.glowplugin;

import org.bukkit.Material;
import org.bukkit.block.Block;
import org.bukkit.event.EventHandler;
import org.bukkit.event.Listener;
import org.bukkit.event.block.Action;
import org.bukkit.event.entity.EntityDeathEvent;
import org.bukkit.event.player.PlayerInteractEvent;

public final class WispListener implements Listener {
    private final GlowPlugin plugin;

    public WispListener(GlowPlugin plugin) {
        this.plugin = plugin;
    }

    @EventHandler
    public void onInteract(PlayerInteractEvent event) {
        Block block = event.getClickedBlock();
        if (event.getAction() != Action.RIGHT_CLICK_BLOCK || block == null || block.getType() != Material.SOUL_LANTERN) {
            return;
        }
        if (event.getItem() != null && event.getItem().getType() == Material.BLAZE_ROD) {
            Wisp.spawn(plugin, block.getLocation().add(0.5, 1, 0.5));
        }
    }

    @EventHandler
    public void onDeath(EntityDeathEvent event) {
        if (Wisp.isWisp(plugin, event.getEntity())) {
            event.getDrops().clear();
            event.getDrops().add(Wisp.heart());
        }
    }
}
