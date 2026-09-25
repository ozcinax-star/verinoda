package com.example.glowmod;

import static org.junit.jupiter.api.Assertions.assertEquals;

import com.example.glowmod.entity.Wisp;
import java.util.ArrayList;
import org.junit.jupiter.api.Test;

class WispTest {
    @Test
    void tickMovesTheWisp() {
        new Wisp(new ArrayList<>()).tick();
    }

    @Test
    void glowLevelCountsTrail() {
        assertEquals(3, new Wisp(new ArrayList<>()).glowLevel(1));
    }
}
