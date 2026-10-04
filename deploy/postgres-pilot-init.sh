#!/bin/sh
set -eu
psql -v ON_ERROR_STOP=1 --username postgres --dbname xpredict_pilot \
  --set=app_password="$LISA_PILOT_DATABASE_PASSWORD" <<'SQL'
CREATE ROLE lisa_pilot LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD :'app_password';
ALTER DATABASE xpredict_pilot OWNER TO lisa_pilot;
GRANT USAGE, CREATE ON SCHEMA public TO lisa_pilot;
CREATE DATABASE xpredict_pilot_test OWNER lisa_pilot;
SQL
psql -v ON_ERROR_STOP=1 --username postgres --dbname xpredict_pilot_test <<'SQL'
GRANT USAGE, CREATE ON SCHEMA public TO lisa_pilot;
SQL
