'use client';

import { useEffect, useMemo, useState } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';

type Task = {
  id: number;
  name: string;
  path: string | null;
  status: string;
  priority: string;
  deadline: string | null;
  assignee: string | null;
  area: string | null;
  type: string | null;
  degree: number;
};

type Feed = { tasks: Task[]; error?: string };

// "2026-04-20" -> {label: "tomorrow", overdue: false, today: false, days: 1}
function parseDeadline(iso: string | null) {
  if (!iso) return null;
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const d = new Date(iso);
  const target = new Date(d.getFullYear(), d.getMonth(), d.getDate());
  const days = Math.round((target.getTime() - today.getTime()) / 86_400_000);
  let label: string;
  if (days < 0) label = `${-days}d over`;
  else if (days === 0) label = 'today';
  else if (days === 1) label = 'tomorrow';
  else if (days < 7) label = `in ${days}d`;
  else label = target.toISOString().slice(5, 10).replace('-', '.');
  return { iso, days, label, overdue: days < 0, today: days === 0 };
}

// Sort: overdue-todos -> today-todos -> near-future-todos (by deadline) ->
// undated-todos -> doing -> done at the very bottom.
function sortKey(t: Task): [number, number, number] {
  const statusRank =
    t.status === 'done' ? 3 :
    t.status === 'doing' || t.status === 'in_progress' ? 2 : 1;
  const d = parseDeadline(t.deadline);
  const priorityRank =
    t.priority === 'high' ? 0 :
    t.priority === 'low' ? 2 : 1;
  if (!d) return [statusRank, 999_999, priorityRank];
  return [statusRank, d.days, priorityRank];
}

function statusColor(status: string, priority: string): string {
  if (status === 'done') return '#3a3a3a';
  if (status === 'doing' || status === 'in_progress') return '#f4d35e';
  if (priority === 'high') return '#fb923c';
  return '#a3b18a';
}

export default function TaskBoard() {
  const [data, setData] = useState<Feed | null>(null);
  const [busyId, setBusyId] = useState<number | null>(null);
  const router = useRouter();
  const params = useSearchParams();
  const selectedId = Number.parseInt(params.get('n') ?? '', 10);

  const load = async () => {
    try {
      const r = await fetch('/api/tasks', { cache: 'no-store' });
      const j = (await r.json()) as Feed;
      setData(j);
    } catch {
      /* keep previous */
    }
  };

  useEffect(() => {
    let cancelled = false;
    const run = async () => {
      const r = await fetch('/api/tasks', { cache: 'no-store' });
      const j = (await r.json()) as Feed;
      if (!cancelled) setData(j);
    };
    run().catch(() => {});
    const t = setInterval(run, 120_000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, []);

  const sorted = useMemo(
    () => (data?.tasks ?? []).slice().sort((a, b) => {
      const ka = sortKey(a);
      const kb = sortKey(b);
      for (let i = 0; i < 3; i++) if (ka[i] !== kb[i]) return ka[i] - kb[i];
      return a.name.localeCompare(b.name);
    }),
    [data],
  );

  const open = sorted.filter((t) => t.status !== 'done');
  const done = sorted.filter((t) => t.status === 'done');

  const select = (id: number) => {
    const q = new URLSearchParams(params.toString());
    q.set('n', String(id));
    router.replace(`?${q.toString()}`, { scroll: false });
  };

  const markDone = async (t: Task) => {
    if (!t.path) {
      alert('Task has no file path — ingester may not have seen it yet');
      return;
    }
    setBusyId(t.id);
    try {
      const r = await fetch('/api/tasks/status', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: t.path, status: 'done' }),
      });
      const j = await r.json();
      if (!r.ok || j.ok === false) {
        alert(`mark done failed: ${j.error ?? 'unknown'}`);
      } else {
        // Optimistic update; graph ingester will re-confirm within 2 min.
        setData((prev) => prev && {
          ...prev,
          tasks: prev.tasks.map((x) => x.id === t.id ? { ...x, status: 'done' } : x),
        });
      }
    } catch (e) {
      alert(`mark done error: ${(e as Error).message}`);
    } finally {
      setBusyId(null);
    }
  };

  const renderTask = (t: Task) => {
    const dl = parseDeadline(t.deadline);
    const active = t.id === selectedId;
    const isDone = t.status === 'done';
    const busy = busyId === t.id;
    return (
      <div key={t.id} className={`task-card${active ? ' active' : ''}${isDone ? ' done' : ''}`}>
        <button
          className="task-card-body"
          onClick={() => select(t.id)}
          type="button"
        >
          <div className="task-top">
            <span
              className="task-pill"
              style={{ background: statusColor(t.status, t.priority) }}
              title={`${t.status} · ${t.priority}`}
            />
            {dl && (
              <span
                className={`task-deadline${dl.overdue ? ' over' : dl.today ? ' today' : ''}`}
              >
                {dl.label}
              </span>
            )}
            {!dl && t.priority === 'high' && (
              <span className="task-deadline prio">hi</span>
            )}
            <span className="task-area">{t.area ?? t.status}</span>
          </div>
          <div className="task-name">{t.name}</div>
        </button>
        {!isDone && t.path && (
          <button
            className="task-done-btn"
            title="Mark as done (writes status: done to vault and commits)"
            onClick={(e) => { e.stopPropagation(); markDone(t); }}
            disabled={busy}
            type="button"
          >
            {busy ? '…' : '\u2713'}
          </button>
        )}
      </div>
    );
  };

  return (
    <div className="task-board">
      {data == null && <div className="activity-empty">loading</div>}
      {data && open.length === 0 && done.length === 0 && (
        <div className="activity-empty">no task nodes yet</div>
      )}
      {data?.error && (
        <div className="activity-empty">error · {data.error}</div>
      )}
      {open.length > 0 && (
        <div className="task-group">
          {open.map(renderTask)}
        </div>
      )}
      {done.length > 0 && (
        <>
          <div className="task-group-label">Done <span>{done.length}</span></div>
          <div className="task-group">
            {done.map(renderTask)}
          </div>
        </>
      )}
    </div>
  );
}
