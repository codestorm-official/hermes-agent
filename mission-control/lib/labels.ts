// Visual vocabulary for Neo4j node labels. Colors chosen so that the
// most common labels (Person, Company, Property) each feel distinct at a
// glance, and the technical labels (Memory, Note, Doc) fade into a
// neutral gray. No blue, no purple - keeps Mission Control out of
// generic-tech-SaaS territory.

export const LABEL_COLOR: Record<string, string> = {
  Person:    '#fb923c', // amber
  Company:   '#a3b18a', // sage
  Property:  '#8ab0d6', // muted steel
  Lead:      '#e57373', // coral
  Task:      '#f4d35e', // mustard
  Project:   '#d4a373', // bronze
  Daily:     '#c8b6ff', // pale lavender (the only violet, earned by "today")
  DailyLog:  '#9a8fb5', // dimmer lavender
  Dashboard: '#94a3a1', // slate-sage
  Memory:    '#b8a89a', // taupe
  Template:  '#707070', // gray
  Document:  '#707070',
  Doc:       '#707070',
  Note:      '#888',
  Stub:      '#3a3a3a', // near-invisible - these are placeholders
  unlabeled: '#3a3a3a',
};

export const LABEL_ORDER = [
  'Person',
  'Company',
  'Property',
  'Lead',
  'Task',
  'Project',
  'Daily',
  'DailyLog',
  'Dashboard',
  'Memory',
  'Template',
  'Note',
  'Document',
  'Doc',
  'Stub',
];

export function colorFor(label?: string | null): string {
  if (!label) return LABEL_COLOR.unlabeled;
  return LABEL_COLOR[label] ?? LABEL_COLOR.unlabeled;
}
