-- Ferme l'exposition des tables `public` via l'API REST Supabase (PostgREST).
--
-- Contexte : l'app HelloCrypto se connecte à Postgres en DIRECT (psycopg2, rôle
-- `postgres` superuser, cf. db/store.py). Elle n'utilise PAS la data API Supabase.
-- Le rôle `postgres` contourne RLS, donc activer RLS sans aucune policy :
--   - bloque tout accès anon/authenticated via l'API REST (la faille signalée)
--   - n'affecte en rien l'app (la connexion directe bypass RLS)
--
-- À exécuter une fois dans Supabase → SQL Editor. Idempotent.

ALTER TABLE public.trades          ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.agent_state     ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.logs            ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.sessions        ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.market_analyses ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.price_snapshots ENABLE ROW LEVEL SECURITY;

-- (Optionnel) Verrouillage supplémentaire : retirer les droits du rôle anon/
-- authenticated au niveau des privilèges Postgres, au cas où RLS serait un jour
-- désactivé par erreur. Décommenter si tu veux la ceinture + les bretelles.
-- REVOKE ALL ON public.trades, public.agent_state, public.logs,
--            public.sessions, public.market_analyses, public.price_snapshots
--   FROM anon, authenticated;
