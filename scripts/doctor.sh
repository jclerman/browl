#!/bin/sh
# Check the tools browl's development needs (`make doctor`, CONTRIBUTING.md). Exits non-zero if a required one is
# missing or too old. Optional ones only warn: locally, the tests that need them are skipped (CI has them all).

status=0

ok() { printf '  ok       %s\n' "$1"; }
warn() { printf '  missing  %s\n' "$1"; }
fail() {
    printf '  MISSING  %s\n' "$1"
    status=1
}

# Succeed if dotted version $1 is at least $2. (macOS's sort has no -V.)
at_least() {
    awk -v have="$1" -v want="$2" 'BEGIN {
        n = split(have, h, "."); m = split(want, w, ".")
        for (i = 1; i <= (n > m ? n : m); i++) {
            if (h[i] + 0 != w[i] + 0) exit !(h[i] + 0 > w[i] + 0)
        }
        exit 0
    }'
}

echo "Required:"

if command -v git >/dev/null 2>&1; then
    ok "$(git --version)"
else
    fail "Git"
fi

if command -v poetry >/dev/null 2>&1; then
    version=$(poetry --version | sed -n 's/.*version \([0-9][0-9.]*\).*/\1/p')
    if at_least "$version" 2.5.1; then
        ok "Poetry $version"
    else
        fail "Poetry 2.5.1+ (found $version)"
    fi
    venv=$(poetry env info --path 2>/dev/null)
else
    fail "Poetry 2.5.1+"
    venv=
fi

# The venv's Python once `make install` has made it, else the python3 Poetry would start from.
if [ -n "$venv" ] && [ -x "$venv/bin/python" ]; then
    python=$venv/bin/python
    where="Poetry venv"
else
    python=$(command -v python3)
    where="python3 on PATH; run \`make install\` to create the venv"
fi
version=$([ -n "$python" ] && "$python" -c 'import platform; print(platform.python_version())' 2>/dev/null)
if [ -z "$version" ]; then
    fail "Python 3.12+"
elif at_least "$version" 3.12; then
    ok "Python $version ($where)"
else
    fail "Python 3.12+ (found $version, $where)"
fi

for tool in curl shasum tar; do
    if command -v "$tool" >/dev/null 2>&1; then
        ok "$tool"
    else
        fail "$tool (\`make test\` downloads robot.jar and Jena's tools with it, \`make ci\` tbls)"
    fi
done

# The tbls the Makefile pins, for the schema docs; `make ci` downloads it when it's missing.
version=$(make -s --no-print-directory -C "$(dirname "$0")/.." tbls-version)
if [ -x "$(dirname "$0")/../.cache/tbls/$version/tbls" ]; then
    ok "tbls $version (in .cache/)"
else
    warn "tbls $version: \`make tbls\` downloads it (\`make ci\` does too)"
fi

hook=$(git rev-parse --git-path hooks/pre-commit 2>/dev/null)
if [ -n "$hook" ] && [ -f "$hook" ]; then
    ok "pre-commit git hook"
else
    fail "pre-commit git hook (run \`make install\`)"
fi

echo "Optional (CI has them):"

# As tests/conftest.py finds it: JAVA_HOME, else on macOS an installed Java 21, else java on PATH.
java_home=${JAVA_HOME:-}
if [ -z "$java_home" ] && [ -x /usr/libexec/java_home ]; then
    java_home=$(/usr/libexec/java_home -v 21 2>/dev/null)
fi
if [ -n "$java_home" ]; then
    java=$java_home/bin/java
else
    java=$(command -v java)
fi
version=$([ -n "$java" ] && "$java" -version 2>&1 | sed -n 's/.*version "\([0-9][0-9._]*\)".*/\1/p' | head -n 1)
if [ "${version%%.*}" = 21 ]; then
    ok "Java $version"
elif [ -n "$version" ]; then
    warn "Java 21 (found $version; set JAVA_HOME to a Java 21): the jvm tests will be skipped"
else
    warn "Java 21: the jvm tests will be skipped"
fi

if [ -n "${BROWL_TEST_FUSEKI_URL:-}" ]; then
    ok "Fuseki for the tests at BROWL_TEST_FUSEKI_URL ($BROWL_TEST_FUSEKI_URL)"
elif ! command -v docker >/dev/null 2>&1; then
    warn "Docker: the fuseki tests will be skipped, and the app can't be run with Docker Compose"
elif ! docker info >/dev/null 2>&1; then
    warn "Docker is installed but not running: the fuseki tests will be skipped"
elif ! docker compose version >/dev/null 2>&1; then
    warn "Docker Compose: the app can't be run with \`docker compose up\`"
else
    ok "$(docker --version | sed 's/,.*//'), with Compose"
fi

if poetry run python -c 'from playwright.sync_api import sync_playwright
with sync_playwright() as playwright:
    playwright.chromium.launch().close()' >/dev/null 2>&1; then
    ok "Playwright's headless Chromium"
else
    warn "Playwright's headless Chromium (run \`make browser\`; on Linux, also its system libraries, CONTRIBUTING.md): the browser tests will be skipped"
fi

if command -v shellcheck >/dev/null 2>&1; then
    ok "ShellCheck"
else
    warn "ShellCheck: actionlint won't check the workflows' run: steps with it"
fi

if [ "$status" -ne 0 ]; then
    echo "Install what's missing: see CONTRIBUTING.md."
fi
exit "$status"
