# Runs at the spot where a wisp died.
particle minecraft:soul_fire_flame ~ ~0.5 ~ 0.3 0.3 0.3 0.02 30
playsound minecraft:block.amethyst_block.chime neutral @a[distance=..16] ~ ~ ~ 1 1.4
summon minecraft:item ~ ~0.5 ~ {Item:{id:"glowmod:wisp_heart",count:1,components:{"minecraft:custom_name":'{"text":"Wisp Heart","color":"aqua","italic":false}'}}}
summon minecraft:experience_orb ~ ~0.5 ~ {Value:5}
