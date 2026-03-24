create table if not exists public.leads (
  id bigint generated always as identity primary key,
  created_at timestamp without time zone not null,
  updated_at timestamp without time zone not null,
  nombre text not null,
  correo text not null,
  telefono text not null,
  edad integer not null,
  ingreso_mensual numeric not null,
  presupuesto_mensual numeric not null,
  dependientes integer not null,
  fumador text not null,
  ocupacion_riesgo text not null,
  objetivo text not null,
  aseguradora text not null,
  cobertura_sugerida numeric not null,
  prima_estimada numeric not null,
  plan_recomendado text not null,
  perfil_riesgo text not null,
  estatus text not null,
  completion_seconds numeric default 0
);

create table if not exists public.usage_logs (
  id bigint generated always as identity primary key,
  timestamp timestamp without time zone not null,
  action_type text not null,
  status text not null,
  detail text,
  duration_ms numeric default 0,
  user_name text,
  source text default 'streamlit',
  metadata_json text
);

alter table public.leads enable row level security;
alter table public.usage_logs enable row level security;

drop policy if exists "Allow service role full access leads" on public.leads;
create policy "Allow service role full access leads"
on public.leads
for all
using (true)
with check (true);

drop policy if exists "Allow service role full access logs" on public.usage_logs;
create policy "Allow service role full access logs"
on public.usage_logs
for all
using (true)
with check (true);