import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import type { Claim } from "@/lib/analysis-schema";

import PortalHandoffPage from "./page";
import {
  type PreparedPortalHandoff,
  PortalHandoffProvider,
} from "./portal-handoff-context";

const claim: Claim = {
  damage: "Front-left bumper dent and scratches",
  dateTime: "July 16, 2026 · 8:42 AM",
  location: "Alexanderplatz, Berlin",
  photoCount: 3,
  status: "ready",
  whatHappened: "Another car hit my vehicle while turning.",
};

const preparedHandoff: PreparedPortalHandoff = {
  claim,
  screenshotDataUrl: "data:image/png;base64,cHJlcGFyZWQ=",
  status: "prepared",
  submitted: false,
};

function renderPortal(initialHandoff: PreparedPortalHandoff | null = null) {
  return render(
    <PortalHandoffProvider initialHandoff={initialHandoff}>
      <PortalHandoffPage />
    </PortalHandoffProvider>,
  );
}

describe("PortalHandoffPage", () => {
  it("shows the Computer Use result with the exact sandbox boundary", () => {
    renderPortal(preparedHandoff);

    expect(screen.getByText("Insurer portal sandbox")).toBeInTheDocument();
    expect(screen.getByText("Sandbox only")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Insurer portal prepared" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Filled by Computer Use")).toBeInTheDocument();
    expect(
      screen.getByRole("img", {
        name: "Completed insurer portal sandbox form filled by Computer Use",
      }),
    ).toHaveAttribute("src", preparedHandoff.screenshotDataUrl);
    expect(screen.getByText("Sandbox only.")).toBeInTheDocument();
    expect(screen.getByText(/Nothing was submitted\./)).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Open filled Demo Mutual portal" }),
    ).toHaveAttribute("href", "/portal/sandbox/claims/new");
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.queryByText(/^Claim submitted$/i)).not.toBeInTheDocument();
  });

  it("keeps direct visits and reloads at a clear empty sandbox boundary", () => {
    renderPortal();

    expect(
      screen.getByRole("heading", { name: "No prepared portal yet" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "Start from a ready claim to let Computer Use fill the insurer portal sandbox.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: "Open filled Demo Mutual portal" }),
    ).not.toBeInTheDocument();
    expect(screen.getByText(/Nothing was submitted\./)).toBeInTheDocument();
  });

  it("offers the filled sandbox link and a back-to-claim action", () => {
    renderPortal(preparedHandoff);

    const links = screen.getAllByRole("link");
    expect(links).toHaveLength(3);
    expect(screen.getByRole("link", { name: "ClaimDone home" })).toHaveAttribute(
      "href",
      "/",
    );
    expect(screen.getByRole("link", { name: "Back to claim" })).toHaveAttribute(
      "href",
      "/",
    );
    expect(
      screen.getByRole("link", { name: "Open filled Demo Mutual portal" }),
    ).toHaveAttribute("href", "/portal/sandbox/claims/new");
    expect(screen.queryByRole("link", { name: /submit/i })).not.toBeInTheDocument();
  });
});
