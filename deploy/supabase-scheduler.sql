-- Run in the dedicated testing project's SQL Editor after enabling Cron,
-- pg_net and Vault in Supabase Integrations / Extensions.
-- Create Vault secrets xpredict_origin (stable HTTPS origin, no trailing /),
-- xpredict_cron_secret (same CRON_SECRET as Vercel), and optionally
-- xpredict_vercel_bypass (deployment-protection automation bypass secret).
-- No plaintext secrets are embedded in stored cron commands.

BEGIN;
CREATE SCHEMA IF NOT EXISTS lisa_scheduler;
REVOKE ALL ON SCHEMA lisa_scheduler FROM PUBLIC;

CREATE TABLE IF NOT EXISTS lisa_scheduler.dispatches (
    request_id BIGINT PRIMARY KEY,
    job TEXT NOT NULL,
    queued_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION lisa_scheduler.enqueue(job_name TEXT)
RETURNS BIGINT
LANGUAGE plpgsql
SECURITY INVOKER
SET search_path = pg_catalog
AS $function$
DECLARE
    origin TEXT;
    cron_secret TEXT;
    bypass TEXT;
    request_headers JSONB;
    request_id BIGINT;
BEGIN
    IF job_name NOT IN ('generation','settlement','history') THEN
        RAISE EXCEPTION 'Unknown application job';
    END IF;
    SELECT decrypted_secret INTO origin FROM vault.decrypted_secrets WHERE name='xpredict_origin';
    SELECT decrypted_secret INTO cron_secret FROM vault.decrypted_secrets WHERE name='xpredict_cron_secret';
    SELECT decrypted_secret INTO bypass FROM vault.decrypted_secrets WHERE name='xpredict_vercel_bypass';
    IF origin IS NULL OR origin !~ '^https://[A-Za-z0-9.-]+(:443)?$'
            OR cron_secret IS NULL OR length(cron_secret)<32 THEN
        RAISE EXCEPTION 'Configure application origin and cron secret in Vault';
    END IF;
    request_headers := jsonb_build_object('Content-Type','application/json',
                                         'Authorization','Bearer '||cron_secret);
    IF bypass IS NOT NULL AND bypass<>'' THEN
        request_headers := request_headers || jsonb_build_object('x-vercel-protection-bypass',bypass);
    END IF;
    SELECT net.http_post(url:=origin||'/api/cron/'||job_name,
        headers:=request_headers,body:='{}'::jsonb,timeout_milliseconds:=300000)
        INTO request_id;
    INSERT INTO lisa_scheduler.dispatches(request_id,job) VALUES (request_id,job_name);
    DELETE FROM lisa_scheduler.dispatches WHERE queued_at<now()-INTERVAL '7 days';
    RETURN request_id;
END;
$function$;
REVOKE ALL ON FUNCTION lisa_scheduler.enqueue(TEXT) FROM PUBLIC;

-- Named jobs are updated when this setup is rerun. These are UTC schedules.
SELECT cron.schedule('xpredict-history','7 * * * *',
                     $job$SELECT lisa_scheduler.enqueue('history');$job$);
SELECT cron.schedule('xpredict-generation','17 * * * *',
                     $job$SELECT lisa_scheduler.enqueue('generation');$job$);
SELECT cron.schedule('xpredict-settlement','*/15 * * * *',
                     $job$SELECT lisa_scheduler.enqueue('settlement');$job$);
COMMIT;

-- Enqueue success is NOT job completion. Inspect HTTP status/timeouts here;
-- completed runs and settlement evidence appear in the app's Operations page.
SELECT d.job,d.queued_at,r.status_code,r.timed_out
FROM lisa_scheduler.dispatches d
LEFT JOIN net._http_response r ON r.id=d.request_id
ORDER BY d.queued_at DESC LIMIT 30;
