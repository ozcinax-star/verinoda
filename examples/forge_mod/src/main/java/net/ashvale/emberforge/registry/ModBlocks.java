package net.ashvale.emberforge.registry;

import java.util.function.Supplier;

import net.ashvale.emberforge.EmberForge;
import net.ashvale.emberforge.block.EmberForgeBlock;
import net.ashvale.emberforge.block.EmberForgeBlockEntity;
import net.minecraft.core.registries.Registries;
import net.minecraft.util.valueproviders.UniformInt;
import net.minecraft.world.level.block.Blocks;
import net.minecraft.world.level.block.DropExperienceBlock;
import net.minecraft.world.level.block.entity.BlockEntityType;
import net.minecraft.world.level.block.state.BlockBehaviour;
import net.minecraft.world.level.material.MapColor;
import net.neoforged.neoforge.registries.DeferredBlock;
import net.neoforged.neoforge.registries.DeferredRegister;

public final class ModBlocks {
    public static final DeferredRegister.Blocks BLOCKS = DeferredRegister.createBlocks(EmberForge.MOD_ID);
    public static final DeferredRegister<BlockEntityType<?>> BLOCK_ENTITIES =
            DeferredRegister.create(Registries.BLOCK_ENTITY_TYPE, EmberForge.MOD_ID);

    public static final DeferredBlock<EmberForgeBlock> EMBER_FORGE = BLOCKS.register("ember_forge",
            () -> new EmberForgeBlock(BlockBehaviour.Properties.of()
                    .mapColor(MapColor.STONE)
                    .strength(3.5F)
                    .requiresCorrectToolForDrops()
                    .lightLevel(state -> state.getValue(EmberForgeBlock.LIT) ? 13 : 0)));

    public static final DeferredBlock<DropExperienceBlock> CINDER_ORE = BLOCKS.register("cinder_ore",
            () -> new DropExperienceBlock(UniformInt.of(1, 4),
                    BlockBehaviour.Properties.ofFullCopy(Blocks.IRON_ORE)));

    public static final Supplier<BlockEntityType<EmberForgeBlockEntity>> EMBER_FORGE_ENTITY =
            BLOCK_ENTITIES.register("ember_forge",
                    () -> BlockEntityType.Builder.of(EmberForgeBlockEntity::new, EMBER_FORGE.get()).build(null));

    private ModBlocks() {
    }
}
