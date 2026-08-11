import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Blocks, money } from "./Blocks";

describe("Blocks", () => {
  it("renders typed API content", () => {
    render(<Blocks blocks={[{ tipo: "texto", texto: "Explicación verificada" }, { tipo: "kv", items: [{ clave: "Cambio", valor: "S/ 20.82" }] }]} />);
    expect(screen.getByText("Explicación verificada")).toBeInTheDocument();
    expect(screen.getByText("S/ 20.82")).toBeInTheDocument();
  });

  it("formats cents without doing financial arithmetic", () => {
    expect(money(2082)).toMatch(/20[.,]82/);
  });
});
