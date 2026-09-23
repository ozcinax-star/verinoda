# Puts out and empties every Ember Forge within 16 blocks. Forges face north afterwards.
fill ~-16 ~-8 ~-16 ~16 ~8 ~16 emberforge:ember_forge[lit=false]{Heat:0,Progress:0} replace emberforge:ember_forge
execute if block ~ ~-1 ~ #emberforge:forge_heat_sources run tellraw @s {"translate":"message.emberforge.reset.heat_source","color":"yellow"}
tellraw @s {"translate":"message.emberforge.reset.done","color":"gold"}
