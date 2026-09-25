package com.example.glowmod.ritual;

public class Ritual {
    private final String config;

    public Ritual(String config) {
        this.config = config;
    }

    int steps() {
        return Integer.parseInt(config.trim());
    }

    public void start() {
        try {
            System.out.println("steps: " + steps());
        } catch (NumberFormatException e) {
            throw new IllegalStateException("ritual config is not a number", e);
        }
    }
}
