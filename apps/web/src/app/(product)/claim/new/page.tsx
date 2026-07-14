import type { Metadata } from "next";

import { IntakeFlow } from "../../../../features/intake/intake-flow";

export const metadata: Metadata = {
  description:
    "Prepare a staged claim intake with local image, statement, consent, and metadata checks.",
  title: "New sandbox intake",
};

export default function NewClaimPage() {
  return <IntakeFlow />;
}
