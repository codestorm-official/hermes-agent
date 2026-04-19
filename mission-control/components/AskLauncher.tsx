'use client';

import { useEffect, useState } from 'react';
import AskPanel from './AskPanel';

// Hosts AskPanel and listens for global triggers: Cmd+/ on the
// keyboard, or the custom 'hermes:ask-open' event fired by the
// command palette's "ask a question" action.

export default function AskLauncher() {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === '/') {
        e.preventDefault();
        setOpen((o) => !o);
      }
    };
    const onEvt = () => setOpen(true);
    window.addEventListener('keydown', onKey);
    window.addEventListener('hermes:ask-open' as any, onEvt);
    return () => {
      window.removeEventListener('keydown', onKey);
      window.removeEventListener('hermes:ask-open' as any, onEvt);
    };
  }, []);

  return <AskPanel open={open} onClose={() => setOpen(false)} />;
}
