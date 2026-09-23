package net.ashvale.emberforge.network;

import io.netty.buffer.ByteBuf;
import net.ashvale.emberforge.EmberForge;
import net.minecraft.core.BlockPos;
import net.minecraft.network.codec.StreamCodec;
import net.minecraft.network.protocol.common.custom.CustomPacketPayload;

/** Client to server: the player pressed Stoke in the forge screen at {@code pos}. */
public record StokeForgePayload(BlockPos pos) implements CustomPacketPayload {
    public static final Type<StokeForgePayload> TYPE = new Type<>(EmberForge.id("stoke_forge"));
    public static final StreamCodec<ByteBuf, StokeForgePayload> STREAM_CODEC =
            BlockPos.STREAM_CODEC.map(StokeForgePayload::new, StokeForgePayload::pos);

    @Override
    public Type<? extends CustomPacketPayload> type() {
        return TYPE;
    }
}
