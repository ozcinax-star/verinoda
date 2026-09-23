package net.ashvale.emberforge.recipe;

import com.mojang.serialization.Codec;
import com.mojang.serialization.MapCodec;
import com.mojang.serialization.codecs.RecordCodecBuilder;
import net.ashvale.emberforge.registry.ModRecipes;
import net.minecraft.core.HolderLookup;
import net.minecraft.core.NonNullList;
import net.minecraft.network.RegistryFriendlyByteBuf;
import net.minecraft.network.codec.ByteBufCodecs;
import net.minecraft.network.codec.StreamCodec;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.crafting.Ingredient;
import net.minecraft.world.item.crafting.Recipe;
import net.minecraft.world.item.crafting.RecipeSerializer;
import net.minecraft.world.item.crafting.RecipeType;
import net.minecraft.world.item.crafting.SingleRecipeInput;
import net.minecraft.world.level.Level;

/**
 * One ingredient becomes the result after forge_time progress points. Progress only
 * grows while the forge has at least min_heat heat.
 */
public record ForgingRecipe(Ingredient ingredient, ItemStack result, int forgeTime, int minHeat)
        implements Recipe<SingleRecipeInput> {

    @Override
    public boolean matches(SingleRecipeInput input, Level level) {
        return ingredient.test(input.item());
    }

    @Override
    public ItemStack assemble(SingleRecipeInput input, HolderLookup.Provider registries) {
        return result.copy();
    }

    @Override
    public boolean canCraftInDimensions(int width, int height) {
        return true;
    }

    @Override
    public ItemStack getResultItem(HolderLookup.Provider registries) {
        return result;
    }

    @Override
    public NonNullList<Ingredient> getIngredients() {
        return NonNullList.of(Ingredient.EMPTY, ingredient);
    }

    @Override
    public RecipeSerializer<?> getSerializer() {
        return ModRecipes.FORGING_SERIALIZER.get();
    }

    @Override
    public RecipeType<?> getType() {
        return ModRecipes.FORGING_TYPE.get();
    }

    public static final class Serializer implements RecipeSerializer<ForgingRecipe> {
        private static final MapCodec<ForgingRecipe> CODEC = RecordCodecBuilder.mapCodec(i -> i.group(
                Ingredient.CODEC_NONEMPTY.fieldOf("ingredient").forGetter(ForgingRecipe::ingredient),
                ItemStack.STRICT_CODEC.fieldOf("result").forGetter(ForgingRecipe::result),
                Codec.INT.optionalFieldOf("forge_time", 200).forGetter(ForgingRecipe::forgeTime),
                Codec.INT.optionalFieldOf("min_heat", 0).forGetter(ForgingRecipe::minHeat)
        ).apply(i, ForgingRecipe::new));

        private static final StreamCodec<RegistryFriendlyByteBuf, ForgingRecipe> STREAM_CODEC = StreamCodec.composite(
                Ingredient.CONTENTS_STREAM_CODEC, ForgingRecipe::ingredient,
                ItemStack.STREAM_CODEC, ForgingRecipe::result,
                ByteBufCodecs.VAR_INT, ForgingRecipe::forgeTime,
                ByteBufCodecs.VAR_INT, ForgingRecipe::minHeat,
                ForgingRecipe::new);

        @Override
        public MapCodec<ForgingRecipe> codec() {
            return CODEC;
        }

        @Override
        public StreamCodec<RegistryFriendlyByteBuf, ForgingRecipe> streamCodec() {
            return STREAM_CODEC;
        }
    }
}
