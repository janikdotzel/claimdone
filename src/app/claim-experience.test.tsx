import type { ComponentProps, ReactElement } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { Claim, ClaimDetailField } from "@/lib/analysis-schema";
import { createDemoReplay, demoActivity } from "@/test/demo-fixtures";

import { ClaimExperience } from "./claim-experience";
import { runMockAnalysis } from "./mock-analysis";
import {
  PortalHandoffProvider,
  usePortalHandoff,
} from "./portal/portal-handoff-context";

const { pushMock } = vi.hoisted(() => ({ pushMock: vi.fn() }));

const completeClaim: Claim = {
  damage: "Visible front-left bumper damage",
  dateTime: "July 16, 2026 · 8:42 AM",
  location: "Alexanderplatz, Berlin",
  photoCount: 3,
  status: "ready",
  whatHappened: "Another car hit the front-left side of my car.",
};

const requiredClaimFields: ReadonlyArray<{
  field: ClaimDetailField;
  label: string;
}> = [
  { field: "damage", label: "Damage" },
  { field: "dateTime", label: "Date and time" },
  { field: "location", label: "Location" },
  { field: "whatHappened", label: "What happened" },
];

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock }),
}));

function renderWithHandoffProvider(element: ReactElement) {
  return render(<PortalHandoffProvider>{element}</PortalHandoffProvider>);
}

function renderMockExperience(
  props: Partial<ComponentProps<typeof ClaimExperience>> = {},
) {
  return renderWithHandoffProvider(
    <ClaimExperience
      analyze={runMockAnalysis}
      analysisDelayMs={0}
      {...props}
    />,
  );
}

async function renderReadyClaim(
  props: Partial<ComponentProps<typeof ClaimExperience>> = {},
) {
  const user = userEvent.setup();
  renderMockExperience(props);

  await user.click(screen.getByRole("button", { name: "Analyze accident" }));
  await screen.findByRole("heading", { name: "Your claim is ready" });

  return user;
}

function PreparedHandoffProbe() {
  const { preparedHandoff } = usePortalHandoff();

  return (
    <output data-testid="prepared-handoff">
      {preparedHandoff?.screenshotDataUrl ?? "not prepared"}
    </output>
  );
}

function mockReducedMotion(matches: boolean) {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    value: vi.fn((query: string) => ({
      addEventListener: vi.fn(),
      addListener: vi.fn(),
      dispatchEvent: vi.fn(),
      matches: matches && query === "(prefers-reduced-motion: reduce)",
      media: query,
      onchange: null,
      removeEventListener: vi.fn(),
      removeListener: vi.fn(),
    })),
    writable: true,
  });
}

describe("ClaimExperience", () => {
  beforeEach(() => {
    pushMock.mockReset();
    mockReducedMotion(false);
  });

  it("keeps the four approved M1 removals out of the interface", () => {
    renderMockExperience();

    expect(screen.queryByText("Accident claim demo")).not.toBeInTheDocument();
    expect(screen.queryByText(/Demo only\./)).not.toBeInTheDocument();
    expect(screen.queryByText(/^Demo$/)).not.toBeInTheDocument();
  });

  it("completes the mock happy path", async () => {
    const user = userEvent.setup();
    renderMockExperience();

    await user.click(screen.getByRole("button", { name: "Analyze accident" }));

    expect(
      await screen.findByRole("heading", { name: "Your claim is ready" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Alexanderplatz, Berlin")).toBeInTheDocument();
    expect(screen.getByText("3 photos")).toBeInTheDocument();
    expect(screen.getByText("Nothing has been submitted.")).toBeInTheDocument();
  });

  it("keeps presenter details isolated to the demo variant", () => {
    renderMockExperience();

    expect(screen.queryByText("Presenter view")).not.toBeInTheDocument();
    expect(screen.queryByText("Agent activity")).not.toBeInTheDocument();
    expect(
      screen.queryByText(/not private model reasoning/i),
    ).not.toBeInTheDocument();
  });

  it("shows validated observable checks in presenter view", async () => {
    const analyzeDemo = vi.fn().mockResolvedValue({
      activity: demoActivity,
      result: { claim: completeClaim, status: "ready" as const },
    });
    const user = userEvent.setup();

    renderWithHandoffProvider(
      <ClaimExperience
        analyzeDemo={analyzeDemo}
        analysisDelayMs={0}
        variant="presenter"
      />,
    );

    expect(screen.getAllByText("Presenter view")).not.toHaveLength(0);
    expect(screen.getByText("Agent activity")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Observable checks and decisions — not private model reasoning.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Standard view" })).toHaveAttribute(
      "href",
      "/",
    );
    expect(
      screen.getByText("Activity will appear when you start the analysis."),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("list", { name: "Demo progress" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Evidence", { selector: "span" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Agent review", { selector: "span" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Portal handoff", { selector: "span" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Evidence", { selector: "span" }).closest("li"),
    ).toHaveAttribute("aria-current", "step");
    expect(
      screen.getByText("Agent review", { selector: "span" }).closest("li"),
    ).not.toHaveAttribute("aria-current");
    expect(screen.queryByText("Evidence staged")).not.toBeInTheDocument();
    expect(
      screen.queryByText("Image and statement review"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText("Completeness and decision"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "Claim to insurer portal" }),
    ).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Analyze accident" }));

    expect(await screen.findByText("Photo 1 reviewed")).toBeInTheDocument();
    expect(
      screen.getAllByText("Decision: Prepare the claim"),
    ).not.toHaveLength(0);
    expect(
      screen.getByText("Agent review", { selector: "span" }).closest("li"),
    ).toHaveAttribute("aria-current", "step");
    expect(analyzeDemo).toHaveBeenCalledOnce();
  });

  it("adds a deterministic presenter decision after missing details are completed", async () => {
    const missingDateTimeActivity = {
      events: demoActivity.events.map((event) => {
        if (event.sequence === 4) {
          return {
            ...event,
            detail: "Date and time was not supplied in the evidence.",
            status: "attention" as const,
            title: "Date and time: needs attention",
          };
        }

        if (event.sequence === 5) {
          return {
            ...event,
            detail: "The claim needs a date and time before portal handoff.",
            status: "attention" as const,
            title: "Decision: Prepare for customer review",
          };
        }

        return event;
      }),
    };
    const analyzeDemo = vi.fn().mockResolvedValue({
      activity: missingDateTimeActivity,
      result: {
        claim: { ...completeClaim, dateTime: "Not provided" },
        status: "ready" as const,
      },
    });
    const user = userEvent.setup();

    renderWithHandoffProvider(
      <ClaimExperience
        analyzeDemo={analyzeDemo}
        analysisDelayMs={0}
        variant="presenter"
      />,
    );

    await user.click(screen.getByRole("button", { name: "Analyze accident" }));
    await screen.findByRole("heading", { name: "Your claim needs details" });
    expect(screen.getByText("Date and time: needs attention")).toBeInTheDocument();
    expect(screen.queryByText("Customer update checked")).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Add missing details" }));
    await user.type(
      screen.getByRole("textbox", { name: "Date and time" }),
      "   ",
    );
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Complete all four claim details before saving.",
    );
    expect(screen.queryByText("Customer update checked")).not.toBeInTheDocument();
    expect(screen.queryByText("Decision: Claim ready")).not.toBeInTheDocument();

    await user.clear(screen.getByRole("textbox", { name: "Date and time" }));
    await user.type(
      screen.getByRole("textbox", { name: "Date and time" }),
      "July 17, 2026 · 2:30 PM",
    );
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(screen.getByText("Customer update checked")).toBeInTheDocument();
    expect(
      screen.getByText(
        "Date and time was supplied and the previously missing claim detail is now confirmed.",
      ),
    ).toBeInTheDocument();
    expect(screen.getAllByText("Decision: Claim ready")).not.toHaveLength(0);
    expect(
      screen.getByText(
        "All four required claim details are complete. The claim can continue to the insurer portal sandbox.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Your claim is ready" }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("heading", { name: "Claim to insurer portal" }),
    ).not.toBeInTheDocument();
  });

  it("replays the captured Computer Use run without leaving presenter view", async () => {
    const screenshotDataUrl = "data:image/png;base64,cHJlcGFyZWQ=";
    const replay = createDemoReplay(screenshotDataUrl);
    const analyzeDemo = vi.fn().mockResolvedValue({
      activity: demoActivity,
      result: { claim: completeClaim, status: "ready" as const },
    });
    const prepareDemoPortal = vi.fn().mockResolvedValue({
      replay,
      screenshotDataUrl,
      status: "prepared" as const,
      submitted: false as const,
    });
    const user = userEvent.setup();

    renderWithHandoffProvider(
      <ClaimExperience
        analyzeDemo={analyzeDemo}
        analysisDelayMs={0}
        prepareDemoPortal={prepareDemoPortal}
        variant="presenter"
      />,
    );

    await user.click(screen.getByRole("button", { name: "Analyze accident" }));
    await screen.findByRole("heading", { name: "Your claim is ready" });
    expect(
      screen.queryByRole("heading", { name: "Claim to insurer portal" }),
    ).not.toBeInTheDocument();
    await user.click(
      screen.getByRole("button", {
        name: "Run Computer Use in insurer sandbox",
      }),
    );

    expect(await screen.findByText("Captured from this run")).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { name: "Claim to insurer portal" }),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Portal handoff", { selector: "span" }).closest("li"),
    ).toHaveAttribute("aria-current", "step");
    expect(screen.getAllByText("Opened Demo Mutual home")).not.toHaveLength(0);
    expect(screen.getAllByText("Clicked “View claims”")).not.toHaveLength(0);
    expect(
      screen.getAllByText("Clicked “Start a motor claim”"),
    ).not.toHaveLength(0);
    expect(screen.getByText("Stopped before submission")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Pause Computer Use replay" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("link", { name: "Open filled Demo Mutual portal" }),
    ).toHaveAttribute("href", "/portal/sandbox/claims/new");
    expect(pushMock).not.toHaveBeenCalled();
  });

  it("keeps the Computer Use replay paused when reduced motion is requested", async () => {
    mockReducedMotion(true);
    const screenshotDataUrl = "data:image/png;base64,cHJlcGFyZWQ=";
    const replay = createDemoReplay(screenshotDataUrl);
    const user = userEvent.setup();

    renderWithHandoffProvider(
      <ClaimExperience
        analyzeDemo={vi.fn().mockResolvedValue({
          activity: demoActivity,
          result: { claim: completeClaim, status: "ready" as const },
        })}
        analysisDelayMs={0}
        prepareDemoPortal={vi.fn().mockResolvedValue({
          replay,
          screenshotDataUrl,
          status: "prepared" as const,
          submitted: false as const,
        })}
        variant="presenter"
      />,
    );

    await user.click(screen.getByRole("button", { name: "Analyze accident" }));
    await screen.findByRole("heading", { name: "Your claim is ready" });
    await user.click(
      screen.getByRole("button", {
        name: "Run Computer Use in insurer sandbox",
      }),
    );

    expect(
      await screen.findByRole("button", { name: "Play Computer Use replay" }),
    ).toBeInTheDocument();
    expect(screen.getAllByText("Opened Demo Mutual home")).not.toHaveLength(0);
  });

  it.each(requiredClaimFields)(
    "marks missing $label details as required and removes the portal action",
    async ({ field }) => {
      const preparePortal = vi.fn();
      const analyze = vi.fn().mockResolvedValue({
        status: "ready" as const,
        claim: { ...completeClaim, [field]: "  not provided  " },
      });
      const user = userEvent.setup();

      renderWithHandoffProvider(
        <ClaimExperience
          analyze={analyze}
          analysisDelayMs={0}
          preparePortal={preparePortal}
        />,
      );

      await user.click(screen.getByRole("button", { name: "Analyze accident" }));

      expect(
        await screen.findByRole("heading", { name: "Your claim needs details" }),
      ).toBeInTheDocument();
      expect(screen.getByText("Required").parentElement).toHaveTextContent(
        "Not provided",
      );
      expect(
        screen.getByText(
          "Complete the fields marked Required before continuing.",
        ),
      ).toBeInTheDocument();
      expect(
        screen.getByLabelText("System status: Needs details"),
      ).toHaveTextContent("Needs details");
      expect(screen.queryByText(/^Status$/)).not.toBeInTheDocument();
      expect(
        screen.getByRole("button", { name: "Add missing details" }),
      ).toBeInTheDocument();
      expect(
        screen.queryByRole("button", { name: "Fill insurer portal sandbox" }),
      ).not.toBeInTheDocument();
      expect(preparePortal).not.toHaveBeenCalled();
    },
  );

  it("requires every missing detail before enabling the portal handoff", async () => {
    const preparePortal = vi.fn().mockResolvedValue({
      screenshotDataUrl: "data:image/png;base64,cHJlcGFyZWQ=",
      status: "prepared" as const,
      submitted: false as const,
    });
    const analyze = vi.fn().mockResolvedValue({
      status: "ready" as const,
      claim: {
        ...completeClaim,
        dateTime: "Not provided",
        location: "Not provided",
      },
    });
    const user = userEvent.setup();

    renderWithHandoffProvider(
      <ClaimExperience
        analyze={analyze}
        analysisDelayMs={0}
        preparePortal={preparePortal}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Analyze accident" }));
    await screen.findByRole("heading", { name: "Your claim needs details" });
    expect(screen.getAllByText("Required")).toHaveLength(2);

    await user.click(screen.getByRole("button", { name: "Add missing details" }));
    const dateTime = screen.getByRole("textbox", { name: "Date and time" });
    const location = screen.getByRole("textbox", { name: "Location" });
    expect(dateTime).toHaveValue("");
    expect(location).toHaveValue("");
    expect(dateTime).toHaveAttribute("aria-invalid", "true");
    expect(location).toHaveAttribute("aria-invalid", "true");

    await user.type(dateTime, "July 17, 2026 · 9:30 AM");
    await user.type(location, "not provided");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Complete all four claim details before saving.",
    );
    expect(location).toHaveAttribute("aria-invalid", "true");
    expect(preparePortal).not.toHaveBeenCalled();

    await user.clear(location);
    await user.type(location, "Potsdamer Platz, Berlin");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(
      screen.getByRole("heading", { name: "Your claim is ready" }),
    ).toBeInTheDocument();
    expect(
      screen.getByLabelText("System status: Ready"),
    ).toHaveTextContent("Ready");
    expect(screen.queryByText("Required")).not.toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "Fill insurer portal sandbox" }),
    );
    await waitFor(() => {
      expect(preparePortal).toHaveBeenCalledWith(
        expect.objectContaining({
          dateTime: "July 17, 2026 · 9:30 AM",
          location: "Potsdamer Platz, Berlin",
        }),
      );
    });
  });

  it("shows edit as the secondary action and Computer Use as the primary action", async () => {
    await renderReadyClaim();

    expect(
      screen.getByRole("button", { name: "Edit details" }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Fill insurer portal sandbox" }),
    ).toBeInTheDocument();
  });

  it("keeps the ready claim visible while the insurer portal is being prepared", async () => {
    const preparePortal = vi.fn(() => new Promise<never>(() => undefined));
    const user = await renderReadyClaim({ preparePortal });

    await user.click(
      screen.getByRole("button", { name: "Fill insurer portal sandbox" }),
    );

    expect(screen.getByRole("heading", { name: "Your claim is ready" })).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent(
      "Opening the insurer portal and preparing the form…",
    );
    expect(
      screen.getByRole("button", { name: "Preparing insurer portal…" }),
    ).toBeDisabled();
    expect(screen.getByRole("button", { name: "Edit details" })).toBeDisabled();
    expect(preparePortal).toHaveBeenCalledTimes(1);
  });

  it("passes saved edits to Computer Use, stores the result in memory, and navigates", async () => {
    const preparePortal = vi.fn().mockResolvedValue({
      screenshotDataUrl: "data:image/png;base64,cHJlcGFyZWQ=",
      status: "prepared" as const,
      submitted: false as const,
    });
    const user = userEvent.setup();

    render(
      <PortalHandoffProvider>
        <ClaimExperience
          analyze={runMockAnalysis}
          analysisDelayMs={0}
          preparePortal={preparePortal}
        />
        <PreparedHandoffProbe />
      </PortalHandoffProvider>,
    );

    await user.click(screen.getByRole("button", { name: "Analyze accident" }));
    await screen.findByRole("heading", { name: "Your claim is ready" });
    await user.click(screen.getByRole("button", { name: "Edit details" }));
    const location = screen.getByRole("textbox", { name: "Location" });
    await user.clear(location);
    await user.type(location, "Potsdamer Platz, Berlin");
    await user.click(screen.getByRole("button", { name: "Save changes" }));
    await user.click(
      screen.getByRole("button", { name: "Fill insurer portal sandbox" }),
    );

    await waitFor(() => {
      expect(preparePortal).toHaveBeenCalledWith(
        expect.objectContaining({
          location: "Potsdamer Platz, Berlin",
          photoCount: 3,
          status: "ready",
        }),
      );
      expect(screen.getByTestId("prepared-handoff")).toHaveTextContent(
        "data:image/png;base64,cHJlcGFyZWQ=",
      );
      expect(pushMock).toHaveBeenCalledWith("/portal");
    });
  });

  it("keeps the claim and offers a retry when the portal cannot be prepared", async () => {
    const preparePortal = vi
      .fn()
      .mockRejectedValueOnce(new Error("Computer Use failed"))
      .mockResolvedValueOnce({
        screenshotDataUrl: "data:image/png;base64,cHJlcGFyZWQ=",
        status: "prepared" as const,
        submitted: false as const,
      });
    const user = await renderReadyClaim({ preparePortal });

    await user.click(
      screen.getByRole("button", { name: "Fill insurer portal sandbox" }),
    );

    expect(
      await screen.findByText("We couldn’t prepare the insurer portal"),
    ).toBeInTheDocument();
    expect(screen.getByText("Your claim is still here. Try the sandbox again.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Your claim is ready" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Try portal again" }));

    await waitFor(() => {
      expect(preparePortal).toHaveBeenCalledTimes(2);
      expect(pushMock).toHaveBeenCalledWith("/portal");
    });
  });

  it("edits and saves the four approved claim details inline", async () => {
    const user = await renderReadyClaim();

    await user.click(screen.getByRole("button", { name: "Edit details" }));

    expect(
      screen.getByRole("heading", { name: "Edit claim details" }),
    ).toBeInTheDocument();

    const updates = {
      "Damage": "Rear bumper dent",
      "Date and time": "July 17, 2026 · 9:30 AM",
      "Location": "Potsdamer Platz, Berlin",
      "What happened": "The other car reversed into my parked vehicle.",
    } as const;

    for (const [name, value] of Object.entries(updates)) {
      const field = screen.getByRole("textbox", { name });
      await user.clear(field);
      await user.type(field, value);
    }

    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(
      screen.getByRole("heading", { name: "Your claim is ready" }),
    ).toBeInTheDocument();
    Object.values(updates).forEach((value) => {
      expect(screen.getByText(value)).toBeInTheDocument();
    });
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("keeps whitespace-only claim edits in edit mode with a clear error", async () => {
    const preparePortal = vi.fn();
    const user = await renderReadyClaim({ preparePortal });

    await user.click(screen.getByRole("button", { name: "Edit details" }));
    const damage = screen.getByRole("textbox", { name: "Damage" });
    await user.clear(damage);
    await user.type(damage, "   ");
    await user.click(screen.getByRole("button", { name: "Save changes" }));

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Complete all four claim details before saving.",
    );
    expect(damage).toHaveAttribute("aria-invalid", "true");
    expect(damage).toHaveAttribute("aria-describedby", "claim-edit-error");
    expect(
      screen.getByRole("heading", { name: "Edit claim details" }),
    ).toBeInTheDocument();
    expect(preparePortal).not.toHaveBeenCalled();
  });

  it("discards draft edits when claim editing is cancelled", async () => {
    const user = await renderReadyClaim();

    await user.click(screen.getByRole("button", { name: "Edit details" }));
    const damage = screen.getByRole("textbox", { name: "Damage" });
    await user.clear(damage);
    await user.type(damage, "Unsaved replacement damage");
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(
      screen.getByRole("heading", { name: "Your claim is ready" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Visible front-left bumper damage")).toBeInTheDocument();
    expect(screen.queryByText("Unsaved replacement damage")).not.toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("keeps attached photos and the system-managed status outside the editable fields", async () => {
    const user = await renderReadyClaim();

    await user.click(screen.getByRole("button", { name: "Edit details" }));

    expect(
      screen.getAllByRole("textbox").map((field) => field.getAttribute("aria-label")),
    ).toEqual(["Damage", "Date and time", "Location", "What happened"]);
    expect(
      screen.queryByRole("textbox", { name: "Attached photos" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("textbox", { name: "Status" }),
    ).not.toBeInTheDocument();
    expect(screen.queryByText(/^Status$/)).not.toBeInTheDocument();
    expect(screen.getByText("3 photos")).toBeInTheDocument();
    expect(
      screen.getByLabelText("System status: Ready"),
    ).toHaveTextContent("Ready");
    expect(screen.getAllByText("Ready")).toHaveLength(1);
    expect(document.querySelector('input[type="file"]')).not.toBeInTheDocument();
  });

  it("shows the analyzing state while the mock result is pending", async () => {
    const user = userEvent.setup();
    const analyze = vi.fn(() => new Promise<never>(() => undefined));
    renderWithHandoffProvider(
      <ClaimExperience analyze={analyze} analysisDelayMs={0} />,
    );

    await user.click(screen.getByRole("button", { name: "Analyze accident" }));

    expect(
      await screen.findByRole("heading", {
        name: "Analyzing your photos and preparing your claim…",
      }),
    ).toBeInTheDocument();
  });

  it("asks exactly one missing-information question and then finishes", async () => {
    const user = userEvent.setup();
    renderMockExperience();

    const description = screen.getByRole("textbox", { name: "Short description" });
    await user.clear(description);
    await user.type(description, "Another car hit my front-left bumper while I was stopped.");
    await user.click(screen.getByRole("button", { name: "Analyze accident" }));

    expect(
      await screen.findByRole("heading", { name: "We need one more detail" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Where did the accident happen?")).toBeInTheDocument();
    expect(screen.getAllByRole("textbox")).toHaveLength(1);

    await user.type(
      screen.getByRole("textbox", { name: "Your answer" }),
      "Alexanderplatz, Berlin",
    );
    await user.click(screen.getByRole("button", { name: "Continue analysis" }));

    expect(
      await screen.findByRole("heading", { name: "Your claim is ready" }),
    ).toBeInTheDocument();
    expect(screen.getByText("Alexanderplatz, Berlin")).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("replaces the sample band with one to three local photo previews", async () => {
    const user = userEvent.setup();
    renderMockExperience();

    const photos = [
      new File(["overview"], "overview.png", { type: "image/png" }),
      new File(["damage"], "damage.jpg", { type: "image/jpeg" }),
    ];

    await user.upload(
      screen.getByLabelText("Use your own accident photos"),
      photos,
    );

    expect(screen.getByAltText("Preview of overview.png")).toBeInTheDocument();
    expect(screen.getByAltText("Preview of damage.jpg")).toBeInTheDocument();
    expect(screen.queryByAltText(/Two vehicles after/)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Remove Overview photo" }));
    await user.click(screen.getByRole("button", { name: "Analyze accident" }));

    expect(await screen.findByText("1 photo")).toBeInTheDocument();
  });

  it("supports the native voice-memo choice and routes it to one question", async () => {
    const user = userEvent.setup();
    renderMockExperience();

    await user.click(screen.getByRole("button", { name: "Voice memo" }));
    const memo = new File(["audio"], "accident.webm", { type: "audio/webm" });
    await user.upload(screen.getByLabelText("Add voice memo"), memo);

    expect(screen.getByText("accident.webm")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Analyze accident" }));

    expect(
      await screen.findByRole("heading", { name: "We need one more detail" }),
    ).toBeInTheDocument();
    expect(screen.getAllByRole("textbox")).toHaveLength(1);
  });

  it("accepts a 60-second voice memo and rejects a longer one", async () => {
    const user = userEvent.setup();
    renderMockExperience();

    await user.click(screen.getByRole("button", { name: "Voice memo" }));
    await user.upload(
      screen.getByLabelText("Add voice memo"),
      new File(["audio"], "accident.webm", { type: "audio/webm" }),
    );

    const audio = document.querySelector("audio");
    expect(audio).not.toBeNull();
    Object.defineProperty(audio, "duration", { configurable: true, value: 60 });
    fireEvent.loadedMetadata(audio as HTMLAudioElement);
    expect(screen.getByText("accident.webm")).toBeInTheDocument();

    Object.defineProperty(audio, "duration", { configurable: true, value: 60.1 });
    fireEvent.loadedMetadata(audio as HTMLAudioElement);

    expect(
      screen.getByRole("alert"),
    ).toHaveTextContent("Keep the voice memo under 60 seconds and 10 MB.");
    expect(screen.queryByText("accident.webm")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Add voice memo")).toHaveAttribute(
      "aria-invalid",
      "true",
    );
  });

  it("does not accept a renamed non-audio file as a voice memo", async () => {
    const user = userEvent.setup({ applyAccept: false });
    renderMockExperience();

    await user.click(screen.getByRole("button", { name: "Voice memo" }));
    await user.upload(
      screen.getByLabelText("Add voice memo"),
      new File(["not audio"], "memo.m4a", { type: "application/pdf" }),
    );

    expect(
      screen.getByText("Use an M4A, MP3, WAV or WebM recording."),
    ).toBeInTheDocument();
    expect(screen.queryByText("memo.m4a")).not.toBeInTheDocument();
  });

  it("shows a clear analysis error and preserves the evidence", async () => {
    const user = userEvent.setup();
    const analyze = vi.fn().mockRejectedValue(new Error("Mock provider failure"));
    renderWithHandoffProvider(
      <ClaimExperience analyze={analyze} analysisDelayMs={0} />,
    );

    const description = screen.getByRole("textbox", { name: "Short description" });
    await user.clear(description);
    await user.type(description, "My saved description in Berlin.");
    await user.click(screen.getByRole("button", { name: "Analyze accident" }));

    const errorHeading = await screen.findByText("We couldn’t analyze these photos");
    expect(errorHeading).toBeInTheDocument();
    expect(errorHeading.closest('[role="alert"]')).toHaveFocus();
    expect(screen.getByRole("textbox", { name: "Short description" })).toHaveValue(
      "My saved description in Berlin.",
    );
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("rejects unsupported photos without replacing the current evidence", async () => {
    const user = userEvent.setup({ applyAccept: false });
    renderMockExperience();

    await user.upload(
      screen.getByLabelText("Use your own accident photos"),
      new File(["not-an-image"], "claim.pdf", { type: "application/pdf" }),
    );

    expect(await screen.findByText("Use JPG or PNG photos.")).toBeInTheDocument();
    expect(screen.getByAltText(/Two vehicles after/)).toBeInTheDocument();
  });

  it("enforces the photo count and size limits", async () => {
    const user = userEvent.setup();
    renderMockExperience();
    const photoInput = screen.getByLabelText("Use your own accident photos");

    await user.upload(
      photoInput,
      [1, 2, 3, 4].map(
        (index) => new File([`photo-${index}`], `photo-${index}.jpg`, { type: "image/jpeg" }),
      ),
    );
    expect(screen.getByText("You can add up to three photos.")).toBeInTheDocument();

    const oversizedPhoto = new File(
      [new Uint8Array(8 * 1024 * 1024 + 1)],
      "oversized.jpg",
      { type: "image/jpeg" },
    );
    await user.upload(photoInput, oversizedPhoto);
    expect(screen.getByText("Each photo must be 8 MB or smaller.")).toBeInTheDocument();
  });

  it("associates an empty-photo error with the evidence control", async () => {
    const user = userEvent.setup();
    renderMockExperience();

    for (let index = 0; index < 3; index += 1) {
      const [removeButton] = screen.getAllByRole("button", {
        name: /^Remove .* photo$/,
      });
      expect(removeButton).toBeDefined();
      await user.click(removeButton as HTMLElement);
    }
    await user.click(screen.getByRole("button", { name: "Analyze accident" }));

    expect(screen.getByRole("alert")).toHaveTextContent(
      "Add at least one accident photo.",
    );
    expect(screen.getByLabelText("Add accident photos")).toHaveAttribute(
      "aria-invalid",
      "true",
    );
    expect(screen.getByLabelText("Add accident photos")).toHaveAttribute(
      "aria-describedby",
      "photo-requirements evidence-validation-error",
    );
  });
});
