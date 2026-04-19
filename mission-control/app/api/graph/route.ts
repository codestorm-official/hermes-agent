import { NextResponse } from 'next/server';
import { readQuery } from '@/lib/neo4j';

// Returns the whole graph as a force-directed-friendly payload.
// Nodes: { id, name, label, degree }. Links: { source, target }.
// degree is used on the client to scale node radius.

export const dynamic = 'force-dynamic';
export const revalidate = 0;

type NodeRow = {
  id: { low: number; high: number } | number;
  name: string;
  label: string | null;
  degree: { low: number; high: number } | number;
};
type LinkRow = {
  source: { low: number; high: number } | number;
  target: { low: number; high: number } | number;
};

const toInt = (v: NodeRow['id']) =>
  typeof v === 'number' ? v : v.low + v.high * 2 ** 32;

export async function GET() {
  try {
    const nodeRows = await readQuery<NodeRow>(
      `MATCH (n)
       OPTIONAL MATCH (n)-[r]-()
       WITH n, count(r) AS degree
       RETURN id(n) AS id, n.name AS name, labels(n)[0] AS label, degree`,
    );
    const linkRows = await readQuery<LinkRow>(
      `MATCH (a)-[:MENTIONS]->(b) RETURN id(a) AS source, id(b) AS target`,
    );
    const nodes = nodeRows.map((n) => ({
      id: toInt(n.id),
      name: n.name ?? '(unnamed)',
      label: n.label ?? 'unlabeled',
      degree: toInt(n.degree),
    }));
    const links = linkRows.map((l) => ({
      source: toInt(l.source),
      target: toInt(l.target),
    }));
    return NextResponse.json({ nodes, links });
  } catch (e) {
    return NextResponse.json(
      { error: (e as Error).message },
      { status: 500 },
    );
  }
}
