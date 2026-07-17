import type { SVGProps } from "react";

type IconProps = SVGProps<SVGSVGElement>;

const commonProps = {
  "aria-hidden": true,
  fill: "none",
  focusable: false,
  stroke: "currentColor",
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
  strokeWidth: 1.8,
  viewBox: "0 0 24 24",
};

export function CameraIcon(props: IconProps) {
  return (
    <svg {...commonProps} {...props}>
      <path d="M14.5 5 13 3H9L7.5 5H5a3 3 0 0 0-3 3v9a3 3 0 0 0 3 3h14a3 3 0 0 0 3-3V8a3 3 0 0 0-3-3h-4.5Z" />
      <circle cx="11" cy="12.5" r="4" />
    </svg>
  );
}

export function MicIcon(props: IconProps) {
  return (
    <svg {...commonProps} {...props}>
      <rect height="12" rx="4" width="7" x="8.5" y="2" />
      <path d="M5 10a7 7 0 0 0 14 0M12 17v4M9 21h6" />
    </svg>
  );
}

export function MessageIcon(props: IconProps) {
  return (
    <svg {...commonProps} {...props}>
      <path d="M21 14a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4v7Z" />
      <path d="M7 8h10M7 12h7" />
    </svg>
  );
}

export function ScanIcon(props: IconProps) {
  return (
    <svg {...commonProps} {...props}>
      <path d="M3 8V5a2 2 0 0 1 2-2h3M16 3h3a2 2 0 0 1 2 2v3M21 16v3a2 2 0 0 1-2 2h-3M8 21H5a2 2 0 0 1-2-2v-3" />
      <circle cx="11" cy="11" r="4" />
      <path d="m14 14 3 3" />
    </svg>
  );
}

export function OrderedListIcon(props: IconProps) {
  return (
    <svg {...commonProps} {...props}>
      <path d="M10 6h11M10 12h11M10 18h11M4 6h1v3M4 12h2l-2 3h2M4 18h2l-2 3h2" />
    </svg>
  );
}

export function FilePlusIcon(props: IconProps) {
  return (
    <svg {...commonProps} {...props}>
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z" />
      <path d="M14 2v6h6M12 12v6M9 15h6" />
    </svg>
  );
}

export function FileCheckIcon(props: IconProps) {
  return (
    <svg {...commonProps} {...props}>
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8Z" />
      <path d="M14 2v6h6m-12 7 2.5 2.5L16 12" />
    </svg>
  );
}

export function CalendarIcon(props: IconProps) {
  return (
    <svg {...commonProps} {...props}>
      <rect height="18" rx="2" width="18" x="3" y="4" />
      <path d="M16 2v4M8 2v4M3 10h18" />
    </svg>
  );
}

export function MapPinIcon(props: IconProps) {
  return (
    <svg {...commonProps} {...props}>
      <path d="M20 10c0 5-8 12-8 12S4 15 4 10a8 8 0 1 1 16 0Z" />
      <circle cx="12" cy="10" r="2.5" />
    </svg>
  );
}

export function CarIcon(props: IconProps) {
  return (
    <svg {...commonProps} {...props}>
      <path d="m5 17-1 3M19 17l1 3M3 12l2-6h14l2 6v6H3v-6Z" />
      <path d="M5 12h14M7 15h.01M17 15h.01" />
    </svg>
  );
}

export function BadgeIcon(props: IconProps) {
  return (
    <svg {...commonProps} {...props}>
      <rect height="14" rx="2" width="18" x="3" y="5" />
      <circle cx="8" cy="12" r="2" />
      <path d="M12 10h5M12 14h4" />
    </svg>
  );
}

export function RouteIcon(props: IconProps) {
  return (
    <svg {...commonProps} {...props}>
      <circle cx="6" cy="19" r="2" />
      <circle cx="18" cy="5" r="2" />
      <path d="M8 19h4a3 3 0 0 0 0-6H9a3 3 0 0 1 0-6h7" />
    </svg>
  );
}

export function LinkIcon(props: IconProps) {
  return (
    <svg {...commonProps} {...props}>
      <path d="M10 13a5 5 0 0 0 7.5.5l2-2a5 5 0 0 0-7-7l-1.2 1.2" />
      <path d="M14 11a5 5 0 0 0-7.5-.5l-2 2a5 5 0 0 0 7 7l1.2-1.2" />
    </svg>
  );
}

export function SendIcon(props: IconProps) {
  return (
    <svg {...commonProps} {...props}>
      <path d="m22 2-7 20-4-9-9-4Z" />
      <path d="M22 2 11 13" />
    </svg>
  );
}

export function EyeIcon(props: IconProps) {
  return (
    <svg {...commonProps} {...props}>
      <path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6S2 12 2 12Z" />
      <circle cx="12" cy="12" r="2.5" />
    </svg>
  );
}

export function ChevronIcon(props: IconProps) {
  return (
    <svg {...commonProps} {...props}>
      <path d="m6 9 6 6 6-6" />
    </svg>
  );
}
