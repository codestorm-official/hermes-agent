'use client';

import { useEffect, useState } from 'react';

type Skill = {
  name: string;
  description: string;
  path: string;
  scripts: string[];
};

type Feed = { skills: Skill[]; error?: string };

export default function SkillsPanel() {
  const [data, setData] = useState<Feed | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const r = await fetch('/api/skills', { cache: 'no-store' });
        const j = (await r.json()) as Feed;
        if (!cancelled) setData(j);
      } catch {
        /* keep previous */
      }
    };
    load();
    const t = setInterval(load, 60_000);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, []);

  if (data == null) return <div className="activity-empty">loading</div>;
  if (data.error) return <div className="activity-empty">error · {data.error}</div>;
  if (!data.skills.length) return <div className="activity-empty">no skills registered yet</div>;

  return (
    <div className="skills-panel">
      {data.skills.map((s) => (
        <div key={s.path} className="skill-card">
          <div className="skill-top">
            <span className="skill-name">{s.name}</span>
            <span className="skill-scripts">
              {s.scripts.length > 0 ? `${s.scripts.length} script${s.scripts.length === 1 ? '' : 's'}` : 'no scripts'}
            </span>
          </div>
          <div className="skill-desc">{s.description}</div>
          {s.scripts.length > 0 && (
            <div className="skill-scripts-list">
              {s.scripts.map((n) => (
                <span key={n} className="skill-script-chip">{n}</span>
              ))}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
