type BrandMarkProps = {
  className?: string | undefined;
};

export function BrandMark({ className }: BrandMarkProps) {
  return (
    <svg
      aria-hidden="true"
      className={className}
      focusable="false"
      viewBox="0 0 76 38"
      xmlns="http://www.w3.org/2000/svg"
    >
      <rect fill="var(--green, #25634f)" height="38" rx="9" width="76" />
      <g
        fill="none"
        stroke="#ffffff"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="2.35"
      >
        <g transform="rotate(-7 15.5 19.5)">
          <rect
            fill="var(--green, #25634f)"
            height="21"
            rx="2.6"
            width="13"
            x="9"
            y="9"
          />
          <circle cx="15.5" cy="19.5" fill="#ffffff" r="1.25" stroke="none" />
        </g>
        <g transform="rotate(-3 21.5 19.5)">
          <rect
            fill="var(--green, #25634f)"
            height="22"
            rx="2.6"
            width="13"
            x="15"
            y="8.5"
          />
          <circle cx="21.5" cy="19.5" fill="#ffffff" r="1.25" stroke="none" />
        </g>
        <rect
          fill="var(--green, #25634f)"
          height="21"
          rx="2.6"
          width="13"
          x="21"
          y="10"
        />
        <circle cx="27.5" cy="20.5" fill="#ffffff" r="1.25" stroke="none" />
        <path d="M36.5 19H42m-2.4-2.5L42 19l-2.4 2.5" />
        <path
          d="M49 7.5h11.5L68 15v12.5a3 3 0 0 1-3 3H49a3 3 0 0 1-3-3v-17a3 3 0 0 1 3-3Z"
          fill="var(--green, #25634f)"
        />
        <path d="M60.5 7.5v5A2.5 2.5 0 0 0 63 15h5M51 17.5h10M51 20.5h8m-8 3.5 3.4 3.3 7.4-7.1" />
      </g>
    </svg>
  );
}
