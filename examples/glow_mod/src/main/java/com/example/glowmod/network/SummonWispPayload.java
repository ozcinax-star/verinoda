package com.example.glowmod.network;

import com.example.glowmod.GlowMod;
import net.minecraft.network.RegistryByteBuf;
import net.minecraft.network.codec.PacketCodec;
import net.minecraft.network.packet.CustomPayload;
import net.minecraft.util.Identifier;
import net.minecraft.util.math.BlockPos;

/** Client -> server: "call a wisp at this block". */
public record SummonWispPayload(BlockPos pos) implements CustomPayload {
    public static final CustomPayload.Id<SummonWispPayload> ID =
            new CustomPayload.Id<>(Identifier.of(GlowMod.MOD_ID, "summon_wisp"));
    public static final PacketCodec<RegistryByteBuf, SummonWispPayload> CODEC =
            PacketCodec.tuple(BlockPos.PACKET_CODEC, SummonWispPayload::pos, SummonWispPayload::new);

    @Override
    public Id<? extends CustomPayload> getId() {
        return ID;
    }
}
