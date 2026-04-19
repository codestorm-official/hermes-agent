'use client';

import { Command } from 'cmdk';
import { useRouter, useSearchParams } from 'next/navigation';
import { useEffect, useMemo, useState } from 'react';
import { colorFor, LABEL_ORDER } from '@/lib/labels';

type Node = { id: number; name: string; label: string; degree: number };
type GraphPayload = { nodes: Node[] };

export default function CommandPalette() {
  const [open, setOpen] = useState(false);
  const [nodes, setNodes] = useState<Node[]>([]);
  const router = useRouter();
  const params = useSearchParams();

  // Cmd+K / Ctrl+K to toggle.
  useEffect(() => {
    const h = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault();
        setOpen((o) => !o);
      }
    };
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  }, []);

  // Lazy-load nodes on first open; reuse the graph endpoint.
  useEffect(() => {
    if (!open || nodes.length) return;
    fetch('/api/graph', { cache: 'no-store' })
      .then((r) => r.json() as Promise<GraphPayload>)
      .then((j) => setNodes(j.nodes))
      .catch(() => {});
  }, [open, nodes.length]);

  // Group nodes by label using LABEL_ORDER, drop Stubs to the bottom.
  const grouped = useMemo(() => {
    const by: Map<string, Node[]> = new Map();
    for (const n of nodes) {
      if (!by.has(n.label)) by.set(n.label, []);
      by.get(n.label)!.push(n);
    }
    // highest-degree first within a label.
    for (const list of by.values()) list.sort((a, b) => b.degree - a.degree);
    const ordered: [string, Node[]][] = [];
    for (const l of LABEL_ORDER) if (by.has(l)) ordered.push([l, by.get(l)!]);
    for (const [l, list] of by) if (!LABEL_ORDER.includes(l)) ordered.push([l, list]);
    return ordered;
  }, [nodes]);

  const is3D = params.get('mode') === '3d';

  const go = (q: URLSearchParams) => {
    router.replace(q.toString() ? `?${q.toString()}` : '/', { scroll: false });
    setOpen(false);
  };

  const selectNode = (id: number) => {
    const q = new URLSearchParams(params.toString());
    q.set('n', String(id));
    go(q);
  };

  const toggleMode = () => {
    const q = new URLSearchParams(params.toString());
    if (is3D) q.delete('mode');
    else q.set('mode', '3d');
    go(q);
  };

  const clearSelection = () => {
    const q = new URLSearchParams(params.toString());
    q.delete('n');
    go(q);
  };

  if (!open) return null;

  return (
    <div className="cmdk-root" onClick={() => setOpen(false)}>
      <div className="cmdk-wrap" onClick={(e) => e.stopPropagation()}>
        <Command shouldFilter={true} label="Command Menu">
          <Command.Input
            placeholder="search entities or run command…"
            className="cmdk-input"
            autoFocus
          />
          <Command.List className="cmdk-list">
            <Command.Empty className="cmdk-empty">no match</Command.Empty>

            <Command.Group heading="Actions" className="cmdk-group">
              <Command.Item
                className="cmdk-item"
                onSelect={() => {
                  setOpen(false);
                  window.dispatchEvent(new Event('hermes:ask-open'));
                }}
                value="ask a question"
              >
                <span className="cmdk-kicker">ask</span>
                <span>natural-language query</span>
              </Command.Item>
              <Command.Item
                className="cmdk-item"
                onSelect={toggleMode}
                value={`switch to ${is3D ? '2d' : '3d'}`}
              >
                <span className="cmdk-kicker">switch</span>
                <span>to {is3D ? '2D' : '3D'} view</span>
              </Command.Item>
              <Command.Item
                className="cmdk-item"
                onSelect={clearSelection}
                value="clear selection"
              >
                <span className="cmdk-kicker">clear</span>
                <span>node selection</span>
              </Command.Item>
            </Command.Group>

            {grouped.map(([label, list]) => (
              <Command.Group heading={label} key={label} className="cmdk-group">
                {list.map((n) => (
                  <Command.Item
                    key={n.id}
                    className="cmdk-item"
                    onSelect={() => selectNode(n.id)}
                    value={`${n.name} ${label}`}
                  >
                    <span
                      className="cmdk-dot"
                      style={{ background: colorFor(n.label) }}
                    />
                    <span className="cmdk-name">{n.name}</span>
                    <span className="cmdk-meta">
                      {n.degree} {n.degree === 1 ? 'edge' : 'edges'}
                    </span>
                  </Command.Item>
                ))}
              </Command.Group>
            ))}
          </Command.List>
          <div className="cmdk-foot">
            <span>
              <kbd>↑↓</kbd> navigate
            </span>
            <span>
              <kbd>↵</kbd> select
            </span>
            <span>
              <kbd>esc</kbd> close
            </span>
          </div>
        </Command>
      </div>
    </div>
  );
}
