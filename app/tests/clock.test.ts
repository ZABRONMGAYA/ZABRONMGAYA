import { describe, expect, it } from "vitest";

import { MasterClock } from "../src/features/multicam/clock";

describe("master clock", () => {
  it("advances only while playing, from where it was paused or sought", () => {
    let now = 0;
    const clock = new MasterClock(() => now);
    clock.seek(10);
    now = 5000;
    expect(clock.now()).toBe(10);
    clock.play();
    now = 7500;
    expect(clock.now()).toBeCloseTo(12.5);
    clock.pause();
    now = 20000;
    expect(clock.now()).toBeCloseTo(12.5);
    clock.seek(100);
    clock.play();
    now = 21000;
    expect(clock.now()).toBeCloseTo(101);
  });

  it("steps by frames, pausing first, and stops at the end of the timeline", () => {
    let now = 0;
    const clock = new MasterClock(() => now);
    const events: string[] = [];
    clock.subscribe((e) => events.push(e));
    clock.end = 3;
    clock.seek(1);
    clock.play();
    clock.step(1, 25);
    expect(clock.playing).toBe(false);
    expect(clock.now()).toBeCloseTo(1.04);
    clock.step(-10, 25);
    expect(clock.now()).toBeCloseTo(0.64);
    clock.play();
    now = 10_000;
    expect(clock.now()).toBe(3);
    expect(clock.playing).toBe(false);
    expect(events).toContain("pause");
  });

  it("plays at a rate (J/L shuttle)", () => {
    let now = 0;
    const clock = new MasterClock(() => now);
    clock.seek(0);
    clock.play();
    clock.setRate(2);
    now = 1000;
    expect(clock.now()).toBeCloseTo(2);
  });
});
