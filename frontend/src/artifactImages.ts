/** An image the agent showed the operator (agent/tools/show_tools.py): a
 * Markdown image whose URL is one of OUR stored images -- nothing else. The
 * pattern is the whole allow-list: an agent cannot embed an outside URL (a
 * tracking pixel, a page that is not an image), because nothing but
 * /api/artifacts/<project>/<32 hex> is ever turned into an <img>. */
const ARTIFACT_IMAGE = /!\[([^\]\n]{0,200})\]\(\/api\/artifacts\/([A-Za-z0-9][A-Za-z0-9._-]{0,63})\/([0-9a-f]{32})\)/g;

/** The URL is rebuilt from the two matched parts rather than passed through
 * from the text, so what reaches an href or src always starts with our own
 * path and can be nothing but a stored image's address. */
function artifactUrl(project: string, id: string): string {
  return "/api/artifacts/" + project + "/" + id;
}

export function artifactImages(text: string): { caption: string; url: string }[] {
  return [...(text || "").matchAll(ARTIFACT_IMAGE)].map((m) => ({ caption: m[1], url: artifactUrl(m[2], m[3]) }));
}

type RichPart = { kind: "text"; text: string } | { kind: "image"; caption: string; url: string };

/** Text split around those images, in order. */
export function splitArtifactImages(text: string): RichPart[] {
  const parts: RichPart[] = [];
  let last = 0;
  for (const m of (text || "").matchAll(ARTIFACT_IMAGE)) {
    if ((m.index ?? 0) > last) parts.push({ kind: "text", text: text.slice(last, m.index) });
    parts.push({ kind: "image", caption: m[1], url: artifactUrl(m[2], m[3]) });
    last = (m.index ?? 0) + m[0].length;
  }
  if (last < (text || "").length || !parts.length) parts.push({ kind: "text", text: (text || "").slice(last) });
  return parts;
}
