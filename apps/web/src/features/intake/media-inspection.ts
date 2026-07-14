import {
  MAX_IMAGE_BYTES,
  type ImageSignature,
} from "./types";

const JPEG_MIME = "image/jpeg";
const PNG_MIME = "image/png";
const JPEG_EXIF = [0x45, 0x78, 0x69, 0x66, 0x00, 0x00] as const;
const PNG_SIGNATURE = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a] as const;

export type ImageInspectionResult = Readonly<{
  error: string | null;
  metadataFound: boolean | null;
  metadataSummary: string;
  signature: ImageSignature | null;
}>;

function startsWith(bytes: Uint8Array, signature: ReadonlyArray<number>) {
  return signature.every((value, index) => bytes[index] === value);
}

function startsWithAt(
  bytes: Uint8Array,
  signature: ReadonlyArray<number>,
  offset: number,
) {
  return signature.every((value, index) => bytes[offset + index] === value);
}

function jpegContainsExifSegment(bytes: Uint8Array) {
  let offset = 2;
  while (offset + 4 <= bytes.length) {
    while (bytes[offset] === 0xff) offset += 1;
    const marker = bytes[offset];
    if (marker === undefined || marker === 0xd9 || marker === 0xda) return false;
    offset += 1;

    if (marker === 0x01 || (marker >= 0xd0 && marker <= 0xd7)) continue;
    if (offset + 2 > bytes.length) return false;

    const segmentLength =
      ((bytes[offset] ?? 0) << 8) | (bytes[offset + 1] ?? 0);
    if (segmentLength < 2 || offset + segmentLength > bytes.length) return false;
    if (marker === 0xe1 && startsWithAt(bytes, JPEG_EXIF, offset + 2)) {
      return true;
    }
    offset += segmentLength;
  }
  return false;
}

function pngContainsExifChunk(bytes: Uint8Array) {
  let offset = PNG_SIGNATURE.length;
  while (offset + 12 <= bytes.length) {
    const length =
      ((bytes[offset] ?? 0) << 24) |
      ((bytes[offset + 1] ?? 0) << 16) |
      ((bytes[offset + 2] ?? 0) << 8) |
      (bytes[offset + 3] ?? 0);
    const unsignedLength = length >>> 0;
    if (unsignedLength > bytes.length || offset + 12 + unsignedLength > bytes.length) {
      return false;
    }
    const chunkType = String.fromCharCode(
      bytes[offset + 4] ?? 0,
      bytes[offset + 5] ?? 0,
      bytes[offset + 6] ?? 0,
      bytes[offset + 7] ?? 0,
    );
    if (chunkType === "eXIf") return true;
    if (chunkType === "IEND") return false;
    offset += 12 + unsignedLength;
  }
  return false;
}

export function inspectImageBytes({
  bytes,
  mimeType,
  size,
}: Readonly<{
  bytes: Uint8Array;
  mimeType: string;
  size: number;
}>): ImageInspectionResult {
  if (size <= 0) {
    return {
      error: "The image is empty.",
      metadataFound: null,
      metadataSummary: "Inspection failed",
      signature: null,
    };
  }
  if (size > MAX_IMAGE_BYTES) {
    return {
      error: "Each image must be 10 MB or smaller.",
      metadataFound: null,
      metadataSummary: "Inspection failed",
      signature: null,
    };
  }

  const isJpeg = startsWith(bytes, [0xff, 0xd8, 0xff]);
  const isPng = startsWith(bytes, PNG_SIGNATURE);
  const signature: ImageSignature | null = isJpeg ? "jpeg" : isPng ? "png" : null;
  const expectedMime = signature === "jpeg" ? JPEG_MIME : signature === "png" ? PNG_MIME : null;

  if (signature === null) {
    return {
      error: "The file signature is not a valid JPG or PNG image.",
      metadataFound: null,
      metadataSummary: "Inspection failed",
      signature: null,
    };
  }
  if (mimeType !== expectedMime) {
    return {
      error: `The declared file type does not match the ${signature.toUpperCase()} signature.`,
      metadataFound: null,
      metadataSummary: "Inspection failed",
      signature,
    };
  }

  const metadataFound =
    signature === "jpeg"
      ? jpegContainsExifSegment(bytes)
      : pngContainsExifChunk(bytes);
  return {
    error: null,
    metadataFound,
    metadataSummary: metadataFound
      ? "Embedded EXIF metadata detected locally."
      : "No embedded EXIF metadata detected.",
    signature,
  };
}

export async function inspectImageFile(file: File): Promise<ImageInspectionResult> {
  const bytes = new Uint8Array(await file.arrayBuffer());
  return inspectImageBytes({ bytes, mimeType: file.type, size: file.size });
}

export function imageFingerprint(file: File) {
  return `${file.name}:${file.size}:${file.lastModified}:${file.type}`;
}

export function isSupportedImageMime(mimeType: string) {
  return mimeType === JPEG_MIME || mimeType === PNG_MIME;
}
