package com.example.glowplugin;

import net.kyori.adventure.text.Component;
import net.kyori.adventure.text.format.NamedTextColor;
import org.bukkit.Location;
import org.bukkit.Material;
import org.bukkit.NamespacedKey;
import org.bukkit.entity.Allay;
import org.bukkit.entity.EntityType;
import org.bukkit.inventory.ItemStack;
import org.bukkit.persistence.PersistentDataType;
import org.bukkit.plugin.Plugin;

/** Paper has no custom entities, so a wisp is a named Allay with a marker key. */
public final class Wisp {
    private Wisp() {
    }

    public static Allay spawn(Plugin plugin, Location location) {
        Allay allay = (Allay) location.getWorld().spawnEntity(location, EntityType.ALLAY);
        allay.customName(Component.text("Wisp", NamedTextColor.AQUA));
        allay.getPersistentDataContainer().set(new NamespacedKey(plugin, "wisp"), PersistentDataType.BYTE, (byte) 1);
        return allay;
    }

    public static boolean isWisp(Plugin plugin, org.bukkit.entity.Entity entity) {
        return entity.getPersistentDataContainer().has(new NamespacedKey(plugin, "wisp"), PersistentDataType.BYTE);
    }

    public static ItemStack heart() {
        ItemStack stack = new ItemStack(Material.GLOWSTONE_DUST);
        stack.editMeta(meta -> meta.displayName(Component.text("Wisp Heart", NamedTextColor.AQUA)));
        return stack;
    }
}
