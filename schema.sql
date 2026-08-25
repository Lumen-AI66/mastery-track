-- Mastery Track — database schema.
--
-- Paste this into the Supabase SQL editor and run it once.
--
-- RLS is on with no policies, which means the anon key cannot touch any of it.
-- Only the secret (service_role) key gets through, and that key lives on the
-- machine running mastery.py — never in a browser.

create table if not exists public.mastery_topics (
  slug          text primary key,
  label         text not null,
  level         text not null default 'foundation'
                check (level in ('foundation','applied','advanced','mastery')),
  streak        integer not null default 0,   -- consecutive flawless independent answers
  last_asked_on date,
  mastered_at   timestamptz,
  active        boolean not null default true,
  created_at    timestamptz not null default now()
);

create table if not exists public.mastery_questions (
  id                  bigint generated always as identity primary key,
  asked_on            date not null default (now() at time zone 'utc')::date,
  slot                integer,                 -- position within the day's set
  topic               text not null references public.mastery_topics(slug),
  level               text not null            -- the topic's level when asked
                      check (level in ('foundation','applied','advanced','mastery')),
  concept             text not null,           -- stays identical across rephrasings
  question            text not null,
  expected            text,                    -- the answer key used for grading
  telegram_message_id bigint,
  answer              text,
  answered_at         timestamptz,
  verdict             text check (verdict in ('correct','incomplete','wrong','unknown')),
  feedback            text,
  status              text not null default 'new'
                      check (status in ('new','open','mastered')),
  attempts            integer not null default 1,
  repeat_of           bigint references public.mastery_questions(id) on delete set null,
  repeat_due_on       date,                    -- scheduled 3-10 days out
  asked_at            timestamptz not null default now(),
  graded_at           timestamptz
);

create index if not exists mastery_questions_topic_idx      on public.mastery_questions (topic);
create index if not exists mastery_questions_status_idx     on public.mastery_questions (status);
create index if not exists mastery_questions_due_idx        on public.mastery_questions (repeat_due_on) where status = 'open';
create index if not exists mastery_questions_asked_on_idx   on public.mastery_questions (asked_on desc);
create index if not exists mastery_questions_msg_idx        on public.mastery_questions (telegram_message_id);
create index if not exists mastery_questions_unanswered_idx on public.mastery_questions (asked_on desc, slot) where answer is null;

-- small key/value store: the Telegram getUpdates offset lives here
create table if not exists public.mastery_state (
  key        text primary key,
  value      jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now()
);

alter table public.mastery_topics    enable row level security;
alter table public.mastery_questions enable row level security;
alter table public.mastery_state     enable row level security;

-- one row per topic, used by `report` and `topics`
create or replace view public.mastery_report as
select
  t.slug                                                          as topic,
  t.label,
  t.level,
  t.streak,
  t.last_asked_on,
  count(q.id)                                                     as asked,
  count(*) filter (where q.status = 'mastered')                   as mastered,
  count(*) filter (where q.status = 'open')                       as open_items,
  count(*) filter (where q.verdict = 'unknown')                   as dont_know,
  count(*) filter (where q.verdict in ('wrong','incomplete'))     as wrong_or_incomplete,
  round(avg(q.attempts) filter (where q.status = 'mastered'), 2)  as avg_attempts_to_mastery,
  round(
    100.0 * count(*) filter (where q.status = 'mastered')
    / nullif(count(*) filter (where q.answer is not null), 0), 1) as mastered_pct
from public.mastery_topics t
left join public.mastery_questions q on q.topic = t.slug
group by t.slug, t.label, t.level, t.streak, t.last_asked_on;

-- Replace these with whatever you are actually studying. The slug is what the
-- model sees, so keep it short; the label is what shows up in your chat.
insert into public.mastery_topics (slug, label) values
  ('hooks',        'Claude Code hooks'),
  ('subagents',    'Subagents and agent orchestration'),
  ('mcp',          'MCP (Model Context Protocol)'),
  ('cicd',         'CI/CD and release automation'),
  ('architecture', 'System architecture'),
  ('tooling',      'The wider AI tooling landscape')
on conflict (slug) do nothing;
