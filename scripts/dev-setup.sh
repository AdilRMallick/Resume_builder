#!/usr/bin/env bash
#
# Bring up Postgres (with pgvector) and Redis on a machine that has no Docker.
#
# The dev path is `docker compose up -d`; this script is for the machines where that
# is not available -- a CI runner, a cloud sandbox, a bare VM. It installs both
# services natively and binds them to the SAME ports docker-compose publishes:
#
#     Postgres  5433   (not the default 5432)
#     Redis     6380   (not the default 6379)
#
# That is deliberate. Every default in the codebase -- jme/config.py,
# fetcher/internal/config/config.go, tests/conftest.py, the Go store and queue
# suites -- points at 5433 and 6380. Matching them here means the test suites need
# no environment variables and no code changes to find their services. If you cannot
# bind those ports, set JME_DATABASE_URL, JME_TEST_DATABASE_URL,
# JME_TEST_DATABASE_URL_GO, JME_REDIS_URL and JME_TEST_REDIS_URL_GO instead.
#
# Usage:  sudo scripts/dev-setup.sh            (installs, starts, verifies)
#         scripts/dev-setup.sh --verify-only   (just checks what is reachable)
#
# Requires Debian/Ubuntu with apt and root. It is idempotent: re-running it is safe.

set -euo pipefail

PG_PORT="${PG_PORT:-5433}"
REDIS_PORT="${REDIS_PORT:-6380}"
PG_USER="${PG_USER:-jme}"
PG_PASSWORD="${PG_PASSWORD:-jme}"
PG_DB="${PG_DB:-jme}"
PG_TEST_DB="${PG_TEST_DB:-jme_test}"

log()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

# A plain TCP connect, for hosts that have the service but not its client binary --
# the common case when the services run in Docker and you are checking from outside.
# Reporting "no Postgres" because pg_isready is missing would be a lie.
port_open() {
    (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null && exec 3>&- && return 0
    return 1
}

verify() {
    local ok=0
    if command -v pg_isready >/dev/null 2>&1; then
        if pg_isready -h 127.0.0.1 -p "$PG_PORT" -q; then
            log "Postgres accepting connections on 127.0.0.1:$PG_PORT"
        else
            warn "no Postgres on 127.0.0.1:$PG_PORT"; ok=1
        fi
    elif port_open "$PG_PORT"; then
        log "port $PG_PORT open (pg_isready not installed, so this is a TCP check only)"
    else
        warn "nothing listening on 127.0.0.1:$PG_PORT"; ok=1
    fi

    if command -v redis-cli >/dev/null 2>&1; then
        if [ "$(redis-cli -h 127.0.0.1 -p "$REDIS_PORT" ping 2>/dev/null)" = "PONG" ]; then
            log "Redis answering PING on 127.0.0.1:$REDIS_PORT"
        else
            warn "no Redis on 127.0.0.1:$REDIS_PORT"; ok=1
        fi
    elif port_open "$REDIS_PORT"; then
        log "port $REDIS_PORT open (redis-cli not installed, so this is a TCP check only)"
    else
        warn "nothing listening on 127.0.0.1:$REDIS_PORT"; ok=1
    fi

    return $ok
}

if [ "${1:-}" = "--verify-only" ]; then
    verify && log "both services up" || die "one or both services are down"
    exit 0
fi

[ "$(id -u)" -eq 0 ] || die "run as root (sudo scripts/dev-setup.sh)"
command -v apt-get >/dev/null 2>&1 || die "this script assumes Debian/Ubuntu apt; on other systems install postgresql + pgvector + redis by hand and bind them to $PG_PORT/$REDIS_PORT"

# ---- install ---------------------------------------------------------------------

log "installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq

# The pgvector package is named for its server major version. Install the server
# first, then ask it what it is, rather than guessing the distro's default.
apt-get install -y -qq postgresql postgresql-contrib redis-server >/dev/null

PG_MAJOR="$(ls /usr/lib/postgresql 2>/dev/null | sort -Vr | head -n1 || true)"
[ -n "$PG_MAJOR" ] || die "postgresql installed but /usr/lib/postgresql is empty"
log "postgres major version $PG_MAJOR"

if apt-get install -y -qq "postgresql-$PG_MAJOR-pgvector" >/dev/null 2>&1; then
    log "pgvector installed from apt"
else
    # Older distros do not ship it. Building from source needs the server headers.
    warn "no postgresql-$PG_MAJOR-pgvector package; building pgvector from source"
    apt-get install -y -qq build-essential git "postgresql-server-dev-$PG_MAJOR" >/dev/null
    tmp="$(mktemp -d)"
    git clone --depth 1 --branch v0.7.4 https://github.com/pgvector/pgvector.git "$tmp/pgvector"
    make -C "$tmp/pgvector" >/dev/null
    make -C "$tmp/pgvector" install >/dev/null
    rm -rf "$tmp"
    log "pgvector built and installed"
fi

# ---- configure ports -------------------------------------------------------------

PG_CONF="/etc/postgresql/$PG_MAJOR/main/postgresql.conf"
if [ -f "$PG_CONF" ]; then
    log "binding Postgres to port $PG_PORT"
    sed -i -E "s/^#?port *=.*/port = $PG_PORT/" "$PG_CONF"
else
    warn "no $PG_CONF; leaving Postgres on its packaged port"
fi

log "binding Redis to port $REDIS_PORT"
if [ -f /etc/redis/redis.conf ]; then
    sed -i -E "s/^#?port .*/port $REDIS_PORT/" /etc/redis/redis.conf
    # appendonly matches docker-compose, so a restart does not silently lose state
    # mid-test the way an RDB-only server can.
    sed -i -E "s/^#?appendonly .*/appendonly yes/" /etc/redis/redis.conf
else
    warn "no /etc/redis/redis.conf; starting redis-server with flags instead"
fi

# ---- start -----------------------------------------------------------------------
#
# Sandboxes and containers frequently have no systemd. Try the service manager, then
# fall back to starting the daemons directly -- a failure of `service` is not a
# failure of the database.

start_service() {
    local name="$1"; shift
    if command -v service >/dev/null 2>&1 && service "$name" start >/dev/null 2>&1; then
        log "$name started via service"
        return 0
    fi
    log "$name: no working init, starting directly"
    "$@" || warn "could not start $name directly either"
}

start_service postgresql \
    su postgres -c "/usr/lib/postgresql/$PG_MAJOR/bin/pg_ctl -D /var/lib/postgresql/$PG_MAJOR/main -o '-p $PG_PORT' -l /tmp/pg.log start"

if [ -f /etc/redis/redis.conf ]; then
    start_service redis-server redis-server /etc/redis/redis.conf --daemonize yes
else
    start_service redis-server redis-server --port "$REDIS_PORT" --appendonly yes --daemonize yes
fi

# pg_ctl returns before the server accepts connections.
for _ in $(seq 1 30); do
    pg_isready -h 127.0.0.1 -p "$PG_PORT" -q && break
    sleep 1
done

# ---- databases and role ----------------------------------------------------------

psql_super() { su postgres -c "psql -p $PG_PORT -qtAX -c \"$1\""; }

log "creating role $PG_USER and databases $PG_DB, $PG_TEST_DB"
psql_super "SELECT 1 FROM pg_roles WHERE rolname='$PG_USER'" | grep -q 1 || \
    psql_super "CREATE ROLE $PG_USER LOGIN SUPERUSER PASSWORD '$PG_PASSWORD'"

for db in "$PG_DB" "$PG_TEST_DB"; do
    psql_super "SELECT 1 FROM pg_database WHERE datname='$db'" | grep -q 1 || \
        psql_super "CREATE DATABASE $db OWNER $PG_USER"
    # The Python test harness creates jme_test itself if it is missing, but it does
    # NOT install the extension, and CREATE EXTENSION needs superuser. Do it here.
    su postgres -c "psql -p $PG_PORT -qtAX -d $db -c 'CREATE EXTENSION IF NOT EXISTS vector'" >/dev/null
done

# ---- verify ----------------------------------------------------------------------

log "verifying"
verify || die "setup finished but a service is unreachable; check /tmp/pg.log"

cat <<EOF

Ready. The suites will find these with no environment variables set:

  Postgres  postgresql://$PG_USER:$PG_PASSWORD@localhost:$PG_PORT/$PG_DB
  Test DB   postgresql://$PG_USER:$PG_PASSWORD@localhost:$PG_PORT/$PG_TEST_DB
  Redis     redis://localhost:$REDIS_PORT/0

Next:
  python -m alembic upgrade head
  make test
EOF
