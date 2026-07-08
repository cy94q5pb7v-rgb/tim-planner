PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

-- ТИМ Планер: командная доска планирования одним JSON-снимком (key='main').
CREATE TABLE IF NOT EXISTS planner_state (
  key        TEXT PRIMARY KEY,
  data       TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
