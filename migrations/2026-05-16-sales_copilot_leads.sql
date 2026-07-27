-- Sales Copilot waitlist leads
-- Gemaakt: 2026-05-16
-- Doel: capture van email + context van LinkedIn-launch tot eerste-call

create table if not exists public.sales_copilot_leads (
  id           uuid        primary key default gen_random_uuid(),
  email        text        not null,
  created_at   timestamptz not null default now(),
  source       text        default 'linkedin-landing',
  utm_source   text,
  utm_medium   text,
  utm_campaign text,
  tier         text        default 'free',
  company      text,
  use_case     text,
  sector       text,
  consent_marketing boolean default false,
  -- Audit-kolommen per Vincent's standaard
  updated_at   timestamptz not null default now(),
  ip_address   inet,
  user_agent   text,
  status       text        default 'new' -- new | contacted | onboarded | declined
);

-- Voorkom dubbele inschrijvingen per email
create unique index if not exists sales_copilot_leads_email_idx
  on public.sales_copilot_leads(lower(email));

-- RLS: anon mag insert, niemand mag select/update behalve service_role
alter table public.sales_copilot_leads enable row level security;

create policy "anon can insert leads"
  on public.sales_copilot_leads
  for insert
  with check (true);

-- Trigger voor updated_at
create or replace function update_updated_at_column()
  returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

create trigger sales_copilot_leads_updated_at
  before update on public.sales_copilot_leads
  for each row execute function update_updated_at_column();
