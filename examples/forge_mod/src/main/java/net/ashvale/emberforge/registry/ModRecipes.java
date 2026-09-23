package net.ashvale.emberforge.registry;

import java.util.function.Supplier;

import net.ashvale.emberforge.EmberForge;
import net.ashvale.emberforge.recipe.ForgingRecipe;
import net.minecraft.core.registries.Registries;
import net.minecraft.world.item.crafting.RecipeSerializer;
import net.minecraft.world.item.crafting.RecipeType;
import net.neoforged.neoforge.registries.DeferredRegister;

public final class ModRecipes {
    public static final DeferredRegister<RecipeType<?>> RECIPE_TYPES =
            DeferredRegister.create(Registries.RECIPE_TYPE, EmberForge.MOD_ID);
    public static final DeferredRegister<RecipeSerializer<?>> RECIPE_SERIALIZERS =
            DeferredRegister.create(Registries.RECIPE_SERIALIZER, EmberForge.MOD_ID);

    public static final Supplier<RecipeType<ForgingRecipe>> FORGING_TYPE =
            RECIPE_TYPES.register("forging", () -> RecipeType.simple(EmberForge.id("forging")));
    public static final Supplier<RecipeSerializer<ForgingRecipe>> FORGING_SERIALIZER =
            RECIPE_SERIALIZERS.register("forging", ForgingRecipe.Serializer::new);

    private ModRecipes() {
    }
}
