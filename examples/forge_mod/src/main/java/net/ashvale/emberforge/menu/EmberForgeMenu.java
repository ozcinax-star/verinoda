package net.ashvale.emberforge.menu;

import net.ashvale.emberforge.block.EmberForgeBlockEntity;
import net.ashvale.emberforge.registry.ModBlocks;
import net.ashvale.emberforge.registry.ModMenus;
import net.ashvale.emberforge.registry.ModTags;
import net.minecraft.core.BlockPos;
import net.minecraft.network.RegistryFriendlyByteBuf;
import net.minecraft.world.entity.player.Inventory;
import net.minecraft.world.entity.player.Player;
import net.minecraft.world.inventory.AbstractContainerMenu;
import net.minecraft.world.inventory.ContainerData;
import net.minecraft.world.inventory.ContainerLevelAccess;
import net.minecraft.world.inventory.SimpleContainerData;
import net.minecraft.world.inventory.Slot;
import net.minecraft.world.item.ItemStack;
import net.neoforged.neoforge.items.IItemHandler;
import net.neoforged.neoforge.items.ItemStackHandler;
import net.neoforged.neoforge.items.SlotItemHandler;

public class EmberForgeMenu extends AbstractContainerMenu {
    private final ContainerLevelAccess access;
    private final ContainerData data;
    private final BlockPos pos;

    /** Client side, from the open-menu packet (the server writes the forge position into it). */
    public EmberForgeMenu(int containerId, Inventory inventory, RegistryFriendlyByteBuf buf) {
        this(containerId, inventory, buf.readBlockPos(), new ItemStackHandler(3), new SimpleContainerData(4));
    }

    /** Server side, from EmberForgeBlockEntity.createMenu. */
    public EmberForgeMenu(int containerId, Inventory inventory, EmberForgeBlockEntity forge, ContainerData data) {
        this(containerId, inventory, forge.getBlockPos(), forge.getItems(), data);
    }

    private EmberForgeMenu(int containerId, Inventory inventory, BlockPos pos, IItemHandler items, ContainerData data) {
        super(ModMenus.EMBER_FORGE.get(), containerId);
        this.pos = pos;
        this.data = data;
        this.access = ContainerLevelAccess.create(inventory.player.level(), pos);

        addSlot(new SlotItemHandler(items, EmberForgeBlockEntity.SLOT_INPUT, 56, 17));
        addSlot(new SlotItemHandler(items, EmberForgeBlockEntity.SLOT_FUEL, 56, 53) {
            @Override
            public boolean mayPlace(ItemStack stack) {
                return stack.is(ModTags.FORGE_FUELS);
            }
        });
        addSlot(new SlotItemHandler(items, EmberForgeBlockEntity.SLOT_OUTPUT, 116, 35) {
            @Override
            public boolean mayPlace(ItemStack stack) {
                return false;
            }
        });
        for (int row = 0; row < 3; row++) {
            for (int col = 0; col < 9; col++) {
                addSlot(new Slot(inventory, col + row * 9 + 9, 8 + col * 18, 84 + row * 18));
            }
        }
        for (int col = 0; col < 9; col++) {
            addSlot(new Slot(inventory, col, 8 + col * 18, 142));
        }
        addDataSlots(data);
    }

    public BlockPos getPos() {
        return pos;
    }

    public int getHeat() {
        return data.get(0);
    }

    public int getMaxHeat() {
        return data.get(1);
    }

    /** How many of {@code width} pixels of the progress arrow are filled. */
    public int progressWidth(int width) {
        int time = data.get(3);
        return time == 0 ? 0 : data.get(2) * width / time;
    }

    @Override
    public ItemStack quickMoveStack(Player player, int index) {
        return ItemStack.EMPTY; // shift-click is not supported yet
    }

    @Override
    public boolean stillValid(Player player) {
        return stillValid(access, player, ModBlocks.EMBER_FORGE.get());
    }
}
