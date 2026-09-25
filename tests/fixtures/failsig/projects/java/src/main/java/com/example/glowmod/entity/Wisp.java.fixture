package com.example.glowmod.entity;

import java.util.List;

public class Wisp {
    private Target target;
    private final List<String> trail;

    public Wisp(List<String> trail) {
        this.trail = trail;
    }

    public void tick() {
        double d = target.distance();
        trail.add("moved " + d);
    }

    public int glowLevel(int light) {
        return trail.stream().mapToInt(s -> s.length() / light).sum();
    }

    public static class Target {
        double distance() {
            return 1.0;
        }
    }
}
