package net.ashvale.emberforge;

import static org.junit.jupiter.api.Assertions.assertEquals;

import net.ashvale.emberforge.block.EmberForgeBlockEntity;
import net.ashvale.emberforge.network.StokeForgePayload;
import org.junit.jupiter.api.Test;

class ForgeTickTest {
    @Test
    void forgeCoolsOnlyOnDecayTicks() {
        assertEquals(49, EmberForgeBlockEntity.cooled(50, 40, 20));
        assertEquals(50, EmberForgeBlockEntity.cooled(50, 41, 20));
    }

    @Test
    void coldForgeStaysCold() {
        assertEquals(0, EmberForgeBlockEntity.cooled(0, 40, 20));
    }

    @Test
    void stokePayloadUsesTheModNamespace() {
        assertEquals("emberforge:stoke_forge", StokeForgePayload.TYPE.id().toString());
    }
}
