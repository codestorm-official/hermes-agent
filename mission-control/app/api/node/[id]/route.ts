import { NextRequest, NextResponse } from 'next/server';
import { readQuery } from '@/lib/neo4j';

// Full detail for a single node: props + 1-hop neighbours grouped by
// direction. Called from the Detail Panel when a user clicks a node.

export const dynamic = 'force-dynamic';
export const revalidate = 0;

type NodeRow = {
  id: { low: number; high: number } | number;
  name: string;
  label: string | null;
  props: Record<string, unknown>;
  degree: { low: number; high: number } | number;
};
type NeighbourRow = {
  id: { low: number; high: number } | number;
  name: string;
  label: string | null;
  direction: 'out' | 'in';
};

const toInt = (v: NodeRow['id']) =>
  typeof v === 'number' ? v : v.low + v.high * 2 ** 32;

export async function GET(
  _req: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  const { id: idStr } = await params;
  const id = Number.parseInt(idStr, 10);
  if (!Number.isFinite(id)) {
    return NextResponse.json({ error: 'invalid id' }, { status: 400 });
  }

  try {
    const [nodeRow] = await readQuery<NodeRow>(
      `MATCH (n) WHERE id(n) = $id
       OPTIONAL MATCH (n)-[r]-()
       WITH n, count(r) AS degree
       RETURN id(n) AS id, n.name AS name, labels(n)[0] AS label,
              properties(n) AS props, degree`,
      { id },
    );
    if (!nodeRow) {
      return NextResponse.json({ error: 'not found' }, { status: 404 });
    }

    const neighbours = await readQuery<NeighbourRow>(
      `MATCH (n)-[r]-(m) WHERE id(n) = $id
       RETURN DISTINCT id(m) AS id, m.name AS name, labels(m)[0] AS label,
              CASE WHEN startNode(r) = n THEN 'out' ELSE 'in' END AS direction
       ORDER BY m.name`,
      { id },
    );

    // Clean node props for JSON transport.
    const cleanProps: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(nodeRow.props)) {
      if (v == null) continue;
      // Neo4j Integer: { low, high }
      if (typeof v === 'object' && 'low' in (v as object) && 'high' in (v as object)) {
        cleanProps[k] = toInt(v as NodeRow['id']);
      } else if (Array.isArray(v)) {
        cleanProps[k] = v;
      } else if (typeof v === 'object') {
        cleanProps[k] = String(v);
      } else {
        cleanProps[k] = v;
      }
    }

    return NextResponse.json({
      id: toInt(nodeRow.id),
      name: nodeRow.name ?? '(unnamed)',
      label: nodeRow.label ?? 'unlabeled',
      degree: toInt(nodeRow.degree),
      props: cleanProps,
      neighbours: neighbours.map((n) => ({
        id: toInt(n.id),
        name: n.name ?? '(unnamed)',
        label: n.label ?? 'unlabeled',
        direction: n.direction,
      })),
    });
  } catch (e) {
    return NextResponse.json(
      { error: (e as Error).message },
      { status: 500 },
    );
  }
}
