import { describe, expect, it } from "vitest";

import {
  MAX_IMAGE_BYTES,
  inspectImageBytes,
  validateAudioDuration,
} from "../src/features/intake";

describe("local media preflight", () => {
  it("recognizes a JPG signature and embedded EXIF marker", () => {
    const bytes = new Uint8Array([
      0xff,
      0xd8,
      0xff,
      0xe1,
      0x00,
      0x08,
      0x45,
      0x78,
      0x69,
      0x66,
      0x00,
      0x00,
      0xff,
      0xd9,
    ]);
    expect(
      inspectImageBytes({ bytes, mimeType: "image/jpeg", size: bytes.length }),
    ).toEqual({
      error: null,
      metadataFound: true,
      metadataSummary: "Embedded EXIF metadata detected locally.",
      signature: "jpeg",
    });
  });

  it("does not treat EXIF-like pixel bytes outside an APP1 segment as metadata", () => {
    const bytes = new Uint8Array([
      0xff,
      0xd8,
      0xff,
      0xda,
      0x45,
      0x78,
      0x69,
      0x66,
      0x00,
      0x00,
      0xff,
      0xd9,
    ]);
    expect(
      inspectImageBytes({ bytes, mimeType: "image/jpeg", size: bytes.length })
        .metadataFound,
    ).toBe(false);
  });

  it("recognizes a PNG eXIf chunk", () => {
    const bytes = new Uint8Array([
      0x89,
      0x50,
      0x4e,
      0x47,
      0x0d,
      0x0a,
      0x1a,
      0x0a,
      0x00,
      0x00,
      0x00,
      0x00,
      0x65,
      0x58,
      0x49,
      0x66,
      0x00,
      0x00,
      0x00,
      0x00,
    ]);
    const result = inspectImageBytes({
      bytes,
      mimeType: "image/png",
      size: bytes.length,
    });
    expect(result.signature).toBe("png");
    expect(result.metadataFound).toBe(true);
  });

  it("rejects a MIME/signature mismatch, unknown bytes, empty files, and oversize files", () => {
    const jpeg = new Uint8Array([0xff, 0xd8, 0xff, 0xd9]);
    expect(
      inspectImageBytes({ bytes: jpeg, mimeType: "image/png", size: jpeg.length }).error,
    ).toMatch(/does not match/);
    expect(
      inspectImageBytes({
        bytes: new Uint8Array([1, 2, 3]),
        mimeType: "image/jpeg",
        size: 3,
      }).error,
    ).toMatch(/signature/);
    expect(
      inspectImageBytes({
        bytes: new Uint8Array(),
        mimeType: "image/png",
        size: 0,
      }).error,
    ).toMatch(/empty/);
    expect(
      inspectImageBytes({
        bytes: jpeg,
        mimeType: "image/jpeg",
        size: MAX_IMAGE_BYTES + 1,
      }).error,
    ).toMatch(/10 MB/);
  });

  it("accepts at most sixty seconds of audio", () => {
    expect(validateAudioDuration(60)).toBeNull();
    expect(validateAudioDuration(60.001)).toBe("Audio must be 60 seconds or less.");
    expect(validateAudioDuration(Number.NaN)).toBe(
      "The audio duration could not be read.",
    );
  });
});
