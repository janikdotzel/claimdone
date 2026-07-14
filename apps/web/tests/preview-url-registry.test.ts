import { describe, expect, it, vi } from "vitest";

import { PreviewUrlRegistry } from "../src/features/intake";

describe("preview URL lifecycle", () => {
  it("revokes removed previews once and all remaining previews on cleanup", () => {
    const revokeObjectURL = vi.fn();
    let sequence = 0;
    const registry = new PreviewUrlRegistry({
      createObjectURL: () => `blob:fixture-${++sequence}`,
      revokeObjectURL,
    });

    const first = registry.create(new Blob(["one"]));
    const second = registry.create(new Blob(["two"]));
    expect(registry.size).toBe(2);

    expect(registry.release(first)).toBe(true);
    expect(registry.release(first)).toBe(false);
    registry.releaseAll();

    expect(revokeObjectURL.mock.calls).toEqual([[first], [second]]);
    expect(registry.size).toBe(0);
  });
});
