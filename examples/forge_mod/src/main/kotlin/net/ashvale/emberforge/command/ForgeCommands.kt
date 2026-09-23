package net.ashvale.emberforge.command

import com.mojang.brigadier.CommandDispatcher
import net.ashvale.emberforge.block.EmberForgeBlockEntity
import net.ashvale.emberforge.config.EmberConfig
import net.ashvale.emberforge.util.ForgeFunctions
import net.minecraft.commands.CommandSourceStack
import net.minecraft.commands.Commands
import net.minecraft.commands.arguments.coordinates.BlockPosArgument
import net.minecraft.core.BlockPos
import net.minecraft.network.chat.Component

/** /emberforge reset and /emberforge heat <pos>. */
object ForgeCommands {
    @JvmStatic
    fun register(dispatcher: CommandDispatcher<CommandSourceStack>) {
        dispatcher.register(
            Commands.literal("emberforge")
                .then(
                    Commands.literal("reset")
                        .requires { it.hasPermission(2) && EmberConfig.ALLOW_RESET_COMMAND.get() }
                        .executes { ctx -> if (ForgeFunctions.resetForges(ctx.source)) 1 else 0 }
                )
                .then(
                    Commands.literal("heat")
                        .then(
                            Commands.argument("pos", BlockPosArgument.blockPos())
                                .executes { ctx -> reportHeat(ctx.source, BlockPosArgument.getLoadedBlockPos(ctx, "pos")) }
                        )
                )
        )
    }

    private fun reportHeat(source: CommandSourceStack, pos: BlockPos): Int {
        val forge = source.level.getBlockEntity(pos) as? EmberForgeBlockEntity
        if (forge == null) {
            source.sendFailure(Component.translatable("command.emberforge.heat.not_a_forge"))
            return 0
        }
        source.sendSuccess({ Component.translatable("command.emberforge.heat", forge.heat) }, false)
        return forge.heat
    }
}
