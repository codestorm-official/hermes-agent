import { NextResponse } from 'next/server';
import { readQuery } from '@/lib/neo4j';

// Task nodes (files in Tasks/) with their YAML frontmatter + degree
// (how many other files reference them). Sorted client-side by
// deadline + priority + status.

export const dynamic = 'force-dynamic';
export const revalidate = 0;

type TaskRow = {
  id: { low: number; high: number } | number;
  name: string;
  path: string | null;
  status: string | null;
  priority: string | null;
  deadline: unknown;
  assignee: string | null;
  area: string | null;
  type: string | null;
  degree: { low: number; high: number } | number;
};

// Normalize mixed casings / spaces in status+priority so UI sort is stable.
// The vault currently has "Todo", "todo", "In Progress", "in_progress" etc.
function normStatus(v: string | null): string {
  const s = (v ?? 'todo').trim().toLowerCase().replace(/\s+/g, '_');
  return s || 'todo';
}
function normPriority(v: string | null): string {
  return (v ?? 'normal').trim().toLowerCase() || 'normal';
}

const toInt = (v: TaskRow['id']) =>
  typeof v === 'number' ? v : v.low + v.high * 2 ** 32;

function normalizeDate(v: unknown): string | null {
  if (v == null) return null;
  if (typeof v === 'string') return v;
  if (typeof v === 'object' && v !== null) {
    // Neo4j Date or DateTime object shape: { year, month, day, ... }
    const o = v as any;
    if (typeof o.year === 'object' && typeof o.month === 'object' && typeof o.day === 'object') {
      const y = toInt(o.year);
      const m = String(toInt(o.month)).padStart(2, '0');
      const d = String(toInt(o.day)).padStart(2, '0');
      return `${y}-${m}-${d}`;
    }
    if (typeof o.year === 'number') {
      const y = o.year;
      const m = String(o.month).padStart(2, '0');
      const d = String(o.day).padStart(2, '0');
      return `${y}-${m}-${d}`;
    }
  }
  return String(v);
}

export async function GET() {
  try {
    const rows = await readQuery<TaskRow>(
      `MATCH (t:Task)
       OPTIONAL MATCH (t)-[r]-()
       WITH t, count(r) AS degree
       RETURN id(t) AS id, t.name AS name, t.path AS path,
              t.status AS status, t.priority AS priority,
              t.deadline AS deadline, t.assignee AS assignee,
              t.area AS area, t.type AS type, degree
       ORDER BY t.name`,
    );
    const tasks = rows.map((r) => ({
      id: toInt(r.id),
      name: r.name ?? '(unnamed)',
      path: r.path ?? null,
      status: normStatus(r.status),
      priority: normPriority(r.priority),
      deadline: normalizeDate(r.deadline),
      assignee: r.assignee ?? null,
      area: r.area ?? null,
      type: r.type ?? null,
      degree: toInt(r.degree),
    }));
    return NextResponse.json({ tasks });
  } catch (e) {
    return NextResponse.json(
      { tasks: [], error: (e as Error).message },
      { status: 200 },
    );
  }
}
