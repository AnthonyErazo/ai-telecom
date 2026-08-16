/** Logo oficial de Movistar según la marca corporativa. */
export default function MovistarLogo({
  className = "h-7 w-auto",
  fill = "#019DF4",
}: { className?: string; fill?: string }) {
  return (
    <img
      src="https://cert-cdn.movistar.com.pe/2024/12/logo-2.svg"
      alt="Movistar"
      className={className}
      style={{ filter: fill !== "#019DF4" ? `drop-shadow(0 0 0 ${fill})` : undefined }}
    />
  );
}
