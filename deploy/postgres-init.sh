#!/bin/sh
set -eu
# Runs only on first initialization. Bind the password through psql quoting;
# do not interpolate it into SQL shell text.
psql -v ON_ERROR_STOP=1 --username postgres --dbname lisa \
  --set=app_password="$LISA_DATABASE_PASSWORD" <<'SQL'
CREATE ROLE lisa LOGIN PASSWORD :'app_password';
ALTER DATABASE lisa OWNER TO lisa;
GRANT USAGE, CREATE ON SCHEMA public TO lisa;
SQL
