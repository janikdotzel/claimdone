import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import InsurerPortalSandboxHomePage from "./page";

describe("InsurerPortalSandboxHomePage", () => {
  it("starts on a synthetic policy home page", () => {
    render(<InsurerPortalSandboxHomePage />);

    expect(
      screen.getByRole("heading", { name: "Welcome to Demo Mutual" }),
    ).toBeInTheDocument();
    expect(screen.getByText("DM-DEMO-2048")).toBeInTheDocument();
    expect(
      screen.getByText("Sandbox only. No information is sent to an insurer."),
    ).toBeInTheDocument();
  });

  it("exposes only the approved first navigation action", () => {
    render(<InsurerPortalSandboxHomePage />);

    const link = screen.getByRole("link", { name: "View claims" });
    expect(link).toHaveAttribute("href", "/portal/sandbox/claims");
    expect(link).toHaveAttribute("data-portal-action", "open_claims");
    expect(screen.getAllByRole("link")).toHaveLength(1);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});
