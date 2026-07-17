"use client";

import {
  createContext,
  type ReactNode,
  useContext,
  useMemo,
  useState,
} from "react";

import type { Claim } from "@/lib/analysis-schema";
import type { ComputerUseReplay } from "@/lib/portal-handoff-schema";

export type PreparedPortalHandoff = {
  claim: Claim;
  replay?: ComputerUseReplay;
  screenshotDataUrl: string;
  status: "prepared";
  submitted: false;
};

type PortalHandoffContextValue = {
  preparedHandoff: PreparedPortalHandoff | null;
  setPreparedHandoff: (handoff: PreparedPortalHandoff | null) => void;
};

const PortalHandoffContext = createContext<PortalHandoffContextValue | null>(null);

type PortalHandoffProviderProps = {
  children: ReactNode;
  initialHandoff?: PreparedPortalHandoff | null;
};

export function PortalHandoffProvider({
  children,
  initialHandoff = null,
}: PortalHandoffProviderProps) {
  const [preparedHandoff, setPreparedHandoff] =
    useState<PreparedPortalHandoff | null>(initialHandoff);
  const value = useMemo(
    () => ({ preparedHandoff, setPreparedHandoff }),
    [preparedHandoff],
  );

  return (
    <PortalHandoffContext.Provider value={value}>
      {children}
    </PortalHandoffContext.Provider>
  );
}

export function usePortalHandoff() {
  const context = useContext(PortalHandoffContext);

  if (!context) {
    throw new Error("usePortalHandoff must be used inside PortalHandoffProvider");
  }

  return context;
}
