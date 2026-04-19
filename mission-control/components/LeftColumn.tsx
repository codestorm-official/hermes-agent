'use client';

import { useState } from 'react';
import ActivityFeed from './ActivityFeed';
import TaskBoard from './TaskBoard';
import SkillsPanel from './SkillsPanel';

type Tab = 'tasks' | 'activity' | 'skills';

export default function LeftColumn() {
  const [tab, setTab] = useState<Tab>('tasks');
  return (
    <aside className="left-col">
      <div className="left-tabs">
        <button
          className={`left-tab${tab === 'tasks' ? ' active' : ''}`}
          onClick={() => setTab('tasks')}
        >
          Tasks
        </button>
        <button
          className={`left-tab${tab === 'activity' ? ' active' : ''}`}
          onClick={() => setTab('activity')}
        >
          Activity
        </button>
        <button
          className={`left-tab${tab === 'skills' ? ' active' : ''}`}
          onClick={() => setTab('skills')}
        >
          Skills
        </button>
      </div>
      <div className="left-body">
        {tab === 'tasks' && <TaskBoard />}
        {tab === 'activity' && <ActivityFeed inline />}
        {tab === 'skills' && <SkillsPanel />}
      </div>
    </aside>
  );
}
