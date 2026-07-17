import type { Metadata } from "next";
import type { ReactNode } from "react";

import { PortalHandoffProvider } from "./portal/portal-handoff-context";
import "./globals.css";

export const metadata: Metadata = {
  description:
    "A minimal demo that turns accident photos and a short statement into a claim preview.",
  title: "ClaimDone",
};

export default function RootLayout({ children }: Readonly<{ children: ReactNode }>) {
  return (
    <html data-scroll-behavior="smooth" lang="en">
      <body>
        <a className="skip-link" href="#main-content">
          Skip to main content
        </a>
        <PortalHandoffProvider>{children}</PortalHandoffProvider>
      </body>
    </html>
  );
}
