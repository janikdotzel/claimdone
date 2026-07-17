import type { Claim } from "./analysis-schema";

export const PORTAL_FIELDS = [
  "damage",
  "dateTime",
  "location",
  "whatHappened",
  "attachedPhotos",
] as const;

export type PortalField = (typeof PORTAL_FIELDS)[number];
export type PortalFieldValues = Record<PortalField, string>;

export function normalizePortalFieldValue(value: string): string {
  return value
    .normalize("NFKC")
    .replace(/[\u00a0\u202f]/g, " ")
    .replace(/[\u00b7\u2022\u2027\u2219]/g, "-")
    .replace(/[\u2010\u2011\u2012\u2013\u2014\u2212]/g, "-")
    .replace(/[\u2018\u2019\u201a\u201b]/g, "'")
    .replace(/[\u201c\u201d\u201e\u201f]/g, '"')
    .replace(/\s+/g, " ")
    .trim();
}

export function getAttachmentLabel(photoCount: 1 | 2 | 3): string {
  return `${photoCount} accident ${photoCount === 1 ? "photo" : "photos"} attached`;
}

export function getPortalFieldValues(claim: Claim): PortalFieldValues {
  return {
    attachedPhotos: getAttachmentLabel(claim.photoCount),
    damage: normalizePortalFieldValue(claim.damage),
    dateTime: normalizePortalFieldValue(claim.dateTime),
    location: normalizePortalFieldValue(claim.location),
    whatHappened: normalizePortalFieldValue(claim.whatHappened),
  };
}
