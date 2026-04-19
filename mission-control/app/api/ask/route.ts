import { NextRequest, NextResponse } from 'next/server';
import neo4j from 'neo4j-driver';
import { driver as neoDriver } from '@/lib/neo4j';

// Natural-language → Cypher. Calls Anthropic's Messages API with a
// schema preamble and five few-shot examples, extracts the first ```
// cypher block, runs it in READ mode, returns both the generated
// query and the rows.

export const dynamic = 'force-dynamic';
export const revalidate = 0;
export const runtime = 'nodejs';

const SCHEMA = `Neo4j graph schema (Hermes vault):

Node labels:
  :Person, :Company, :Property, :Task, :Lead, :Project,
  :Daily, :DailyLog, :Dashboard, :Memory, :Template,
  :Note, :Document, :Doc, :Stub (no dedicated file yet)

Common node properties (vary by label - not every node has all):
  name (string, always present, used for MERGE identity)
  path (string, relative vault path)
  folder (string, top-level folder)
  type, status, priority, deadline, assignee, area   (YAML on Tasks)
  email, phone, context                              (People)
  portfolio, mfiles_id                               (Properties)
  date                                               (Daily notes, ISO 8601)

Relationships:
  [:MENTIONS]   - from any node to any node; created from [[wikilinks]]
                  in the markdown body of the source file.
  [:REFERS_TO {via: <yaml_key>}]
                - from a node to another node whose name matches a YAML
                  value. {via: "eigentuemer" | "hausmeister" |
                  "hausverwaltung" | "area" | "assignee" | "context" |
                  "contact" | ...}. Never points at Stubs.

Conventions:
  - Names are human-readable (e.g. "Sandra Habermann", "Zweibrueckenstr. 15").
  - Dates use date(d.date); never datetime() on Daily.date values.
  - Always LIMIT results reasonably (10-50) unless asked otherwise.
  - Read-only queries only. Never MERGE, CREATE, DELETE, SET, REMOVE.`;

const EXAMPLES: { q: string; cypher: string }[] = [
  {
    q: 'welche Properties hat Zweibrueckenstr. 15 Grundstuecks GbR als Eigentuemer',
    cypher:
      "MATCH (p:Property)-[:REFERS_TO {via: 'eigentuemer'}]->(c {name: 'Zweibrueckenstr. 15 Grundstuecks GbR'}) RETURN p.name AS property LIMIT 20",
  },
  {
    q: 'alle offenen Tasks mit hoher Prioritaet',
    cypher:
      "MATCH (t:Task) WHERE toLower(coalesce(t.status, 'todo')) IN ['todo','doing'] AND toLower(coalesce(t.priority,'')) IN ['high','hi'] RETURN t.name AS task, t.status AS status, t.deadline AS deadline ORDER BY t.deadline LIMIT 30",
  },
  {
    q: 'wer haengt alles mit Fabrizi zusammen',
    cypher:
      "MATCH (p {name: 'Fabrizi'})-[r:MENTIONS|REFERS_TO]-(n) RETURN DISTINCT n.name AS name, labels(n)[0] AS label, type(r) AS rel LIMIT 25",
  },
  {
    q: 'welche Personen arbeiten bei Buero Birnbaum',
    cypher:
      "MATCH (p:Person)-[:REFERS_TO {via: 'context'}]-(c {name: 'Buero Birnbaum'}) RETURN p.name AS person LIMIT 30",
  },
  {
    q: 'wie viele Daily Notes habe ich',
    cypher:
      'MATCH (d:Daily) RETURN count(d) AS daily_notes',
  },
];

const FORBIDDEN = /\b(MERGE|CREATE|DELETE|SET|REMOVE|DROP|CALL\s+db\.|DBMS|FOREACH)\b/i;

function buildPrompt(question: string): string {
  const shots = EXAMPLES.map(
    (e) => `Frage: ${e.q}\n\`\`\`cypher\n${e.cypher}\n\`\`\``,
  ).join('\n\n');
  return `${SCHEMA}

Beispiele:

${shots}

Frage: ${question}
\`\`\`cypher
`;
}

function extractCypher(text: string): string | null {
  const m = text.match(/```(?:cypher)?\s*([\s\S]*?)```/i);
  if (m) return m[1].trim();
  // Fallback - model may have omitted fences.
  const trimmed = text.trim();
  if (trimmed.toUpperCase().startsWith('MATCH') || trimmed.toUpperCase().startsWith('RETURN')) {
    return trimmed;
  }
  return null;
}

async function callLLM(prompt: string): Promise<string> {
  const key = process.env.ANTHROPIC_API_KEY;
  if (!key) throw new Error('ANTHROPIC_API_KEY not configured');
  const r = await fetch('https://api.anthropic.com/v1/messages', {
    method: 'POST',
    headers: {
      'x-api-key': key,
      'anthropic-version': '2023-06-01',
      'content-type': 'application/json',
    },
    body: JSON.stringify({
      model: 'claude-haiku-4-5-20251001',
      max_tokens: 512,
      system:
        'Du uebersetzt deutsche/englische Fragen in read-only Cypher ' +
        'fuer einen Neo4j-Graph. Antworte AUSSCHLIESSLICH mit einem ' +
        '```cypher ... ``` Block. Kein Vorreden, keine Erklaerung.',
      messages: [{ role: 'user', content: prompt }],
    }),
  });
  if (!r.ok) {
    const t = await r.text();
    throw new Error(`Anthropic API ${r.status}: ${t.slice(0, 200)}`);
  }
  const j = (await r.json()) as { content: { text: string }[] };
  return j.content[0]?.text ?? '';
}

async function runReadOnly(cypher: string): Promise<unknown[]> {
  const s = neoDriver().session({ defaultAccessMode: neo4j.session.READ });
  try {
    const res = await s.run(cypher);
    return res.records.map((r) => {
      const obj = r.toObject() as Record<string, unknown>;
      // Unwrap Neo4j Integer so the JSON response is clean.
      for (const [k, v] of Object.entries(obj)) {
        if (v && typeof v === 'object' && 'low' in (v as object) && 'high' in (v as object)) {
          obj[k] = (v as any).low + (v as any).high * 2 ** 32;
        }
      }
      return obj;
    });
  } finally {
    await s.close();
  }
}

export async function POST(req: NextRequest) {
  const body = (await req.json().catch(() => ({}))) as { question?: string };
  const question = (body.question ?? '').trim();
  if (!question) {
    return NextResponse.json({ error: 'no question' }, { status: 400 });
  }
  if (!process.env.ANTHROPIC_API_KEY) {
    return NextResponse.json(
      {
        configured: false,
        error:
          'Ask panel requires ANTHROPIC_API_KEY on hermes-mission-control. Set via Railway dashboard.',
      },
      { status: 200 },
    );
  }
  try {
    const prompt = buildPrompt(question);
    const raw = await callLLM(prompt);
    const cypher = extractCypher(raw);
    if (!cypher) {
      return NextResponse.json({
        configured: true,
        question,
        raw,
        error: 'no cypher block extracted',
      });
    }
    if (FORBIDDEN.test(cypher)) {
      return NextResponse.json({
        configured: true,
        question,
        cypher,
        error: 'query contains a write keyword and was refused',
      });
    }
    const rows = await runReadOnly(cypher);
    return NextResponse.json({ configured: true, question, cypher, rows });
  } catch (e) {
    return NextResponse.json(
      { configured: true, question, error: (e as Error).message },
      { status: 200 },
    );
  }
}
