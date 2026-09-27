/** An href for a link whose URL came from the server rather than from this
 * code: http(s) only, anything else (javascript:, data:, a bare string) is
 * no link at all. React warns about javascript: URLs and still renders them. */
export function safeHref(raw: string | null | undefined): string | undefined {
  if (!raw) return undefined;
  try {
    const u = new URL(raw);
    return u.protocol === "https:" || u.protocol === "http:" ? u.href : undefined;
  } catch {
    return undefined;
  }
}
