import { NextResponse } from 'next/server';
import { hermesPost } from '@/lib/hermes-agent';

export const dynamic = 'force-dynamic';

type StatusBody = { path?: string; status?: string };

export async function POST(req: Request) {
  try {
    const body = (await req.json()) as StatusBody;
    if (!body.path || !body.status) {
      return NextResponse.json(
        { ok: false, error: 'path and status required' },
        { status: 400 },
      );
    }
    const data = await hermesPost<{
      ok: boolean;
      path: string;
      old_status: string;
      new_status: string;
      git: string;
    }>('/api/vault/task/status', body);
    return NextResponse.json(data);
  } catch (e) {
    return NextResponse.json(
      { ok: false, error: (e as Error).message },
      { status: 500 },
    );
  }
}
