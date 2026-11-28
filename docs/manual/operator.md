# Operating browl

For operators: deploying browl, configuring it, and keeping it running. [Docker Compose](#deploying-with-docker-compose)
is the supported deployment. For Kubernetes, or any other platform, read this page first, then
[Deploying on Kubernetes](kubernetes.md), which says what a platform must provide, with example manifests.

**Contents:**

- [What you're deploying](#what-youre-deploying)
- [Requirements](#requirements)
- [Deploying with Docker Compose](#deploying-with-docker-compose)
- [Settings](#settings)
- [HTTPS and reverse proxies](#https-and-reverse-proxies)
- [Upgrading](#upgrading)
- [Backups](#backups)
- [Logs and health](#logs-and-health)
- [Troubleshooting](#troubleshooting)

## What you're deploying

browl runs as three containers, all running as uid 10001:

| Container | Image | Does | Needs network |
| --- | --- | --- | --- |
| **app** | `Dockerfile` | The website (FastAPI, port 8000), and the load pipeline: fetching imports, building the triple store, computing the search index and hierarchy counts | Inbound from users; outbound to fetch an ontology's imports and for Google sign-in |
| **fuseki** | `docker/fuseki/Dockerfile` | [Apache Jena Fuseki](https://jena.apache.org/documentation/fuseki2/), holding the ontology in a TDB2 store, queried by the app | From the app only. Its admin protocol is open to anything that reaches it, so never expose it |
| **sandbox** | `Dockerfile` (the app's image) | Runs [ROBOT](https://github.com/ontodev/robot) (reasoning) and the OWL Functional Syntax parser for the app, over a Unix socket | **None.** It's a security boundary: an import that gets past browl's checks fails the load instead of reaching the network or the app's files |

And four volumes:

| Volume | Mounted at | By | Holds | If lost |
| --- | --- | --- | --- | --- |
| `data` | `/data` | app | Job state, the admins added in the UI, the session key, derived databases (search index, hierarchy counts) | **Back it up.** Admins added in the UI are gone, and the site shows no ontology until an admin uploads again |
| `fuseki` | `/fuseki` | fuseki, app | The served ontology's TDB2 store | **Back it up**, or keep the ontology file: pages fail until an admin uploads again |
| `jobs` | `/data/uploads` | app, sandbox | The latest load's files: the upload, fetched imports, reasoner output | Nothing; each upload replaces them |
| `sandbox` | `/sandbox` | app, sandbox | The sandbox's socket | Nothing; recreated at start |

The app's loader writes each new TDB2 store onto Fuseki's volume, and the app and sandbox share the jobs volume at the
same path, so these volumes must be shared between the containers that mount them.

Restarting a container doesn't reload the ontology: Fuseki reopens its store in under a second, and the app reuses its
derived database. [DESIGN.md](../DESIGN.md#data-and-persistence) has the details.

## Requirements

- **Docker Engine 25 or later, with Compose v2.24 or later**, on Linux, macOS or Windows. browl's images are built for
  the host's architecture (amd64 and arm64 both work).
- **A domain name and HTTPS** for anything but a local trial: Google sign-in accepts `http` only for `localhost`.
- **A Google OAuth client**, which you register yourself ([`AUTHENTICATION.md`](../AUTHENTICATION.md)). It's free and
  needs no billing.
- **Memory**, which depends on the ontology. Figures are in GiB and MiB (a heap setting's `g` and `m` mean the same);
  disk sizes below are in GB.

  | Container | Needs | Default settings |
  | --- | --- | --- |
  | fuseki | Its heap (`FUSEKI_HEAP`) plus about 0.5 GiB. NCBITaxon (2.6 M classes) needs about 500 MiB of heap | 1.5 GiB |
  | sandbox | `REASONER_MAX_HEAP` plus about 0.5 GiB, only while a reasoner runs. ELK needs about 1 GiB for Uberon and 16 GiB for NCBITaxon | 8.5 GiB while reasoning |
  | app | Its JVM's heap (`APP_JVM_MAX_HEAP`, 1 GiB) plus the Python process, which builds the derived data after each load | Not yet measured; watch `docker stats` during your ontology's first load |

  Fuseki's store is memory-mapped, so spare memory makes pages faster.
- **Disk**, roughly: on the jobs volume, the upload (up to 20 GB by default), its fetched imports, and the reasoner's
  output, which can be much larger, with 1 GB to spare; on the fuseki volume, the TDB2 store, a few times the
  ontology's Turtle size (3.7 GB for NCBITaxon), twice over while a new upload replaces it; and on the data volume, the
  derived databases, also twice over during a load.

## Deploying with Docker Compose

browl doesn't publish images yet; Compose builds them from the source.

1. **Get the source** at the release you want:

   ```sh
   git clone https://github.com/jclerman/browl.git
   cd browl
   git checkout v0.1.0
   ```

2. **Register Google sign-in** as [`AUTHENTICATION.md`](../AUTHENTICATION.md) says. The redirect URI to register is
   your `BASE_URL` without its trailing `/`, then `/_browl/admin/oidc-callback` (with your `RESERVED_PREFIX` in place
   of `_browl`, if you change it), e.g. `https://ontology.example.org/_browl/admin/oidc-callback`. It must match
   exactly.

3. **Write `.env`** from the example, which Compose passes to the app:

   ```sh
   cp .env.example .env
   ```

   Then edit it. For production, at least:

   ```sh
   BASE_URL=https://ontology.example.org/
   OIDC_CLIENT_ID=1234567890-abc.apps.googleusercontent.com
   OIDC_CLIENT_SECRET=GOCSPX-…
   SEED_ADMINS='["you@example.org"]'
   ```

   **Delete the line `AUTH_DISABLED=true`**, which the example has, or set it to `false`: with it, sign-in is off and
   everyone who reaches the site is an admin. `.env` holds a secret; keep it readable only by you (`chmod 600 .env`).

   For a local trial, the example as it is works without Google, with sign-in off. Then also set
   `BROWL_PORT=127.0.0.1:8000`, so only your own computer can reach the site: Docker publishes ports on every network
   interface by default, past firewalls such as ufw.

4. **Start it:**

   ```sh
   docker compose up -d --build
   ```

   The first build downloads Java, ROBOT, Jena and Fuseki, and takes a few minutes. `docker compose ps` shows the
   three services as `healthy` once they're up.

5. **Check it** at `BASE_URL`: with no ontology loaded, the site says it's not available. Opening
   `/_browl/admin/upload` should send you to Google to sign in; if it shows the upload form instead, sign-in is off.
   Sign in and upload an ontology ([Administering browl](admin.md)).

`docker compose stop` and `start` stop and restart the containers; `down` also removes them, but keeps the volumes.
`docker compose down -v` **deletes the volumes**, and with them the ontology and the admin list.

The Compose project's name, `browl` (set in `docker-compose.yml`), prefixes the volumes: `browl_data`, …. A second
deployment on the same host needs a name of its own, even from another directory, or it takes over the first one's
volumes: run its every command as `docker compose -p <name> …` (or set `COMPOSE_PROJECT_NAME=<name>` in its `.env`),
with another `BROWL_PORT`. Its volumes are then `<name>_data`, ….

## Settings

Settings are environment variables, which Compose takes from `.env`. Lists and mappings are JSON. The app checks them
at startup and refuses to start, naming the variable, if one is missing or invalid.

| Setting | Default | What it does |
| --- | --- | --- |
| `BASE_URL` | none; required | Where the site is served, e.g. `https://ontology.example.org/`. Entities whose IRIs are under it get pages at their own paths ([URLs](user.md#urls)). Serve browl at the root of its host; serving it under a sub-path isn't supported yet |
| `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` | none | Your Google OAuth client ([`AUTHENTICATION.md`](../AUTHENTICATION.md)). Required unless `AUTH_DISABLED` |
| `SEED_ADMINS` | none | Admins' Google account email addresses, e.g. `'["you@example.org"]'`. They can't be removed in the UI. Required unless `AUTH_DISABLED` |
| `AUTH_DISABLED` | `false` | `true` turns sign-in off and makes everyone an admin. For local development only; **never in production** |
| `RESERVED_PREFIX` | `_browl` | The first path segment of browl's own pages (search, trees, admin). Must start with `_`. Change it only if your ontology's IRIs use `_browl` |
| `NAME_PROPERTIES` | `["rdfs:label"]` | The annotation properties that name entities, best first, as CURIEs or IRIs, e.g. `'["rdfs:label", "skos:prefLabel"]'`. Used for display names and search |
| `LANGUAGE_RANKING` | `["en"]` | Preferred languages for names, best first. `none` stands for values with no language tag, last unless listed |
| `EXTRA_PREFIXES` | `{}` | CURIE prefixes to show, overriding the ontology's own and browl's built-in ones, e.g. `'{"ex": "http://example.org/"}'` |
| `REASONER_TIMEOUT_S` | `3600` | Time limit for a reasoner run, in seconds |
| `REASONER_MAX_HEAP` | `8g` | Heap limit for a reasoner run. Set `16g` for ontologies of NCBITaxon's size |
| `MATERIALIZE_PROPERTIES` | `[]` | Object properties for which reasoning also adds inferred `SubClassOf(C, P some D)` axioms, as IRIs or CURIEs such as `obo:BFO_0000050` |
| `APP_JVM_MAX_HEAP` | `1g` | Heap for the app's own JVM, which renders one page's axioms at a time |
| `MAX_UPLOAD_BYTES` | `20000000000` | The largest upload accepted (20 GB) |
| `PAGE_CACHE_SIZE` | `100` | Reserved for the page cache, which isn't in 0.1.0; it has no effect yet |

Compose itself reads a few more from `.env`, which aren't browl settings:

| Variable | Default | What it does |
| --- | --- | --- |
| `BROWL_PORT` | `8000` | The host port the app is published on. `127.0.0.1:8000` publishes it on the loopback interface only, for a reverse proxy on the same host |
| `FUSEKI_HEAP` | `1g` | Fuseki's heap. Raise it for ontologies larger than NCBITaxon or with large imports |
| `FUSEKI_MEM_LIMIT` | none | A memory limit for Fuseki's container: the heap plus about 0.5 GiB, e.g. `1536m`. Optional; it contains a runaway Fuseki |
| `SANDBOX_MEM_LIMIT` | none | A memory limit for the sandbox's container: `REASONER_MAX_HEAP` plus about 0.5 GiB, e.g. `8704m` |

The rest (`FUSEKI_URL`, `SANDBOX_SOCKET`, `DATA_DIR`, `FUSEKI_BASE`, `ROBOT_JAR`, `JENA_HOME`) have defaults that suit
the images, or are set by `docker-compose.yml`; change them only when deploying another way.
[DESIGN.md §2.12](../DESIGN.md#212-configuration) lists them all.

After changing `.env`, run `docker compose up -d` to recreate the containers with the new settings. **Don't do it while
a load runs**: recreating the app fails the load ("interrupted by restart"). A change to `BASE_URL`,
`RESERVED_PREFIX`, `NAME_PROPERTIES`, `LANGUAGE_RANKING` or `EXTRA_PREFIXES` makes the app rebuild its derived data
from the served ontology at startup, with the site on its "loading" page meanwhile; no upload is needed. Don't restart
the app during that rebuild either: an interrupted rebuild isn't retried, and the site stays unavailable until an
admin uploads the ontology again. The reasoner settings and `MATERIALIZE_PROPERTIES` take effect from the next upload.

## HTTPS and reverse proxies

browl serves plain HTTP on port 8000; put a reverse proxy in front of it for HTTPS (Caddy, nginx, Traefik, or a cloud
load balancer). Set `BASE_URL` to the public `https` address. browl builds its sign-in callback and every link from
`BASE_URL`, not from the request, so the proxy needn't pass forwarding headers, and session cookies are marked `Secure`
when `BASE_URL` is `https`.

Uploads can be many gigabytes, so the proxy must let them through to the upload page, `/_browl/admin/upload`:

- no limit on the request body below `MAX_UPLOAD_BYTES` (browl enforces that limit itself, as the file streams in);
- stream the body to browl rather than buffering it first, so the upload's progress shows and the proxy needs no disk
  for it;
- long enough timeouts for the upload to arrive.

Lift the limits for that path only, so the rest of the site keeps the proxy's defaults. Also redirect `http` to
`https`, and send HSTS, so browsers use `https` from then on. With nginx, for example:

```nginx
server {
    listen 80;
    server_name ontology.example.org;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name ontology.example.org;
    # ssl_certificate …
    add_header Strict-Transport-Security "max-age=31536000" always;

    location / {
        proxy_pass http://127.0.0.1:8000;
    }

    location = /_browl/admin/upload {
        proxy_pass http://127.0.0.1:8000;
        client_max_body_size 0;
        proxy_request_buffering off;
        proxy_send_timeout 1h;
        proxy_read_timeout 1h;
    }
}
```

Caddy redirects to `https` itself and has no body limit by default:
`ontology.example.org { reverse_proxy 127.0.0.1:8000 }`. Add a `header Strict-Transport-Security max-age=31536000`
line for HSTS.

With the proxy on the same host, set `BROWL_PORT=127.0.0.1:8000`, so browl's plain-HTTP port isn't reachable from
other machines: Docker publishes ports on every interface by default, past host firewalls such as ufw and firewalld.

## Upgrading

Check out the new release and rebuild:

```sh
git fetch --tags
git checkout v0.2.0   # the release you're upgrading to
docker compose up -d --build
```

The volumes are kept. If the new release computes its derived data differently, the app rebuilds it from the served
ontology at startup, with the site on its "loading" page meanwhile; that takes about as long as a load's Pre-Cache
stage. Upgrade when no load is running, and don't restart the app during the rebuild ([Settings](#settings) says
why). Read the release notes on the [Releases page](https://github.com/jclerman/browl/releases) before upgrading, and
[back up](#backups) first.

## Backups

The volumes live in Docker's data root (on Docker Desktop, inside its VM), so host backups don't cover them. Back up
`data` and `fuseki`; `jobs` and `sandbox` need none.

**A backup of `data` is a secret.** It holds the key that signs sign-in cookies (`session.key`), with which anyone can
sign in as an admin, and the admins' email addresses. Keep backups outside the git checkout, readable only by you, and
store them as you would a password. (Leaving `session.key` out, with `--exclude=./session.key`, is also fine: browl
makes a new one, and everyone signs in again.)

With the stack stopped, so the files are consistent:

```sh
project=browl                # the Compose project's name
backups="$HOME/browl-backups"  # outside the checkout
mkdir -p -m 700 "$backups"
docker compose -p "$project" stop
for v in data fuseki; do
  docker run --rm -v "${project}_$v":/volume:ro -v "$backups":/backup debian:trixie-slim \
    sh -c "umask 077 && tar -czf /backup/${project}_$v.tar.gz -C /volume ."
done
docker compose -p "$project" start
```

To restore, which **replaces everything on the two volumes**: stop the stack (or create it without starting it,
`docker compose up --no-start`), check both archives, then restore them:

```sh
docker compose -p "$project" stop
docker run --rm -v "$backups":/backup:ro debian:trixie-slim \
  gzip -t "/backup/${project}_data.tar.gz" "/backup/${project}_fuseki.tar.gz" &&
for v in data fuseki; do
  docker run --rm -v "${project}_$v":/volume -v "$backups":/backup:ro debian:trixie-slim \
    sh -c "find /volume -mindepth 1 -delete && tar -xzpf /backup/${project}_$v.tar.gz -C /volume"
done
docker compose -p "$project" start
```

Restore both from the same backup: the job state on `data` names the store on `fuseki`. Instead of backing up
`fuseki`, you can keep the ontology file and upload it again after a restore; the site is unavailable until you do.

## Logs and health

`docker compose logs -f app` follows the app's log, which records each load's stages and any errors; `fuseki` and
`sandbox` log too. Docker keeps the logs until a container is removed, and by default never rotates them, so on a
long-running host, cap them in Docker's `/etc/docker/daemon.json` (then restart Docker):

```json
{
  "log-driver": "json-file",
  "log-opts": {"max-size": "10m", "max-file": "5"}
}
```

`/_browl/health` answers `{"status":"ok","fuseki":"up"}` while the app is up, with `"down"` if Fuseki doesn't answer.
It answers even while the site is loading or unavailable, so use it for uptime checks. Each container has its own
health check, which `docker compose ps` shows.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| The app exits at startup with a validation error | A setting is missing or invalid; the error names it. Without sign-in configured, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` and `SEED_ADMINS` are required |
| The app exits at startup, saying `session.key` is damaged | The session key file was emptied or cut short, perhaps by a bad restore; check the volume's other files too. Delete it (`docker compose run --rm --no-deps app rm /data/session.key`) and start again; everyone must sign in again |
| The log warns that `SANDBOX_SOCKET` is unset | The app runs ROBOT itself, unsandboxed. Expected only outside Compose and the manifests above; don't run like this in production |
| Google says `redirect_uri_mismatch` | The OAuth client doesn't list this deployment's redirect URI exactly, as [Deploying with Docker Compose](#deploying-with-docker-compose), step 2, gives it |
| A load fails, "interrupted by restart" or "interrupted by shutdown" | The app was restarted or recreated during the load. Upload again, and don't restart the app while a load runs |
| The site stays "not available" after a restart, and the status page says the rebuild was interrupted | An interrupted rebuild isn't retried. Upload the ontology again |
| Fuseki restarts during a load or under load, logging `OutOfMemoryError` | Raise `FUSEKI_HEAP`, and `FUSEKI_MEM_LIMIT` with it if set |
| A load fails at reasoning with "memory limit" | Raise `REASONER_MAX_HEAP`, and `SANDBOX_MEM_LIMIT` (or the sandbox's memory limit) with it |
| A load fails at reasoning with "killed" | The sandbox's container limit is below `REASONER_MAX_HEAP` plus about 0.5 GiB; raise it |
| A load fails with "disk space" | The jobs volume filled up: the reasoner's output can be much larger than the upload. Give it more space |
| The site stays on "loading" after an upgrade or a settings change | The app is rebuilding its derived data; the admin status page shows its progress |
