'use client';

import { useEffect, useState } from 'react';

type Run = {
  at: string | null;
  files: number | null;
  mentions: number | null;
  refers: number | null;
  duration_ms: number | null;
  error: string | null;
};
type Status = { configured: boolean; run: Run | null; error?: string };

function relTime(iso: string | null): string {
  if (!iso) return '—';
  const delta = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (delta < 60) return `${Math.floor(delta)}s ago`;
  if (delta < 3600) return `${Math.floor(delta / 60)}m ago`;
  if (delta < 86400) return `${Math.floor(delta / 3600)}h ago`;
  return `${Math.floor(delta / 86400)}d ago`;
}

// Traffic-light colour: green <2 min, amber 2-10 min, red >10 min OR
// an ingest error. Nothing is "critical" on a personal graph, but a
// 30-minute-silent ingester usually means the loop died.
function tone(run: Run | null): 'ok' | 'warn' | 'err' {
  if (!run) return 'warn';
  if (run.error) return 'err';
  if (!run.at) return 'warn';
  const delta = (Date.now() - new Date(run.at).getTime()) / 1000;
  if (delta < 120) return 'ok';
  if (delta < 600) return 'warn';
  return 'err';
}

export default function IngestStatus() {
  const [status, setStatus] = useState<Status | null>(null);
  const [, setTick] = useState(0);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const r = await fetch('/api/status', { cache: 'no-store' });
        const j = (await r.json()) as Status;
        if (!cancelled) setStatus(j);
      } catch {
        /* keep previous */
      }
    };
    load();
    const t = setInterval(load, 30_000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, []);

  // Recompute relTime every 10s so "42s ago" stays correct without
  // re-fetching the status.
  useEffect(() => {
    const t = setInterval(() => setTick((n) => n + 1), 10_000);
    return () => clearInterval(t);
  }, []);

  const t = tone(status?.run ?? null);
  return (
    <div
      className="ingest-status"
      title={
        status?.run?.error
          ? `ingest error: ${status.run.error}`
          : status?.run
          ? `${status.run.files ?? 0} files · ${status.run.mentions ?? 0}+${status.run.refers ?? 0} edges · ${
              status.run.duration_ms ?? 0
            }ms`
          : 'no ingest run recorded yet'
      }
    >
      <span className={`ingest-dot tone-${t}`} />
      <span className="ingest-label">Ingest</span>
      <span className="ingest-value">{relTime(status?.run?.at ?? null)}</span>
    </div>
  );
}
