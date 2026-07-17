import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import InsurerPortalSandboxLayout from "./layout";

describe("InsurerPortalSandboxLayout", () => {
  it("keeps the synthetic insurer identity visible across the portal flow", () => {
    render(
      <InsurerPortalSandboxLayout>
        <main>Portal route</main>
      </InsurerPortalSandboxLayout>,
    );

    expect(screen.getByText("Demo Mutual")).toBeInTheDocument();
    expect(screen.getByText("Synthetic sandbox")).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
  });
});
