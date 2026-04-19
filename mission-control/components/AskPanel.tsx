'use client';

import { useEffect, useRef, useState } from 'react';

type AskResp = {
  configured: boolean;
  question?: string;
  cypher?: string;
  rows?: unknown[];
  raw?: string;
  error?: string;
};

export default function AskPanel({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const [q, setQ] = useState('');
  const [loading, setLoading] = useState(false);
  const [resp, setResp] = useState<AskResp | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (open) {
      setResp(null);
      setQ('');
      setTimeout(() => inputRef.current?.focus(), 40);
    }
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const h = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', h);
    return () => window.removeEventListener('keydown', h);
  }, [open, onClose]);

  const submit = async () => {
    if (!q.trim() || loading) return;
    setLoading(true);
    setResp(null);
    try {
      const r = await fetch('/api/ask', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ question: q }),
      });
      const j = (await r.json()) as AskResp;
      setResp(j);
    } catch (e) {
      setResp({ configured: true, error: (e as Error).message });
    } finally {
      setLoading(false);
    }
  };

  if (!open) return null;

  const rows = resp?.rows ?? [];
  const cols = rows.length
    ? Object.keys(rows[0] as Record<string, unknown>)
    : [];

  return (
    <div className="cmdk-root" onClick={onClose}>
      <div
        className="ask-wrap"
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            submit();
          }
        }}
      >
        <div className="ask-head">
          <span className="ask-kicker">ask</span>
          <input
            ref={inputRef}
            className="ask-input"
            placeholder="welche Properties gehoeren der GmbH X…"
            value={q}
            onChange={(e) => setQ(e.target.value)}
          />
          <button
            className="ask-send"
            onClick={submit}
            disabled={loading || !q.trim()}
          >
            {loading ? '…' : 'Go'}
          </button>
        </div>

        {resp && (
          <div className="ask-body">
            {resp.configured === false && (
              <div className="ask-setup">
                set <code>ANTHROPIC_API_KEY</code> on hermes-mission-control to enable
              </div>
            )}
            {resp.cypher && (
              <pre className="ask-cypher" title="generated cypher">
                <code>{resp.cypher}</code>
              </pre>
            )}
            {resp.error && <div className="ask-err">{resp.error}</div>}
            {rows.length > 0 && (
              <div className="ask-table-wrap">
                <table className="ask-table">
                  <thead>
                    <tr>
                      {cols.map((c) => (
                        <th key={c}>{c}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {rows.slice(0, 50).map((row, i) => (
                      <tr key={i}>
                        {cols.map((c) => {
                          const v = (row as Record<string, unknown>)[c];
                          return (
                            <td key={c}>
                              {v == null
                                ? '—'
                                : typeof v === 'object'
                                ? JSON.stringify(v)
                                : String(v)}
                            </td>
                          );
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
                {rows.length > 50 && (
                  <div className="ask-more">+{rows.length - 50} more rows</div>
                )}
              </div>
            )}
            {rows.length === 0 && resp.cypher && !resp.error && (
              <div className="ask-empty">no rows</div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
