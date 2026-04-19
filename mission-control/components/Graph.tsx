'use client';

import dynamic from 'next/dynamic';
import { useRouter, useSearchParams } from 'next/navigation';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import * as THREE from 'three';
import { colorFor, LABEL_COLOR, LABEL_ORDER } from '@/lib/labels';
import { communityColor, computeCommunities, COMMUNITY_COLORS } from '@/lib/clustering';
import DetailPanel from './DetailPanel';

const ForceGraph2D = dynamic(
  () => import('react-force-graph-2d').then((m) => m.default),
  { ssr: false },
) as any;
const ForceGraph3D = dynamic(
  () => import('react-force-graph-3d').then((m) => m.default),
  { ssr: false },
) as any;

type Node = {
  id: number;
  name: string;
  label: string;
  degree: number;
  x?: number;
  y?: number;
  z?: number;
};
type LinkType = 'MENTIONS' | 'REFERS_TO';
type Link = {
  source: number | Node;
  target: number | Node;
  type: LinkType;
  via: string | null;
};
type GraphData = { nodes: Node[]; links: Link[] };

const EDGE_BASE: Record<LinkType, string> = {
  MENTIONS: 'rgba(180, 180, 180, 0.08)',
  REFERS_TO: 'rgba(163, 177, 138, 0.18)',
};
const EDGE_HIT: Record<LinkType, string> = {
  MENTIONS: 'rgba(251, 146, 60, 0.6)',
  REFERS_TO: 'rgba(251, 146, 60, 0.5)',
};
const EDGE_DIM: Record<LinkType, string> = {
  MENTIONS: 'rgba(180, 180, 180, 0.02)',
  REFERS_TO: 'rgba(163, 177, 138, 0.04)',
};

const linkEnds = (l: Link): [number, number] => [
  typeof l.source === 'number' ? l.source : l.source.id,
  typeof l.target === 'number' ? l.target : l.target.id,
];

export default function Graph() {
  const router = useRouter();
  const params = useSearchParams();
  const selectedId = useMemo(() => {
    const v = params.get('n');
    return v ? Number.parseInt(v, 10) : null;
  }, [params]);
  const is3D = params.get('mode') === '3d';
  const colorMode = params.get('color') === 'community' ? 'community' : 'label';

  const [data, setData] = useState<GraphData | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [hover, setHover] = useState<Node | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const fgRef = useRef<any>(null);
  const [size, setSize] = useState<{ w: number; h: number }>({ w: 0, h: 0 });
  const idleTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

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

  // Louvain communities keyed by node id. Recomputed only when the
  // graph topology changes.
  const communities = useMemo(() => {
    if (!data) return null;
    return computeCommunities(data.nodes, data.links);
  }, [data]);

  // Community sizes in rank order for the legend.
  const communityStats = useMemo(() => {
    if (!communities) return [] as { idx: number; n: number }[];
    const sizes = new Map<number, number>();
    for (const c of communities.values()) sizes.set(c, (sizes.get(c) ?? 0) + 1);
    return [...sizes.entries()]
      .sort((a, b) => a[0] - b[0])
      .slice(0, COMMUNITY_COLORS.length)
      .map(([idx, n]) => ({ idx, n }));
  }, [communities]);

  const edgeCounts = useMemo(() => {
    if (!data) return { mentions: 0, refers: 0 };
    let m = 0;
    let r = 0;
    for (const l of data.links) {
      if (l.type === 'REFERS_TO') r++;
      else m++;
    }
    return { mentions: m, refers: r };
  }, [data]);

  // Pre-compute the selected node's neighbour set so every frame's colour
  // callbacks can check membership in O(1). Recomputes only when selection
  // or data changes.
  const neighbourSet = useMemo(() => {
    if (selectedId == null || !data) return null;
    const s = new Set<number>([selectedId]);
    for (const l of data.links) {
      const [sid, tid] = linkEnds(l);
      if (sid === selectedId) s.add(tid);
      if (tid === selectedId) s.add(sid);
    }
    return s;
  }, [selectedId, data]);

  const setSelection = useCallback(
    (id: number | null) => {
      const q = new URLSearchParams(params.toString());
      if (id == null) q.delete('n');
      else q.set('n', String(id));
      router.replace(q.toString() ? `?${q.toString()}` : '/', { scroll: false });
    },
    [params, router],
  );

  const toggleMode = () => {
    const q = new URLSearchParams(params.toString());
    if (is3D) q.delete('mode');
    else q.set('mode', '3d');
    router.replace(q.toString() ? `?${q.toString()}` : '/', { scroll: false });
  };

  const toggleColorMode = () => {
    const q = new URLSearchParams(params.toString());
    if (colorMode === 'community') q.delete('color');
    else q.set('color', 'community');
    router.replace(q.toString() ? `?${q.toString()}` : '/', { scroll: false });
  };

  // Camera fly-to on selection.
  useEffect(() => {
    if (!fgRef.current || selectedId == null || !data) return;
    const target = data.nodes.find((n) => n.id === selectedId);
    if (!target) return;
    if (is3D) {
      if (target.x == null || target.y == null || target.z == null) return;
      const dist = 80;
      const r = Math.hypot(target.x, target.y, target.z) || 1;
      fgRef.current.cameraPosition(
        {
          x: target.x * (1 + dist / r),
          y: target.y * (1 + dist / r),
          z: target.z * (1 + dist / r),
        },
        target,
        800,
      );
    } else {
      if (target.x == null || target.y == null) return;
      fgRef.current.centerAt(target.x, target.y, 600);
      fgRef.current.zoom(2.8, 600);
    }
  }, [selectedId, data, is3D]);

  // 3D-only cinematic stack:
  //  - UnrealBloomPass (core glow, existing)
  //  - FilmPass (grain + faint scanline, analog feel)
  //  - Custom ChromaticAberrationShader (tiny color fringe at screen edges)
  //  - Custom VignetteShader (cinematic edge darkening)
  //  - Auto-rotate controls with "breathing" idle motion on top
  //  - Idle pause: any pointer/key input pauses auto-rotate + breathing for 30s
  // All passes are wrapped in try/catch — a shader compile failure on older
  // GPUs silently skips that one effect without breaking the whole graph.
  useEffect(() => {
    if (!is3D || !fgRef.current || !data || size.w === 0) return;
    let cancelled = false;
    const addedPasses: any[] = [];
    let ctlRef: any = null;
    let breathRafId: number | null = null;
    let userInteracted = false;
    let breathPauseUntil = 0;

    const attach = async () => {
      const three = await import('three');
      const { UnrealBloomPass } = await import(
        'three/examples/jsm/postprocessing/UnrealBloomPass.js'
      );
      const { FilmPass } = await import(
        'three/examples/jsm/postprocessing/FilmPass.js'
      );
      const { ShaderPass } = await import(
        'three/examples/jsm/postprocessing/ShaderPass.js'
      );
      if (cancelled || !fgRef.current) return () => {};
      const composer = fgRef.current.postProcessingComposer?.();
      if (!composer) return () => {};

      const safeAdd = (label: string, fn: () => any) => {
        try {
          const pass = fn();
          if (pass) {
            composer.addPass(pass);
            addedPasses.push(pass);
          }
        } catch (e) {
          console.warn(`[graph fx] ${label} skipped:`, e);
        }
      };

      safeAdd('bloom', () => new UnrealBloomPass(
        new three.Vector2(size.w, size.h),
        1.15, 0.72, 0.82,
      ));

      // Film grain + very faint scanlines. The constructor signature varies
      // between three versions; pass intensity as first arg, rest default.
      safeAdd('film', () => {
        const fp: any = new (FilmPass as any)(
          0.22,  // noise intensity
          0.06,  // scanline intensity (very subtle)
          2048,  // scanline count
          false, // grayscale
        );
        return fp;
      });

      // Chromatic aberration — cheap GLSL, subtle color fringe near the edges
      safeAdd('chromaticAberration', () => {
        const shader = {
          uniforms: {
            tDiffuse: { value: null },
            uOffset: { value: 0.0022 },
          },
          vertexShader: `
            varying vec2 vUv;
            void main() {
              vUv = uv;
              gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
            }
          `,
          fragmentShader: `
            uniform sampler2D tDiffuse;
            uniform float uOffset;
            varying vec2 vUv;
            void main() {
              vec2 dir = vUv - vec2(0.5);
              float falloff = smoothstep(0.2, 0.9, length(dir));
              float off = uOffset * falloff;
              float r = texture2D(tDiffuse, vUv + dir * off).r;
              float g = texture2D(tDiffuse, vUv).g;
              float b = texture2D(tDiffuse, vUv - dir * off).b;
              gl_FragColor = vec4(r, g, b, 1.0);
            }
          `,
        };
        return new (ShaderPass as any)(shader);
      });

      // Vignette — cinematic edge darkening
      safeAdd('vignette', () => {
        const shader = {
          uniforms: {
            tDiffuse: { value: null },
            uDarkness: { value: 0.85 },
            uOffset:   { value: 0.95 },
          },
          vertexShader: `
            varying vec2 vUv;
            void main() {
              vUv = uv;
              gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
            }
          `,
          fragmentShader: `
            uniform sampler2D tDiffuse;
            uniform float uDarkness;
            uniform float uOffset;
            varying vec2 vUv;
            void main() {
              vec4 c = texture2D(tDiffuse, vUv);
              vec2 p = vUv - vec2(0.5);
              float d = length(p);
              float v = smoothstep(uOffset, uOffset - 0.7, d);
              c.rgb *= mix(1.0 - uDarkness, 1.0, v);
              gl_FragColor = c;
            }
          `,
        };
        return new (ShaderPass as any)(shader);
      });

      // Auto-rotate idle camera plus breathing motion on top.
      const controls = fgRef.current.controls();
      if (!controls) return () => {};
      controls.autoRotate = true;
      controls.autoRotateSpeed = 0.26;
      ctlRef = controls;

      // Breathing: gently drift controls.target on a slow Y+Z sine so the
      // camera feels alive even when the user isn't orbiting. Adds ~0.6
      // units of movement - enough to notice, not enough to disorient.
      const baseTarget = controls.target.clone();
      const t0 = performance.now();
      const tick = (now: number) => {
        if (cancelled || !ctlRef) return;
        const t = (now - t0) / 1000;
        // Pause breathing while user is interacting or within idle grace period.
        const paused = now < breathPauseUntil;
        if (!paused) {
          ctlRef.target.set(
            baseTarget.x,
            baseTarget.y + Math.sin(t * 0.35) * 0.6,
            baseTarget.z + Math.sin(t * 0.21 + 1.2) * 0.4,
          );
          ctlRef.update();
        }
        breathRafId = requestAnimationFrame(tick);
      };
      breathRafId = requestAnimationFrame(tick);

      const bump = () => {
        if (!ctlRef) return;
        userInteracted = true;
        ctlRef.autoRotate = false;
        breathPauseUntil = performance.now() + 30_000;
        if (idleTimer.current) clearTimeout(idleTimer.current);
        idleTimer.current = setTimeout(() => {
          if (!ctlRef) return;
          ctlRef.autoRotate = true;
          userInteracted = false;
        }, 30_000);
      };
      const target = wrapRef.current;
      target?.addEventListener('pointermove', bump);
      target?.addEventListener('pointerdown', bump);
      window.addEventListener('keydown', bump);
      return () => {
        target?.removeEventListener('pointermove', bump);
        target?.removeEventListener('pointerdown', bump);
        window.removeEventListener('keydown', bump);
      };
    };

    const cleanup = attach();
    return () => {
      cancelled = true;
      if (idleTimer.current) clearTimeout(idleTimer.current);
      if (breathRafId != null) cancelAnimationFrame(breathRafId);
      if (ctlRef) ctlRef.autoRotate = false;
      cleanup.then((fn) => fn && fn()).catch(() => {});
      if (fgRef.current && addedPasses.length) {
        try {
          const composer = fgRef.current.postProcessingComposer?.();
          for (const p of addedPasses) composer?.removePass?.(p);
        } catch {
          /* ignore */
        }
      }
    };
  }, [is3D, data, size.w, size.h]);

  const linkColor = (l: Link) => {
    const [sid, tid] = linkEnds(l);
    if (neighbourSet) {
      if (sid === selectedId || tid === selectedId) return EDGE_HIT[l.type];
      return EDGE_DIM[l.type];
    }
    if (hover && (sid === hover.id || tid === hover.id)) {
      return 'rgba(251, 146, 60, 0.35)';
    }
    return EDGE_BASE[l.type];
  };

  const linkWidth = (l: Link) => {
    const [sid, tid] = linkEnds(l);
    if (neighbourSet && (sid === selectedId || tid === selectedId)) return 1.4;
    if (hover && (sid === hover.id || tid === hover.id)) return 1;
    return 0.55;
  };

  // Directional particles only on edges touching hover OR selection.
  // Keeps idle graph calm; hover/select makes it feel alive.
  const particleCount = (l: Link) => {
    const [sid, tid] = linkEnds(l);
    const hitSelected =
      selectedId != null && (sid === selectedId || tid === selectedId);
    const hitHover = hover != null && (sid === hover.id || tid === hover.id);
    return hitSelected || hitHover ? 2 : 0;
  };

  const nodeOpacity3D = (n: Node) => {
    if (!neighbourSet) return 0.85;
    return neighbourSet.has(n.id) ? 1 : 0.15;
  };

  const nodeColor = (n: Node) => {
    if (selectedId === n.id) return '#fb923c';
    const base =
      colorMode === 'community'
        ? communityColor(communities?.get(n.id))
        : colorFor(n.label);
    if (neighbourSet && !neighbourSet.has(n.id)) {
      return base.length === 7 ? base + '26' : base; // 0.15 alpha
    }
    return base;
  };

  return (
    <div className="graph-wrap" ref={wrapRef}>
      {err && <div className="graph-err">{err}</div>}
      {data && size.w > 0 && !is3D && (
        <ForceGraph2D
          ref={fgRef}
          graphData={data}
          width={size.w}
          height={size.h}
          backgroundColor="#0a0a0a"
          nodeRelSize={3}
          nodeVal={(n: Node) => 1 + Math.min(n.degree, 20) * 0.5}
          nodeLabel={(n: Node) => `${n.name} · ${n.label}`}
          nodeColor={nodeColor}
          linkColor={linkColor}
          linkWidth={linkWidth}
          linkDirectionalParticles={particleCount}
          linkDirectionalParticleWidth={0.6}
          linkDirectionalParticleSpeed={0.004}
          linkDirectionalParticleColor={() => '#fde68a'}
          cooldownTicks={100}
          d3VelocityDecay={0.3}
          onNodeHover={(n: Node | null) => setHover(n)}
          onNodeClick={(n: Node) => setSelection(n.id)}
          onBackgroundClick={() => setSelection(null)}
          nodeCanvasObjectMode={() => 'after'}
          nodeCanvasObject={(n: Node, ctx: CanvasRenderingContext2D, scale: number) => {
            const isSelected = selectedId === n.id;
            const isNeighbour = neighbourSet && neighbourSet.has(n.id);
            const show = isSelected || (isNeighbour && neighbourSet) || n.degree >= 6 || scale > 2.4;
            if (!show) return;
            const r = 3 + Math.min(n.degree, 20) * 0.5;
            const fontSize = Math.max(9, 11 / scale);
            ctx.font = `${isSelected ? 600 : 400} ${fontSize}px "Space Grotesk", ui-sans-serif`;
            ctx.textAlign = 'left';
            ctx.textBaseline = 'middle';
            const alpha = neighbourSet && !isNeighbour ? 0.22 : 0.78;
            ctx.fillStyle = isSelected
              ? 'rgba(251, 146, 60, 1)'
              : `rgba(230, 230, 230, ${alpha})`;
            ctx.fillText(n.name, (n.x ?? 0) + r + 3, n.y ?? 0);
          }}
        />
      )}
      {data && size.w > 0 && is3D && (
        <ForceGraph3D
          ref={fgRef}
          graphData={data}
          width={size.w}
          height={size.h}
          backgroundColor="#0a0a0a"
          nodeRelSize={4}
          nodeVal={(n: Node) => 1 + Math.min(n.degree, 20) * 0.6}
          nodeLabel={(n: Node) => `${n.name} · ${n.label}`}
          nodeColor={(n: Node) => {
            if (selectedId === n.id) return '#fb923c';
            return colorMode === 'community'
              ? communityColor(communities?.get(n.id))
              : colorFor(n.label);
          }}
          nodeOpacity={nodeOpacity3D as any}
          nodeThreeObjectExtend={true}
          nodeThreeObject={(n: Node) => {
            // Extend the default sphere mesh with a halo ring for big hubs
            // (Buero Birnbaum, RA Ostendorf, EstateMate, etc.). degree=25
            // threshold ~matches top ~8-12 hub nodes in the graph. Hermes's
            // force-graph-3d default node is a sphere sized by nodeRelSize;
            // we only add a ring on top via nodeThreeObjectExtend=true.
            if (n.degree < 25) return null;
            const radius = 3 + Math.min(n.degree, 40) * 0.45;
            const geom = new THREE.TorusGeometry(radius * 1.35, 0.18, 12, 48);
            const mat = new THREE.MeshBasicMaterial({
              color: selectedId === n.id ? 0xfb923c : 0xfde68a,
              transparent: true,
              opacity: 0.32,
            });
            const ring = new THREE.Mesh(geom, mat);
            ring.rotation.x = Math.PI / 2;
            (ring as any).userData.isHubHalo = true;
            return ring;
          }}
          linkColor={linkColor}
          linkWidth={linkWidth}
          linkOpacity={0.55}
          linkDirectionalParticles={particleCount}
          linkDirectionalParticleWidth={0.8}
          linkDirectionalParticleSpeed={0.004}
          linkDirectionalParticleColor={() => '#fde68a'}
          cooldownTicks={120}
          onNodeClick={(n: Node) => setSelection(n.id)}
          onBackgroundClick={() => setSelection(null)}
          onNodeHover={(n: Node | null) => setHover(n)}
        />
      )}
      <div className="legend">
        {colorMode === 'label' ? (
          <>
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
          </>
        ) : (
          <>
            <div className="legend-title">Communities</div>
            {communityStats.map(({ idx, n }) => (
              <div className="legend-row" key={idx}>
                <span
                  className="legend-dot"
                  style={{ background: communityColor(idx) }}
                />
                <span className="legend-label">Cluster {idx + 1}</span>
                <span className="legend-n">{n}</span>
              </div>
            ))}
          </>
        )}
        <div className="legend-divider" />
        <div className="legend-title">Edges</div>
        <div className="legend-row">
          <span className="legend-edge mentions" />
          <span className="legend-label">Mentions</span>
          <span className="legend-n">{edgeCounts.mentions}</span>
        </div>
        <div className="legend-row">
          <span className="legend-edge refers" />
          <span className="legend-label">Refers</span>
          <span className="legend-n">{edgeCounts.refers}</span>
        </div>
      </div>
      <div className="mode-stack">
        <button className="mode-toggle" onClick={toggleColorMode} title="Colour by">
          <span className={`mode-segment${colorMode === 'label' ? ' active' : ''}`}>
            Label
          </span>
          <span className={`mode-segment${colorMode === 'community' ? ' active' : ''}`}>
            Cluster
          </span>
        </button>
        <button className="mode-toggle" onClick={toggleMode} title="Render mode">
          <span className={`mode-segment${!is3D ? ' active' : ''}`}>2D</span>
          <span className={`mode-segment${is3D ? ' active' : ''}`}>3D</span>
        </button>
      </div>
      {hover && selectedId == null && (
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
      <DetailPanel
        nodeId={selectedId}
        onClose={() => setSelection(null)}
        onNavigate={(id) => setSelection(id)}
      />
    </div>
  );
}
