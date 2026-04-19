'use client';

import dynamic from 'next/dynamic';
import { useEffect, useMemo, useRef, useState } from 'react';
import { colorFor, LABEL_ORDER, LABEL_COLOR } from '@/lib/labels';

// react-force-graph-2d uses window/canvas, so must be client-side and
// ssr-disabled. Wrapped once here and reused by the page.
const ForceGraph2D = dynamic(
  () => import('react-force-graph-2d').then((m) => m.default),
  { ssr: false },
) as any;

type Node = { id: number; name: string; label: string; degree: number };
type Link = { source: number; target: number };
type GraphData = { nodes: Node[]; links: Link[] };

export default function Graph() {
  const [data, setData] = useState<GraphData | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [hover, setHover] = useState<Node | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const [size, setSize] = useState<{ w: number; h: number }>({ w: 0, h: 0 });

  // Fetch + refresh on a slow cadence. The graph ingests every 120s so
  // no point polling faster than that.
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const r = await fetch('/api/graph', { cache: 'no-store' });
        if (!r.ok) throw new Error(`API ${r.status}`);
        const j = (await r.json()) as GraphData;
        if (!cancelled) setData(j);
      } catch (e) {
        if (!cancelled) setErr((e as Error).message);
      }
    };
    load();
    const t = setInterval(load, 120_000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, []);

  // Size the canvas to its container. ForceGraph2D needs explicit w/h
  // props; it doesn't flex. ResizeObserver handles window resizes.
  useEffect(() => {
    if (!wrapRef.current) return;
    const el = wrapRef.current;
    const ro = new ResizeObserver(() => {
      setSize({ w: el.clientWidth, h: el.clientHeight });
    });
    ro.observe(el);
    setSize({ w: el.clientWidth, h: el.clientHeight });
    return () => ro.disconnect();
  }, []);

  const labelCounts = useMemo(() => {
    if (!data) return [] as { label: string; n: number }[];
    const m = new Map<string, number>();
    for (const n of data.nodes) m.set(n.label, (m.get(n.label) ?? 0) + 1);
    return LABEL_ORDER.filter((l) => m.has(l)).map((l) => ({
      label: l,
      n: m.get(l)!,
    }));
  }, [data]);

  return (
    <div className="graph-wrap" ref={wrapRef}>
      {err && <div className="graph-err">{err}</div>}
      {data && size.w > 0 && (
        <ForceGraph2D
          graphData={data}
          width={size.w}
          height={size.h}
          backgroundColor="#0a0a0a"
          nodeRelSize={3}
          nodeVal={(n: Node) => 1 + Math.min(n.degree, 20) * 0.5}
          nodeLabel={(n: Node) => `${n.name} · ${n.label}`}
          nodeColor={(n: Node) => colorFor(n.label)}
          linkColor={() => 'rgba(180, 180, 180, 0.08)'}
          linkWidth={0.6}
          linkDirectionalParticles={0}
          cooldownTicks={100}
          d3VelocityDecay={0.3}
          onNodeHover={(n: Node | null) => setHover(n)}
          nodeCanvasObjectMode={() => 'after'}
          nodeCanvasObject={(n: Node, ctx: CanvasRenderingContext2D, scale: number) => {
            // Only draw labels for high-degree nodes or when zoomed in.
            const show = n.degree >= 6 || scale > 2.4;
            if (!show) return;
            const r = 3 + Math.min(n.degree, 20) * 0.5;
            const fontSize = Math.max(9, 11 / scale);
            ctx.font = `${fontSize}px "Space Grotesk", ui-sans-serif`;
            ctx.textAlign = 'left';
            ctx.textBaseline = 'middle';
            ctx.fillStyle = 'rgba(230, 230, 230, 0.72)';
            ctx.fillText(n.name, (n as any).x + r + 3, (n as any).y);
          }}
        />
      )}
      <div className="legend">
        <div className="legend-title">Entities</div>
        {labelCounts.map(({ label, n }) => (
          <div className="legend-row" key={label}>
            <span
              className="legend-dot"
              style={{ background: LABEL_COLOR[label] ?? '#3a3a3a' }}
            />
            <span className="legend-label">{label}</span>
            <span className="legend-n">{n}</span>
          </div>
        ))}
      </div>
      {hover && (
        <div className="hover-card">
          <div className="hover-name">{hover.name}</div>
          <div className="hover-meta">
            <span
              className="hover-dot"
              style={{ background: colorFor(hover.label) }}
            />
            {hover.label} · {hover.degree} {hover.degree === 1 ? 'edge' : 'edges'}
          </div>
        </div>
      )}
    </div>
  );
}
