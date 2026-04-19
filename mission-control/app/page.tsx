import { Suspense } from 'react';
import { readQuery } from '@/lib/neo4j';
import Graph from '@/components/Graph';
import LeftColumn from '@/components/LeftColumn';
import IngestStatus from '@/components/IngestStatus';

export const dynamic = 'force-dynamic';
export const revalidate = 0;

type Count = { n: { low: number; high: number } | number };

const toInt = (v: Count['n']) =>
  typeof v === 'number' ? v : v.low + v.high * 2 ** 32;

async function safeCount(cypher: string): Promise<number | null> {
  // Each stat is its own query so one broken Cypher doesn't strip the
  // whole top bar. Returns null on failure - the UI renders "—".
  try {
    const rows = await readQuery<Count>(cypher);
    if (!rows.length) return 0;
    return toInt(rows[0].n);
  } catch {
    return null;
  }
}

async function loadStats() {
  return {
    nodes: await safeCount('MATCH (n) RETURN count(n) AS n'),
    edges: await safeCount('MATCH ()-[r]->() RETURN count(r) AS n'),
    // date(d.date) is identity on a Neo4j Date, parses ISO strings, and
    // extracts the date part from a DateTime. Safer than datetime(d.date)
    // which would reject plain Date values written by the YAML ingester.
    recent: await safeCount(
      `MATCH (d:Daily)-[:MENTIONS]->(e)
       WHERE d.date IS NOT NULL
         AND date(d.date) > date() - duration({days: 1})
       RETURN count(DISTINCT e) AS n`,
    ),
  };
}

export default async function Page() {
  const stats = await loadStats();
  const ts = new Date().toISOString().slice(0, 16).replace('T', ' ');
  const render = (v: number | null) => (v == null ? '—' : v.toLocaleString('de-DE'));

  return (
    <main>
      <header className="topbar">
        <div className="brand">
          <div className="brand-name">Hermes</div>
          <div className="brand-sub">mission control · {ts}z</div>
        </div>
        <div className="stat-row">
          <div className="stat">
            <div className="stat-label">Nodes</div>
            <div className="stat-value">{render(stats.nodes)}</div>
          </div>
          <div className="stat">
            <div className="stat-label">Edges</div>
            <div className="stat-value">{render(stats.edges)}</div>
          </div>
          <div className="stat">
            <div className="stat-label">Active 24h</div>
            <div className="stat-value accent">{render(stats.recent)}</div>
          </div>
        </div>
        <div className="topbar-right">
          <IngestStatus />
          <div className="pulse">
            <span className="pulse-dot" />
            live
          </div>
        </div>
      </header>
      <div className="stage">
        <Suspense>
          <LeftColumn />
        </Suspense>
        <Suspense>
          <Graph />
        </Suspense>
      </div>
    </main>
  );
}
