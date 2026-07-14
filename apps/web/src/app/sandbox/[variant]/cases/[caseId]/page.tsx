import { notFound } from "next/navigation";

import { PORTAL_VARIANTS, type PortalVariant } from "../../../../../features/sandbox/contracts";
import { SandboxPortalClient } from "../../../../../features/sandbox/portal-client";

interface SandboxCasePageProps {
  readonly params: Promise<{
    readonly caseId: string;
    readonly variant: string;
  }>;
}

export default async function SandboxCasePage({ params }: SandboxCasePageProps) {
  const { caseId, variant } = await params;
  if (!(PORTAL_VARIANTS as readonly string[]).includes(variant)) notFound();
  return <SandboxPortalClient caseId={caseId} variant={variant as PortalVariant} />;
}
