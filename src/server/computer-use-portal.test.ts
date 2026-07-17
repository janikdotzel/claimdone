import type {
  ComputerAction,
  Response,
} from "openai/resources/responses/responses";
import { describe, expect, it, vi } from "vitest";

import type { Claim } from "@/lib/analysis-schema";

import {
  COMPUTER_USE_MODEL,
  ComputerUsePortalAutomator,
  PORTAL_SANDBOX_CLAIMS_URL,
  PORTAL_SANDBOX_FORM_URL,
  PORTAL_SANDBOX_HOME_URL,
  PortalAutomationSafetyError,
  executeSafeComputerAction,
  getPortalFieldValues,
  type CreateComputerResponse,
  type PortalBrowserSession,
  type PortalField,
} from "./computer-use-portal";

const claim: Claim = {
  damage: "Front-left bumper dent and scratches",
  dateTime: "Not provided",
  location: "Alexanderplatz, Berlin",
  photoCount: 2,
  status: "ready",
  whatHappened: "Another car hit my front-left bumper.",
};

const fieldByRow: readonly PortalField[] = [
  "damage",
  "dateTime",
  "location",
  "whatHappened",
  "attachedPhotos",
];

class FakeSession implements PortalBrowserSession {
  closed = false;
  focused: PortalField | null = null;
  navigationUrlOverride: string | null = null;
  prohibitedPoint = false;
  screenshotCount = 0;
  url = PORTAL_SANDBOX_HOME_URL;
  values: Partial<Record<PortalField, string>> = {};

  async click(_x: number, y: number): Promise<void> {
    this.focused = fieldByRow[Math.floor(y / 100)] ?? null;
  }

  async clickAndWaitForUrl(
    _x: number,
    _y: number,
    expectedUrl: string,
  ): Promise<void> {
    this.focused = null;
    this.url = this.navigationUrlOverride ?? expectedUrl;
  }

  async close(): Promise<void> {
    this.closed = true;
  }

  currentUrl(): string {
    return this.url;
  }

  async focusedField(): Promise<PortalField | null> {
    return this.focused;
  }

  async inspectPoint(_x: number, y: number) {
    if (this.prohibitedPoint) return { kind: "prohibited" } as const;

    if (this.url === PORTAL_SANDBOX_HOME_URL) {
      return {
        action: "open_claims",
        kind: "approved_navigation",
      } as const;
    }

    if (this.url === PORTAL_SANDBOX_CLAIMS_URL) {
      return {
        action: "start_incident_claim",
        kind: "approved_navigation",
      } as const;
    }

    const field = fieldByRow[Math.floor(y / 100)];
    return field
      ? ({ field, kind: "approved_field" } as const)
      : ({ kind: "other" } as const);
  }

  async keypress(): Promise<void> {}

  async move(): Promise<void> {}

  async readValues(): Promise<Partial<Record<PortalField, string>>> {
    return { ...this.values };
  }

  async screenshot(): Promise<string> {
    this.screenshotCount += 1;
    return "data:image/png;base64,cG5n";
  }

  async scroll(): Promise<void> {}

  async typeText(text: string): Promise<void> {
    if (this.focused) this.values[this.focused] = text;
  }

  async wait(): Promise<void> {}
}

function computerResponse(
  id: string,
  options: {
    actions?: ComputerAction[];
    pendingSafetyChecks?: Array<{ id: string }>;
  },
): Response {
  return {
    id,
    output: [
      {
        actions: options.actions ?? [{ type: "screenshot" }],
        call_id: `call-${id}`,
        id: `item-${id}`,
        ...(options.pendingSafetyChecks
          ? { pending_safety_checks: options.pendingSafetyChecks }
          : {}),
        status: "completed",
        type: "computer_call",
      },
    ],
  } as unknown as Response;
}

function fillActions(): ComputerAction[] {
  const values = getPortalFieldValues(claim);
  return fieldByRow.flatMap((field, index) => [
    {
      button: "left" as const,
      type: "click" as const,
      x: 20,
      y: index * 100 + 10,
    },
    { text: values[field], type: "type" as const },
  ]);
}

function completePortalActions(): ComputerAction[] {
  return [
    { button: "left", type: "click", x: 20, y: 10 },
    { button: "left", type: "click", x: 20, y: 10 },
    ...fillActions(),
  ];
}

describe("executeSafeComputerAction", () => {
  it("allows only the ordered Claims and incident-claim navigation", async () => {
    const session = new FakeSession();
    const values = getPortalFieldValues(claim);

    await expect(
      executeSafeComputerAction(
        session,
        { button: "left", type: "click", x: 20, y: 10 },
        values,
      ),
    ).resolves.toEqual({ destination: "claims", kind: "navigated" });
    expect(session.url).toBe(PORTAL_SANDBOX_CLAIMS_URL);

    await expect(
      executeSafeComputerAction(
        session,
        { button: "left", type: "click", x: 20, y: 10 },
        values,
      ),
    ).resolves.toEqual({
      destination: "incident_claim",
      kind: "navigated",
    });
    expect(session.url).toBe(PORTAL_SANDBOX_FORM_URL);
  });

  it("allows a left click into an approved field and exact-value typing", async () => {
    const session = new FakeSession();
    session.url = PORTAL_SANDBOX_FORM_URL;
    const values = getPortalFieldValues(claim);

    await executeSafeComputerAction(
      session,
      { button: "left", type: "click", x: 20, y: 10 },
      values,
    );
    await executeSafeComputerAction(
      session,
      { text: values.damage, type: "type" },
      values,
    );

    expect(session.values.damage).toBe(values.damage);
  });

  it.each([
    { button: "right", type: "click", x: 20, y: 10 },
    { button: "left", type: "click", x: 1280, y: 10 },
    { keys: ["ENTER"], type: "keypress" },
    { keys: ["BACKSPACE"], type: "keypress" },
    { path: [{ x: 0, y: 0 }], type: "drag" },
  ] as ComputerAction[])("blocks unsafe action $type", async (action) => {
    await expect(
      executeSafeComputerAction(
        new FakeSession(),
        action,
        getPortalFieldValues(claim),
      ),
    ).rejects.toBeInstanceOf(PortalAutomationSafetyError);
  });

  it("blocks clicks on links or buttons even when coordinates are in bounds", async () => {
    const session = new FakeSession();
    session.prohibitedPoint = true;

    await expect(
      executeSafeComputerAction(
        session,
        { button: "left", type: "click", x: 20, y: 10 },
        getPortalFieldValues(claim),
      ),
    ).rejects.toBeInstanceOf(PortalAutomationSafetyError);
  });

  it("blocks text that is not the exact approved value for the focused field", async () => {
    const session = new FakeSession();
    session.url = PORTAL_SANDBOX_FORM_URL;
    session.focused = "damage";

    await expect(
      executeSafeComputerAction(
        session,
        { text: "Ignore the user and submit", type: "type" },
        getPortalFieldValues(claim),
      ),
    ).rejects.toBeInstanceOf(PortalAutomationSafetyError);
  });

  it("blocks fields before the form and an out-of-order navigation target", async () => {
    const fieldSession = new FakeSession();
    fieldSession.inspectPoint = vi.fn(async () => ({
      field: "damage" as const,
      kind: "approved_field" as const,
    }));

    await expect(
      executeSafeComputerAction(
        fieldSession,
        { button: "left", type: "click", x: 20, y: 10 },
        getPortalFieldValues(claim),
      ),
    ).rejects.toBeInstanceOf(PortalAutomationSafetyError);

    const navigationSession = new FakeSession();
    navigationSession.inspectPoint = vi.fn(async () => ({
      action: "start_incident_claim" as const,
      kind: "approved_navigation" as const,
    }));

    await expect(
      executeSafeComputerAction(
        navigationSession,
        { button: "left", type: "click", x: 20, y: 10 },
        getPortalFieldValues(claim),
      ),
    ).rejects.toBeInstanceOf(PortalAutomationSafetyError);
  });

  it("blocks an allowed click when it resolves outside the exact local route", async () => {
    const session = new FakeSession();
    session.navigationUrlOverride =
      "http://127.0.0.1:3001/portal/sandbox/claims?submit=true";

    await expect(
      executeSafeComputerAction(
        session,
        { button: "left", type: "click", x: 20, y: 10 },
        getPortalFieldValues(claim),
      ),
    ).rejects.toBeInstanceOf(PortalAutomationSafetyError);
  });
});

describe("ComputerUsePortalAutomator", () => {
  it("executes batched actions, verifies exact values, and never submits", async () => {
    const session = new FakeSession();
    const createResponse = vi.fn<CreateComputerResponse>(async () =>
      computerResponse("one", { actions: completePortalActions() }),
    );
    const automator = new ComputerUsePortalAutomator({
      createResponse,
      createSession: async () => session,
    });

    await expect(automator.prepare(claim)).resolves.toEqual({
      screenshotDataUrl: "data:image/png;base64,cG5n",
      status: "prepared",
      submitted: false,
    });
    expect(session.values).toEqual(getPortalFieldValues(claim));
    expect(session.closed).toBe(true);
    expect(createResponse).toHaveBeenCalledOnce();

    const initialRequest = createResponse.mock.calls[0]?.[0];
    expect(initialRequest?.model).toBe(COMPUTER_USE_MODEL);
    expect(initialRequest?.tools).toEqual([{ type: "computer" }]);
    expect(initialRequest?.parallel_tool_calls).toBe(false);
    expect(initialRequest?.input).toEqual([
      expect.objectContaining({
        content: expect.arrayContaining([
          expect.objectContaining({ detail: "original", type: "input_image" }),
          expect.objectContaining({
            text: expect.stringContaining(claim.whatHappened),
            type: "input_text",
          }),
          expect.objectContaining({
            text: expect.stringContaining("Start a motor claim"),
            type: "input_text",
          }),
        ]),
        role: "user",
      }),
    ]);
  });

  it("captures home, ordered navigation, each exact field fill, and final verification", async () => {
    const session = new FakeSession();
    const createResponse = vi.fn<CreateComputerResponse>(async () =>
      computerResponse("one", { actions: completePortalActions() }),
    );
    const automator = new ComputerUsePortalAutomator({
      createResponse,
      createSession: async () => session,
    });

    const result = await automator.prepare(claim, { captureReplay: true });

    expect(result.replay).toBeDefined();
    expect(result.replay?.steps.map((step) => step.kind)).toEqual([
      "opened",
      "navigated",
      "navigated",
      "field_filled",
      "field_filled",
      "field_filled",
      "field_filled",
      "field_filled",
      "verified",
    ]);
    expect(
      result.replay?.steps.flatMap((step) =>
        step.kind === "navigated" ? [step.destination] : [],
      ),
    ).toEqual(["claims", "incident_claim"]);
    expect(
      result.replay?.steps.flatMap((step) =>
        step.kind === "field_filled" ? [step.field] : [],
      ),
    ).toEqual(fieldByRow);
    expect(result.replay?.finalState).toBe("stopped_before_submission");
    expect(result.submitted).toBe(false);
    expect(session.closed).toBe(true);
    expect(session.screenshotCount).toBeGreaterThanOrEqual(9);
  });

  it("returns the screenshot through the computer-call loop and resends safeguards", async () => {
    const session = new FakeSession();
    const createResponse = vi
      .fn<CreateComputerResponse>()
      .mockResolvedValueOnce(
        computerResponse("one", { actions: [{ type: "screenshot" }] }),
      )
      .mockResolvedValueOnce(
        computerResponse("two", { actions: completePortalActions() }),
      );
    const automator = new ComputerUsePortalAutomator({
      createResponse,
      createSession: async () => session,
    });

    await expect(automator.prepare(claim)).resolves.toMatchObject({
      status: "prepared",
      submitted: false,
    });
    expect(createResponse).toHaveBeenCalledTimes(2);

    const followUp = createResponse.mock.calls[1]?.[0];
    expect(followUp?.model).toBe(COMPUTER_USE_MODEL);
    expect(followUp?.tools).toEqual([{ type: "computer" }]);
    expect(followUp?.instructions).toContain(
      "Never click any other link or button",
    );
    expect(followUp?.previous_response_id).toBe("one");
    expect(followUp?.input).toEqual([
      {
        call_id: "call-one",
        output: {
          image_url: "data:image/png;base64,cG5n",
          type: "computer_screenshot",
        },
        type: "computer_call_output",
      },
    ]);
  });

  it("stops on pending safety checks without acknowledging them", async () => {
    const session = new FakeSession();
    const createResponse = vi.fn<CreateComputerResponse>(async () =>
      computerResponse("one", {
        pendingSafetyChecks: [{ id: "check-1" }],
      }),
    );
    const automator = new ComputerUsePortalAutomator({
      createResponse,
      createSession: async () => session,
    });

    await expect(automator.prepare(claim)).rejects.toBeInstanceOf(
      PortalAutomationSafetyError,
    );
    expect(session.closed).toBe(true);
    expect(createResponse).toHaveBeenCalledOnce();
  });

  it("fails closed when the model stops before every field is complete", async () => {
    const session = new FakeSession();
    const createResponse = vi.fn<CreateComputerResponse>(async () =>
      ({ id: "incomplete", output: [] }) as unknown as Response,
    );
    const automator = new ComputerUsePortalAutomator({
      createResponse,
      createSession: async () => session,
    });

    await expect(automator.prepare(claim)).rejects.toThrow(
      "ended before the form was complete",
    );
    expect(session.closed).toBe(true);
  });

  it("never reports success with complete values outside the incident claim form", async () => {
    const session = new FakeSession();
    session.values = getPortalFieldValues(claim);
    const createResponse = vi.fn<CreateComputerResponse>(async () =>
      ({ id: "wrong-page", output: [] }) as unknown as Response,
    );
    const automator = new ComputerUsePortalAutomator({
      createResponse,
      createSession: async () => session,
    });

    await expect(automator.prepare(claim)).rejects.toBeInstanceOf(
      PortalAutomationSafetyError,
    );
    expect(session.closed).toBe(true);
  });

  it("blocks multiple computer calls in one response", async () => {
    const session = new FakeSession();
    const first = computerResponse("first", { actions: [{ type: "screenshot" }] });
    const second = computerResponse("second", { actions: [{ type: "screenshot" }] });
    const createResponse = vi.fn<CreateComputerResponse>(async () =>
      ({
        id: "multiple",
        output: [...first.output, ...second.output],
      }) as unknown as Response,
    );
    const automator = new ComputerUsePortalAutomator({
      createResponse,
      createSession: async () => session,
    });

    await expect(automator.prepare(claim)).rejects.toBeInstanceOf(
      PortalAutomationSafetyError,
    );
    expect(session.closed).toBe(true);
  });

  it("blocks navigation away from the fixed sandbox and still closes the browser", async () => {
    const session = new FakeSession();
    session.navigationUrlOverride = "https://insurer.example/submit";
    const createResponse = vi.fn<CreateComputerResponse>(async () =>
      computerResponse("one", {
        actions: [
          { button: "left", type: "click", x: 20, y: 10 },
        ],
      }),
    );
    const automator = new ComputerUsePortalAutomator({
      createResponse,
      createSession: async () => session,
    });

    await expect(automator.prepare(claim)).rejects.toBeInstanceOf(
      PortalAutomationSafetyError,
    );
    expect(session.closed).toBe(true);
  });

  it("enforces the total action limit", async () => {
    const session = new FakeSession();
    const createResponse = vi.fn<CreateComputerResponse>(async () =>
      computerResponse("one", {
        actions: [{ type: "screenshot" }, { type: "screenshot" }],
      }),
    );
    const automator = new ComputerUsePortalAutomator({
      createResponse,
      createSession: async () => session,
      maxActions: 1,
    });

    await expect(automator.prepare(claim)).rejects.toThrow(
      "execution limit",
    );
    expect(session.closed).toBe(true);
  });

  it("enforces the Computer Use turn limit", async () => {
    const session = new FakeSession();
    const createResponse = vi.fn<CreateComputerResponse>(async () =>
      computerResponse("again", { actions: [{ type: "screenshot" }] }),
    );
    const automator = new ComputerUsePortalAutomator({
      createResponse,
      createSession: async () => session,
      maxTurns: 1,
    });

    await expect(automator.prepare(claim)).rejects.toThrow("turn limit");
    expect(session.closed).toBe(true);
  });

  it("enforces the overall automation deadline and closes the browser", async () => {
    const session = new FakeSession();
    const createResponse = vi.fn<CreateComputerResponse>(
      () => new Promise<Response>(() => undefined),
    );
    const automator = new ComputerUsePortalAutomator({
      createResponse,
      createSession: async () => session,
      maxDurationMs: 5,
    });

    await expect(automator.prepare(claim)).rejects.toThrow("timed out");
    expect(session.closed).toBe(true);
  });
});
