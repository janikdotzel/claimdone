import type { Metadata } from "next";

import { IntakeFlow } from "../../../../features/intake/intake-flow";

export const metadata: Metadata = {
  description:
    "Turn three incident photos and a short statement into a complete, reviewable insurance claim.",
  title: "Start your claim",
};

export default function NewClaimPage() {
  return <IntakeFlow />;
}
