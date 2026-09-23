package net.ashvale.emberforge.heat

import net.ashvale.emberforge.config.EmberConfig
import net.ashvale.emberforge.registry.ModTags
import net.minecraft.world.item.ItemStack
import net.minecraft.world.item.Items

/** Heat rules of the Ember Forge. */
object HeatMath {
    private const val HEAT_PER_EXTRA_STEP = 50
    private const val MAX_STEPS = 4

    /** Heat one fuel item adds; blaze rods count double. 0 for items that are not forge fuel. */
    @JvmStatic
    fun heatForFuel(stack: ItemStack): Int {
        if (stack.isEmpty || !stack.`is`(ModTags.FORGE_FUELS)) return 0
        val base = EmberConfig.HEAT_PER_FUEL.get()
        return if (stack.`is`(Items.BLAZE_ROD)) base * 2 else base
    }

    @JvmStatic
    fun addHeat(current: Int, amount: Int): Int = addHeat(current, amount, EmberConfig.MAX_HEAT.get())

    @JvmStatic
    fun addHeat(current: Int, amount: Int, maxHeat: Int): Int = (current + amount).coerceIn(0, maxHeat)

    /**
     * Forging progress per tick: nothing below the recipe's minimum heat, then 1,
     * plus 1 for every full 50 heat above the minimum, at most 4.
     */
    @JvmStatic
    fun progressPerTick(heat: Int, minHeat: Int): Int {
        if (heat < minHeat) return 0
        return (1 + (heat - minHeat) / HEAT_PER_EXTRA_STEP).coerceAtMost(MAX_STEPS)
    }
}
