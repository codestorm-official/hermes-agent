import { readQuery } from '@/lib/neo4j';
import Graph from '@/components/Graph';

export const dynamic = 'force-dynamic';
export const revalidate = 0;

type Count = { n: { low: number; high: number } | number };

async function loadStats() {
  const [nodesRow] = await readQuery<Count>(
    'MATCH (n) RETURN count(n) AS n',
  );
  const [edgesRow] = await readQuery<Count>(
    'MATCH ()-[r]->() RETURN count(r) AS n',
  );
  const [recentRow] = await readQuery<Count>(
    `MATCH (d:Daily)-[:MENTIONS]->(e)
     WHERE d.date IS NOT NULL
       AND datetime() - duration({hours: 24}) < datetime(d.date)
     RETURN count(DISTINCT e) AS n`,
  );
  const toInt = (v: Count['n']) =>
    typeof v === 'number' ? v : v.low + v.high * 2 ** 32;
  return {
    nodes: toInt(nodesRow.n),
    edges: toInt(edgesRow.n),
    recent: toInt(recentRow?.n ?? 0),
  };
}

export default async function Page() {
  let stats: { nodes: number; edges: number; recent: number } | null = null;
  let errMsg: string | null = null;
  try {
    stats = await loadStats();
  } catch (e) {
    errMsg = (e as Error).message;
  }

  const ts = new Date().toISOString().slice(0, 16).replace('T', ' ');

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
            <div className="stat-value">{stats?.nodes ?? '—'}</div>
          </div>
          <div className="stat">
            <div className="stat-label">Edges</div>
            <div className="stat-value">{stats?.edges ?? '—'}</div>
          </div>
          <div className="stat">
            <div className="stat-label">Active 24h</div>
            <div className="stat-value accent">{stats?.recent ?? '—'}</div>
          </div>
        </div>
        <div className="pulse">
          <span className="pulse-dot" />
          live
        </div>
      </header>
      {errMsg ? (
        <div className="err">Neo4j connect failed: {errMsg}</div>
      ) : (
        <Graph />
      )}
    </main>
  );
}
