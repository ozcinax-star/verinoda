package net.ashvale.emberforge;

import static org.junit.jupiter.api.Assertions.assertEquals;

import net.ashvale.emberforge.heat.HeatMath;
import org.junit.jupiter.api.Test;

class HeatMathTest {
    @Test
    void noProgressBelowTheRecipesMinimumHeat() {
        assertEquals(0, HeatMath.progressPerTick(39, 40));
        assertEquals(1, HeatMath.progressPerTick(40, 40));
    }

    @Test
    void progressGrowsWithHeatButIsCapped() {
        assertEquals(2, HeatMath.progressPerTick(90, 40));
        assertEquals(4, HeatMath.progressPerTick(1000, 40));
    }

    @Test
    void heatStaysBetweenZeroAndTheMaximum() {
        assertEquals(200, HeatMath.addHeat(190, 40, 200));
        assertEquals(0, HeatMath.addHeat(5, -10, 200));
    }
}
