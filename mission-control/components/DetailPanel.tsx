'use client';

import { useEffect, useState } from 'react';
import { colorFor } from '@/lib/labels';

type Neighbour = {
  id: number;
  name: string;
  label: string;
  direction: 'in' | 'out';
};

type NodeDetail = {
  id: number;
  name: string;
  label: string;
  degree: number;
  props: Record<string, unknown>;
  neighbours: Neighbour[];
};

// Props to hide from the "Properties" list - path/folder are plumbing,
// not signal; name is the header.
const HIDE_PROPS = new Set(['path', 'folder', 'name']);

function formatValue(v: unknown): string {
  if (v == null) return '';
  if (Array.isArray(v)) return v.join(', ');
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}

export default function DetailPanel({
  nodeId,
  onClose,
  onNavigate,
}: {
  nodeId: number | null;
  onClose: () => void;
  onNavigate: (id: number) => void;
}) {
  const [data, setData] = useState<NodeDetail | null>(null);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    if (nodeId == null) {
      setData(null);
      setErr(null);
      return;
    }
    let cancelled = false;
    setLoading(true);
    setErr(null);
    fetch(`/api/node/${nodeId}`, { cache: 'no-store' })
      .then(async (r) => {
        if (!r.ok) throw new Error(`API ${r.status}`);
        return (await r.json()) as NodeDetail;
      })
      .then((j) => {
        if (!cancelled) setData(j);
      })
      .catch((e) => {
        if (!cancelled) setErr((e as Error).message);
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [nodeId]);

  if (nodeId == null) return null;

  const propEntries = data
    ? Object.entries(data.props).filter(([k]) => !HIDE_PROPS.has(k))
    : [];

  const outNeighbours = data?.neighbours.filter((n) => n.direction === 'out') ?? [];
  const inNeighbours = data?.neighbours.filter((n) => n.direction === 'in') ?? [];

  return (
    <aside className="panel">
      <button
        className="panel-close"
        aria-label="Close"
        onClick={onClose}
      >
        ×
      </button>
      {loading && <div className="panel-loading">loading</div>}
      {err && <div className="panel-err">{err}</div>}
      {data && (
        <>
          <header className="panel-head">
            <div className="panel-meta">
              <span
                className="panel-dot"
                style={{ background: colorFor(data.label) }}
              />
              <span className="panel-label-name">{data.label}</span>
              <span className="panel-sep">·</span>
              <span className="panel-degree">
                {data.degree} {data.degree === 1 ? 'edge' : 'edges'}
              </span>
            </div>
            <h2 className="panel-name">{data.name}</h2>
          </header>

          {propEntries.length > 0 && (
            <section className="panel-section">
              <h3 className="panel-section-title">Properties</h3>
              <dl className="panel-props">
                {propEntries.map(([k, v]) => (
                  <div className="panel-prop-row" key={k}>
                    <dt>{k}</dt>
                    <dd>{formatValue(v)}</dd>
                  </div>
                ))}
              </dl>
            </section>
          )}

          {outNeighbours.length > 0 && (
            <section className="panel-section">
              <h3 className="panel-section-title">
                Mentions <span className="panel-section-n">{outNeighbours.length}</span>
              </h3>
              <div className="chip-list">
                {outNeighbours.map((n) => (
                  <button
                    key={n.id}
                    className="chip"
                    onClick={() => onNavigate(n.id)}
                    title={`${n.name} · ${n.label}`}
                  >
                    <span
                      className="chip-dot"
                      style={{ background: colorFor(n.label) }}
                    />
                    {n.name}
                  </button>
                ))}
              </div>
            </section>
          )}

          {inNeighbours.length > 0 && (
            <section className="panel-section">
              <h3 className="panel-section-title">
                Mentioned by <span className="panel-section-n">{inNeighbours.length}</span>
              </h3>
              <div className="chip-list">
                {inNeighbours.map((n) => (
                  <button
                    key={n.id}
                    className="chip"
                    onClick={() => onNavigate(n.id)}
                    title={`${n.name} · ${n.label}`}
                  >
                    <span
                      className="chip-dot"
                      style={{ background: colorFor(n.label) }}
                    />
                    {n.name}
                  </button>
                ))}
              </div>
            </section>
          )}
        </>
      )}
    </aside>
  );
}
