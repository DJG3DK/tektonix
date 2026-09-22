/* The Tektonix mark on its own: the plumb line, without the wordmark.
 *
 * Why a drawn SVG rather than the favicon or the logo PNG. The favicon is a
 * rounded tile with its own dark background baked in -- on the dashboard's
 * dark chat surface that reads as a flat dark square with a T on it, which is
 * the opposite of standing out. The logo PNG is the full lockup, mark plus
 * wordmark at 815x216, and cropping a 32px avatar out of it wastes most of
 * the file on type nobody can read at that size.
 *
 * Drawn, it floats: no tile, no plate, no circle, just the mark against
 * whatever is behind it. It also stays crisp at 32px on a retina display,
 * which a 32px PNG does not.
 *
 * The colours are the brand's, not the theme's. `--accent` is per-theme (gold,
 * periwinkle, orchid) and following it would recolour the plumb bob to match
 * whichever theme is on -- cohesive, and no longer the Tektonix mark. The
 * sidebar lockup is fixed gold for the same reason, and these two sit on the
 * same screen.
 */

const T_COLOR = "#e8edf4";   // the light blue-white of the lockup's letterforms
const BOB_COLOR = "#d4a72c"; // the plumb bob

export function TektonixMark({ size = 24, className = "" }: { size?: number; className?: string }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 24 24"
      fill="none"
      className={className}
      // Decorative: the name is already beside it in text, and a screen
      // reader announcing "Tektonix logo, Agent" is worse than "Agent".
      aria-hidden="true"
      focusable="false"
    >
      {/* The T -- crossbar and stem as one filled path, proportioned off the
          lockup (stem ≈ 19% of the bar's width, bar ≈ 15% of the height). */}
      <path d="M3 2h18v3.1h-7.15v7.6h-3.7V5.1H3V2Z" fill={T_COLOR} />
      {/* The plumb bob, hanging from the stem. */}
      <path d="M6.6 12.7h10.8L12 22 6.6 12.7Z" fill={BOB_COLOR} />
    </svg>
  );
}
