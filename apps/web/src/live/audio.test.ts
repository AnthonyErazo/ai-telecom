import { describe, expect, it } from "vitest";
import { microphoneSupportError } from "./audio";

describe("compatibilidad del micrófono", () => {
  it("explica cuando el navegador no expone getUserMedia", () => {
    expect(microphoneSupportError()).toMatch(/micrófono|HTTPS|navegador/i);
  });
});
