# Lantern ritual, run at the altar. The effects live here; the wisps are called by the mod.
scoreboard objectives add glowmod.ritual dummy
kill @e[type=minecraft:marker,tag=glowmod.altar,distance=..1]
summon minecraft:marker ~ ~ ~ {Tags:["glowmod.altar"]}
scoreboard players set @e[type=minecraft:marker,tag=glowmod.altar,distance=..1] glowmod.ritual 200
playsound minecraft:block.beacon.activate block @a[distance=..24] ~ ~ ~ 1 0.8
function glowmod:ritual/tick
