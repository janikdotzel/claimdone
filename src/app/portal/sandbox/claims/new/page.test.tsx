import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import type { Claim } from "@/lib/analysis-schema";

import {
  type PreparedPortalHandoff,
  PortalHandoffProvider,
} from "../../../portal-handoff-context";
import NewIncidentClaimPage from "./page";

const preparedClaim: Claim = {
  damage: "Front-left bumper dent",
  dateTime: "July 16, 2026 at 8:42 AM",
  location: "Alexanderplatz, Berlin",
  photoCount: 3,
  status: "ready",
  whatHappened: "Another car hit my vehicle while turning.",
};

const preparedHandoff: PreparedPortalHandoff = {
  claim: preparedClaim,
  screenshotDataUrl: "data:image/png;base64,cHJlcGFyZWQ=",
  status: "prepared",
  submitted: false,
};

function renderForm(initialHandoff: PreparedPortalHandoff | null = null) {
  return render(
    <PortalHandoffProvider initialHandoff={initialHandoff}>
      <NewIncidentClaimPage />
    </PortalHandoffProvider>,
  );
}

describe("NewIncidentClaimPage", () => {
  it("exposes exactly the five controlled fields Computer Use may fill", async () => {
    const user = userEvent.setup();
    renderForm();

    const fields = [
      ["Damage", "Front-left bumper dent"],
      ["Date and time", "July 16, 2026 at 8:42 AM"],
      ["Location", "Alexanderplatz, Berlin"],
      ["Attached photos", "3 photos"],
      ["What happened", "Another car hit my vehicle while turning."],
    ] as const;

    for (const [name, value] of fields) {
      const field = screen.getByRole("textbox", { name });
      await user.type(field, value);
      expect(field).toHaveValue(value);
    }

    expect(document.querySelectorAll("[data-portal-field]")).toHaveLength(5);
  });

  it("has no navigation, button, or submission control", () => {
    renderForm();

    expect(
      screen.getByRole("heading", { name: "Tell us about the incident" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/This form has no submit action/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("link")).not.toBeInTheDocument();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: /submit/i })).not.toBeInTheDocument();
  });

  it("shows the reviewed claim from the prepared handoff", () => {
    renderForm(preparedHandoff);

    expect(screen.getByRole("textbox", { name: "Damage" })).toHaveValue(
      preparedClaim.damage,
    );
    expect(screen.getByRole("textbox", { name: "Date and time" })).toHaveValue(
      preparedClaim.dateTime,
    );
    expect(screen.getByRole("textbox", { name: "Location" })).toHaveValue(
      preparedClaim.location,
    );
    expect(screen.getByRole("textbox", { name: "Attached photos" })).toHaveValue(
      "3 accident photos attached",
    );
    expect(screen.getByRole("textbox", { name: "What happened" })).toHaveValue(
      preparedClaim.whatHappened,
    );
  });
});
