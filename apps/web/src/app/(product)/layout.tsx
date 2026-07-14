import type { ReactNode } from "react";

import { ProductChrome } from "../../components/ui";

export default function ProductLayout({ children }: Readonly<{ children: ReactNode }>) {
  return <ProductChrome>{children}</ProductChrome>;
}
