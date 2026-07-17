import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import InsurerPortalClaimsPage from "./page";

describe("InsurerPortalClaimsPage", () => {
  it("shows the minimal claims overview and sandbox boundary", () => {
    render(<InsurerPortalClaimsPage />);

    expect(screen.getByRole("heading", { name: "Claims" })).toBeInTheDocument();
    expect(
      screen.getByText("No existing claims in this sandbox."),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Sandbox only. No information is sent to an insurer."),
    ).toBeInTheDocument();
  });

  it("exposes only the approved incident-claim action", () => {
    render(<InsurerPortalClaimsPage />);

    const link = screen.getByRole("link", { name: "Start a motor claim" });
    expect(link).toHaveAttribute("href", "/portal/sandbox/claims/new");
    expect(link).toHaveAttribute(
      "data-portal-action",
      "start_incident_claim",
    );
    expect(screen.getAllByRole("link")).toHaveLength(1);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});
