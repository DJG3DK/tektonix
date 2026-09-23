/** An image the agent showed the operator (agent/tools/show_tools.py): a
 * Markdown image whose URL is one of OUR stored images -- nothing else. The
 * pattern is the whole allow-list: an agent cannot embed an outside URL (a
 * tracking pixel, a page that is not an image), because nothing but
 * /api/artifacts/<project>/<32 hex> is ever turned into an <img>. */
const ARTIFACT_IMAGE = /!\[([^\]\n]{0,200})\]\((\/api\/artifacts\/[A-Za-z0-9._-]{1,64}\/[0-9a-f]{32})\)/g;

export function artifactImages(text: string): { caption: string; url: string }[] {
  return [...(text || "").matchAll(ARTIFACT_IMAGE)].map((m) => ({ caption: m[1], url: m[2] }));
}

export type RichPart = { kind: "text"; text: string } | { kind: "image"; caption: string; url: string };

/** Text split around those images, in order. */
export function splitArtifactImages(text: string): RichPart[] {
  const parts: RichPart[] = [];
  let last = 0;
  for (const m of (text || "").matchAll(ARTIFACT_IMAGE)) {
    if ((m.index ?? 0) > last) parts.push({ kind: "text", text: text.slice(last, m.index) });
    parts.push({ kind: "image", caption: m[1], url: m[2] });
    last = (m.index ?? 0) + m[0].length;
  }
  if (last < (text || "").length || !parts.length) parts.push({ kind: "text", text: (text || "").slice(last) });
  return parts;
}
