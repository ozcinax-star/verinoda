package net.ashvale.emberforge.block;

import java.util.Optional;

import net.ashvale.emberforge.EmberForge;
import net.ashvale.emberforge.config.EmberConfig;
import net.ashvale.emberforge.heat.HeatMath;
import net.ashvale.emberforge.menu.EmberForgeMenu;
import net.ashvale.emberforge.recipe.ForgingRecipe;
import net.ashvale.emberforge.registry.ModBlocks;
import net.ashvale.emberforge.registry.ModRecipes;
import net.ashvale.emberforge.registry.ModTags;
import net.minecraft.core.BlockPos;
import net.minecraft.core.HolderLookup;
import net.minecraft.nbt.CompoundTag;
import net.minecraft.network.chat.Component;
import net.minecraft.world.MenuProvider;
import net.minecraft.world.entity.player.Inventory;
import net.minecraft.world.entity.player.Player;
import net.minecraft.world.inventory.AbstractContainerMenu;
import net.minecraft.world.inventory.ContainerData;
import net.minecraft.world.item.ItemStack;
import net.minecraft.world.item.crafting.RecipeHolder;
import net.minecraft.world.item.crafting.SingleRecipeInput;
import net.minecraft.world.level.Level;
import net.minecraft.world.level.block.entity.BlockEntity;
import net.minecraft.world.level.block.state.BlockState;
import net.neoforged.neoforge.items.ItemStackHandler;

/**
 * Inventory (input, fuel, output), heat and forging progress of one Ember Forge.
 */
public class EmberForgeBlockEntity extends BlockEntity implements MenuProvider {
    public static final int SLOT_INPUT = 0;
    public static final int SLOT_FUEL = 1;
    public static final int SLOT_OUTPUT = 2;

    private final ItemStackHandler items = new ItemStackHandler(3) {
        @Override
        protected void onContentsChanged(int slot) {
            setChanged();
        }
    };
    private int heat;
    private int progress;
    private int forgeTime;

    private final ContainerData data = new ContainerData() {
        @Override
        public int get(int index) {
            return switch (index) {
                case 0 -> heat;
                case 1 -> EmberConfig.MAX_HEAT.get();
                case 2 -> progress;
                case 3 -> forgeTime;
                default -> 0;
            };
        }

        @Override
        public void set(int index, int value) {
            // the server owns these values; the client menu keeps its own copy
        }

        @Override
        public int getCount() {
            return 4;
        }
    };

    public EmberForgeBlockEntity(BlockPos pos, BlockState state) {
        super(ModBlocks.EMBER_FORGE_ENTITY.get(), pos, state);
    }

    public static void serverTick(Level level, BlockPos pos, BlockState state, EmberForgeBlockEntity forge) {
        boolean changed = false;

        ItemStack fuel = forge.items.getStackInSlot(SLOT_FUEL);
        int fuelHeat = HeatMath.heatForFuel(fuel);
        if (fuelHeat > 0 && forge.heat + fuelHeat <= EmberConfig.MAX_HEAT.get()) {
            forge.items.extractItem(SLOT_FUEL, 1, false);
            forge.heat = HeatMath.addHeat(forge.heat, fuelHeat);
            changed = true;
        }

        if (level.getGameTime() % 40 == 0 && level.getBlockState(pos.below()).is(ModTags.FORGE_HEAT_SOURCES)) {
            forge.heat = HeatMath.addHeat(forge.heat, 2);
            changed = true;
        }

        ItemStack input = forge.items.getStackInSlot(SLOT_INPUT);
        Optional<RecipeHolder<ForgingRecipe>> match = level.getRecipeManager()
                .getRecipeFor(ModRecipes.FORGING_TYPE.get(), new SingleRecipeInput(input), level);
        if (match.isPresent() && forge.canOutput(match.get().value().result())) {
            ForgingRecipe recipe = match.get().value();
            forge.forgeTime = recipe.forgeTime();
            forge.progress += HeatMath.progressPerTick(forge.heat, recipe.minHeat());
            if (forge.progress >= recipe.forgeTime()) {
                forge.finish(recipe);
            }
            changed = true;
        } else if (forge.progress != 0) {
            forge.progress = 0;
            changed = true;
        }

        int cooled = cooled(forge.heat, level.getGameTime(), EmberConfig.DECAY_INTERVAL.get());
        if (cooled != forge.heat) {
            forge.heat = cooled;
            changed = true;
        }

        boolean lit = forge.heat > 0;
        if (state.getValue(EmberForgeBlock.LIT) != lit) {
            level.setBlock(pos, state.setValue(EmberForgeBlock.LIT, lit), 3);
        }
        if (changed) {
            setChanged(level, pos, state);
        }
    }

    /** Heat after cooling: one less on every decayInterval-th game tick, never below 0. */
    public static int cooled(int heat, long gameTime, int decayInterval) {
        return heat > 0 && gameTime % decayInterval == 0 ? heat - 1 : heat;
    }

    private boolean canOutput(ItemStack result) {
        ItemStack out = items.getStackInSlot(SLOT_OUTPUT);
        return out.isEmpty() || (ItemStack.isSameItemSameComponents(out, result)
                && out.getCount() + result.getCount() <= out.getMaxStackSize());
    }

    private void finish(ForgingRecipe recipe) {
        items.extractItem(SLOT_INPUT, 1, false);
        items.insertItem(SLOT_OUTPUT, recipe.result().copy(), false);
        progress = 0;
        EmberForge.LOGGER.debug("Ember Forge at {} forged {}", worldPosition, recipe.result());
    }

    public void stoke(Player player) {
        heat = HeatMath.addHeat(heat, EmberConfig.STOKE_HEAT.get());
        setChanged();
        player.displayClientMessage(Component.translatable("message.emberforge.stoked", heat), true);
    }

    @Override
    protected void loadAdditional(CompoundTag tag, HolderLookup.Provider registries) {
        super.loadAdditional(tag, registries);
        items.deserializeNBT(registries, tag.getCompound("Items"));
        heat = tag.getInt("Heat");
        progress = tag.getInt("Progress");
    }

    @Override
    protected void saveAdditional(CompoundTag tag, HolderLookup.Provider registries) {
        super.saveAdditional(tag, registries);
        tag.put("Items", items.serializeNBT(registries));
        tag.putInt("Heat", heat);
        tag.putInt("Progress", progress);
    }

    public ItemStackHandler getItems() {
        return items;
    }

    public int getHeat() {
        return heat;
    }

    @Override
    public Component getDisplayName() {
        return Component.translatable("container.emberforge.ember_forge");
    }

    @Override
    public AbstractContainerMenu createMenu(int containerId, Inventory inventory, Player player) {
        return new EmberForgeMenu(containerId, inventory, this, data);
    }
}
