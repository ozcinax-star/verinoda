package com.example.glowmod.command;

import com.example.glowmod.config.GlowConfig;
import com.example.glowmod.entity.Wisp;
import com.example.glowmod.ritual.Ritual;
import com.mojang.brigadier.context.CommandContext;
import net.fabricmc.fabric.api.command.v2.CommandRegistrationCallback;
import net.minecraft.server.command.CommandManager;
import net.minecraft.server.command.ServerCommandSource;
import net.minecraft.text.Text;
import net.minecraft.util.math.BlockPos;

/** /glow wisp | ritual | reload (operators only). */
public final class GlowCommands {
    private GlowCommands() {
    }

    public static void register() {
        CommandRegistrationCallback.EVENT.register((dispatcher, registryAccess, environment) ->
                dispatcher.register(CommandManager.literal("glow")
                        .requires(source -> source.hasPermissionLevel(2))
                        .then(CommandManager.literal("wisp").executes(GlowCommands::wisp))
                        .then(CommandManager.literal("ritual").executes(GlowCommands::ritual))
                        .then(CommandManager.literal("reload").executes(GlowCommands::reload))));
    }

    private static int wisp(CommandContext<ServerCommandSource> ctx) {
        ServerCommandSource source = ctx.getSource();
        Wisp wisp = Wisp.spawn(source.getWorld(), BlockPos.ofFloored(source.getPosition()));
        if (wisp == null) {
            source.sendError(Text.translatable("command.glowmod.wisp.failed"));
            return 0;
        }
        source.sendFeedback(() -> Text.translatable("command.glowmod.wisp.spawned"), false);
        return 1;
    }

    private static int ritual(CommandContext<ServerCommandSource> ctx) {
        ServerCommandSource source = ctx.getSource();
        Ritual.baslat(source.getWorld(), BlockPos.ofFloored(source.getPosition()));
        return 1;
    }

    private static int reload(CommandContext<ServerCommandSource> ctx) {
        GlowConfig.load();
        ctx.getSource().sendFeedback(() -> Text.translatable("command.glowmod.reload.done"), true);
        return 1;
    }
}
