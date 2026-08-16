type BillSenseLogoProps = {
  className?: string;
  compact?: boolean;
};

export function BillSenseLogo({ className = "", compact = false }: BillSenseLogoProps) {
  return (
    <div className={`bill-sense-logo ${compact ? "compact" : ""} ${className}`.trim()}>
      <svg viewBox="0 0 420 180" aria-label="BillSense" role="img">
        <g transform="translate(40 20)">
          <path
            d="M48 110C48 70 62 42 86 42C100 42 110 49 118 58L124 64L130 58C137 49 148 42 162 42C186 42 200 70 200 110V124H164V92C164 79 157 72 144 72C132 72 125 79 125 92V124H89V92C89 79 82 72 70 72C57 72 50 79 50 92V124H48V110Z"
            fill="url(#billSenseBlue)"
          />
          <path
            d="M62 120C62 92 76 70 100 70C120 70 133 82 138 96C143 82 157 70 177 70C201 70 216 92 216 120V124H62V120Z"
            fill="url(#billSenseBlueDark)"
            opacity="0.9"
          />
          <circle cx="42" cy="42" r="22" fill="#1bc9ff" />
          <circle cx="42" cy="42" r="10" fill="#ffffff" opacity="0.9" />
          <circle cx="226" cy="42" r="22" fill="#1bc9ff" />
          <circle cx="226" cy="42" r="10" fill="#ffffff" opacity="0.9" />
          <circle cx="75" cy="84" r="12" fill="#0e2d41" />
          <circle cx="147" cy="84" r="12" fill="#0e2d41" />
          <path d="M96 108C106 116 117 120 127 120C138 120 149 116 159 108" stroke="#0e2d41" strokeWidth="7" strokeLinecap="round" fill="none" />
        </g>
        <defs>
          <linearGradient id="billSenseBlue" x1="0" x2="1">
            <stop offset="0%" stopColor="#5de0ff" />
            <stop offset="100%" stopColor="#1eaef8" />
          </linearGradient>
          <linearGradient id="billSenseBlueDark" x1="0" x2="1">
            <stop offset="0%" stopColor="#1ab0f3" />
            <stop offset="100%" stopColor="#0d7ad6" />
          </linearGradient>
        </defs>
      </svg>
      <div className="bill-sense-wordmark" aria-hidden="true">
        <span className="bill-sense-bill">Bill</span>
        <span className="bill-sense-sense">Sense</span>
      </div>
    </div>
  );
}
