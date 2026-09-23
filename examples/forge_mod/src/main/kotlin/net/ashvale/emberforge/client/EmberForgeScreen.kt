package net.ashvale.emberforge.client

import net.ashvale.emberforge.EmberForge
import net.ashvale.emberforge.menu.EmberForgeMenu
import net.ashvale.emberforge.network.StokeForgePayload
import net.minecraft.client.gui.GuiGraphics
import net.minecraft.client.gui.components.Button
import net.minecraft.client.gui.screens.inventory.AbstractContainerScreen
import net.minecraft.network.chat.Component
import net.minecraft.world.entity.player.Inventory
import net.neoforged.neoforge.network.PacketDistributor

class EmberForgeScreen(menu: EmberForgeMenu, inventory: Inventory, title: Component) :
    AbstractContainerScreen<EmberForgeMenu>(menu, inventory, title) {

    override fun init() {
        super.init()
        addRenderableWidget(
            Button.builder(Component.translatable("gui.emberforge.stoke")) { requestStoke() }
                .bounds(leftPos + 120, topPos + 58, 44, 16)
                .build()
        )
    }

    private fun requestStoke() {
        PacketDistributor.sendToServer(StokeForgePayload(menu.pos))
    }

    override fun renderBg(graphics: GuiGraphics, partialTick: Float, mouseX: Int, mouseY: Int) {
        graphics.blit(TEXTURE, leftPos, topPos, 0, 0, imageWidth, imageHeight)
        graphics.blit(TEXTURE, leftPos + 79, topPos + 34, 176, 14, menu.progressWidth(24), 17)
        val heatHeight = if (menu.maxHeat == 0) 0 else menu.heat * 52 / menu.maxHeat
        graphics.blit(TEXTURE, leftPos + 30, topPos + 70 - heatHeight, 176, 83 - heatHeight, 8, heatHeight)
    }

    override fun render(graphics: GuiGraphics, mouseX: Int, mouseY: Int, partialTick: Float) {
        super.render(graphics, mouseX, mouseY, partialTick)
        renderTooltip(graphics, mouseX, mouseY)
    }

    companion object {
        private val TEXTURE = EmberForge.id("textures/gui/ember_forge.png")
    }
}
