import { NextResponse } from 'next/server';
import { readQuery } from '@/lib/neo4j';

// IngestRun singleton written by graph-ingester/ingest.py on every
// cycle. Mission Control surfaces this in the top bar so the operator
// sees at a glance whether the ingester is still alive.

export const dynamic = 'force-dynamic';
export const revalidate = 0;

type Int = { low: number; high: number } | number;
type Row = {
  at: { year: Int; month: Int; day: Int; hour: Int; minute: Int; second: Int } | string | null;
  files: Int | null;
  mentions: Int | null;
  refers: Int | null;
  duration_ms: Int | null;
  error: string | null;
};

const toInt = (v: Int | null | undefined): number | null =>
  v == null ? null : typeof v === 'number' ? v : v.low + v.high * 2 ** 32;

function atToIso(v: Row['at']): string | null {
  if (v == null) return null;
  if (typeof v === 'string') return v;
  const o: any = v;
  const y = toInt(o.year);
  const m = String(toInt(o.month) ?? 0).padStart(2, '0');
  const d = String(toInt(o.day) ?? 0).padStart(2, '0');
  const hh = String(toInt(o.hour) ?? 0).padStart(2, '0');
  const mm = String(toInt(o.minute) ?? 0).padStart(2, '0');
  const ss = String(Math.floor(toInt(o.second) ?? 0)).padStart(2, '0');
  return `${y}-${m}-${d}T${hh}:${mm}:${ss}Z`;
}

export async function GET() {
  try {
    const rows = await readQuery<Row>(
      `MATCH (r:IngestRun {key: 'latest'})
       RETURN r.at AS at, r.files AS files, r.mentions AS mentions,
              r.refers AS refers, r.duration_ms AS duration_ms,
              r.error AS error`,
    );
    if (!rows.length) {
      return NextResponse.json({ configured: true, run: null });
    }
    const r = rows[0];
    return NextResponse.json({
      configured: true,
      run: {
        at: atToIso(r.at),
        files: toInt(r.files),
        mentions: toInt(r.mentions),
        refers: toInt(r.refers),
        duration_ms: toInt(r.duration_ms),
        error: r.error,
      },
    });
  } catch (e) {
    return NextResponse.json(
      { configured: false, run: null, error: (e as Error).message },
      { status: 200 },
    );
  }
}
