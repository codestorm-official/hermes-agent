import Graph from 'graphology';
import louvain from 'graphology-communities-louvain';

// 10-colour harmonic palette tuned to the existing label colours. The
// first slot is the amber accent (reserved for the dominant cluster so
// it reads as "Ari's main working set"); the rest are muted earth +
// slate tones that sit alongside Person/Company/Property/Task hues
// without clashing.
export const COMMUNITY_COLORS = [
  '#fb923c', // amber - dominant cluster
  '#a3b18a', // sage
  '#c8b6ff', // pale lavender
  '#f4d35e', // mustard
  '#8ab0d6', // steel
  '#e57373', // coral
  '#b8a89a', // taupe
  '#9fb8a3', // muted sage-blue
  '#d4a373', // bronze
  '#94a3a1', // slate
] as const;

export type CommunityMap = Map<number, number>; // nodeId -> cluster index

export function computeCommunities(
  nodes: { id: number }[],
  links: { source: number | { id: number }; target: number | { id: number } }[],
): CommunityMap {
  if (nodes.length === 0) return new Map();
  const g = new Graph({ multi: false, type: 'undirected' });
  for (const n of nodes) g.addNode(String(n.id));
  for (const l of links) {
    const s = typeof l.source === 'number' ? l.source : l.source.id;
    const t = typeof l.target === 'number' ? l.target : l.target.id;
    if (s === t) continue;
    const ss = String(s);
    const tt = String(t);
    if (!g.hasNode(ss) || !g.hasNode(tt)) continue;
    if (!g.hasEdge(ss, tt)) g.addEdge(ss, tt);
  }
  // Louvain returns {nodeId: communityNumber}. Community numbers are
  // arbitrary, so we remap to 0..N-1 in order of appearance for stable
  // palette assignment.
  const raw = louvain(g) as Record<string, number>;
  const ordered = new Map<number, number>();
  const seen = new Map<number, number>();
  let next = 0;
  for (const n of nodes) {
    const cid = raw[String(n.id)];
    if (cid == null) {
      ordered.set(n.id, 0);
      continue;
    }
    if (!seen.has(cid)) {
      seen.set(cid, next++);
    }
    ordered.set(n.id, seen.get(cid)!);
  }
  // Return communities ordered by SIZE so index 0 = largest cluster
  // (gets the amber accent colour).
  const sizes = new Map<number, number>();
  for (const c of ordered.values()) sizes.set(c, (sizes.get(c) ?? 0) + 1);
  const ranking = [...sizes.entries()]
    .sort((a, b) => b[1] - a[1])
    .map(([c], i) => [c, i] as const);
  const rankMap = new Map<number, number>(ranking);
  const final = new Map<number, number>();
  for (const [id, c] of ordered) final.set(id, rankMap.get(c) ?? 0);
  return final;
}

export function communityColor(idx: number | undefined): string {
  if (idx == null) return '#3a3a3a';
  return COMMUNITY_COLORS[idx % COMMUNITY_COLORS.length];
}
