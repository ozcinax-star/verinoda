# One step of the ritual effects; keeps rescheduling itself while an altar has time left.
execute as @e[type=minecraft:marker,tag=glowmod.altar] at @s run particle minecraft:end_rod ~ ~1 ~ 1.5 0.5 1.5 0.01 6
scoreboard players remove @e[type=minecraft:marker,tag=glowmod.altar] glowmod.ritual 1
kill @e[type=minecraft:marker,tag=glowmod.altar,scores={glowmod.ritual=..0}]
execute if entity @e[type=minecraft:marker,tag=glowmod.altar] run schedule function glowmod:ritual/tick 1t
