package net.ashvale.emberforge.network;

import net.ashvale.emberforge.EmberForge;
import net.ashvale.emberforge.block.EmberForgeBlockEntity;
import net.ashvale.emberforge.menu.EmberForgeMenu;
import net.minecraft.server.level.ServerPlayer;
import net.neoforged.bus.api.SubscribeEvent;
import net.neoforged.fml.common.EventBusSubscriber;
import net.neoforged.neoforge.network.event.RegisterPayloadHandlersEvent;
import net.neoforged.neoforge.network.handling.IPayloadContext;
import net.neoforged.neoforge.network.registration.PayloadRegistrar;

@EventBusSubscriber(modid = EmberForge.MOD_ID, bus = EventBusSubscriber.Bus.MOD)
public final class EmberNetwork {
    private static final String PROTOCOL = "1";
    private static final double MAX_DISTANCE_SQR = 8.0 * 8.0;

    private EmberNetwork() {
    }

    @SubscribeEvent
    public static void register(RegisterPayloadHandlersEvent event) {
        PayloadRegistrar registrar = event.registrar(PROTOCOL);
        registrar.playToServer(StokeForgePayload.TYPE, StokeForgePayload.STREAM_CODEC, EmberNetwork::handleStoke);
    }

    /** Runs on the server thread. Ignores the packet unless that forge's menu is open and close by. */
    static void handleStoke(StokeForgePayload payload, IPayloadContext context) {
        if (!(context.player() instanceof ServerPlayer player)) {
            return;
        }
        if (!(player.containerMenu instanceof EmberForgeMenu menu) || !menu.getPos().equals(payload.pos())) {
            return;
        }
        if (player.distanceToSqr(payload.pos().getCenter()) > MAX_DISTANCE_SQR) {
            return;
        }
        if (player.level().getBlockEntity(payload.pos()) instanceof EmberForgeBlockEntity forge) {
            forge.stoke(player);
        }
    }
}
