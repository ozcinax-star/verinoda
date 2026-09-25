package com.example.glowmod;

import com.example.glowmod.entity.Wisp;
import com.example.glowmod.ritual.Ritual;
import java.util.ArrayList;
import java.util.List;

public class Main {
    public static void main(String[] args) {
        switch (args[0]) {
            case "npe" -> new Wisp(new ArrayList<>()).tick();
            case "cause" -> new Ritual("three").start();
            case "lambda" -> {
                List<String> t = new ArrayList<>();
                t.add("a");
                new Wisp(t).glowLevel(0);
            }
            default -> throw new IllegalArgumentException(args[0]);
        }
    }
}
