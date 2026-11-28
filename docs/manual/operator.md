# Operating browl

For operators: deploying browl, configuring it, and keeping it running. [Docker Compose](#deploying-with-docker-compose)
is the supported deployment; [Kubernetes](#deploying-on-kubernetes) describes what any other platform must provide,
with example manifests.

**Contents:**

- [What you're deploying](#what-youre-deploying)
- [Requirements](#requirements)
- [Deploying with Docker Compose](#deploying-with-docker-compose)
- [Settings](#settings)
- [HTTPS and reverse proxies](#https-and-reverse-proxies)
- [Upgrading](#upgrading)
- [Backups](#backups)
- [Logs and health](#logs-and-health)
- [Deploying on Kubernetes](#deploying-on-kubernetes)
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
- **Memory**, which depends on the ontology:

  | Container | Needs | Default settings |
  | --- | --- | --- |
  | fuseki | Its heap (`FUSEKI_HEAP`) plus about 0.5 GiB. NCBITaxon (2.6 M classes) needs about 500 MiB of heap | 1.5 GiB |
  | sandbox | `REASONER_MAX_HEAP` plus about 0.5 GiB, only while a reasoner runs. ELK needs about 1 GB for Uberon and 16 GB for NCBITaxon | 8.5 GiB while reasoning |
  | app | Its JVM's heap (`APP_JVM_MAX_HEAP`, 1 GB) plus the Python process, which builds the derived data after each load | Not yet measured; watch `docker stats` during your ontology's first load |

  Fuseki's store is memory-mapped, so spare memory makes pages faster.
- **Disk**, roughly: the upload (up to 20 GB by default) and the reasoner's output, which can be much larger, on the
  jobs volume, with 1 GB to spare; the TDB2 store, a few times the ontology's Turtle size (2.4 GB for NCBITaxon), on
  the fuseki volume, twice over while a new upload replaces it; and the derived databases on the data volume.

## Deploying with Docker Compose

browl doesn't publish images yet; Compose builds them from the source.

1. **Get the source** at the release you want:

   ```sh
   git clone https://github.com/jclerman/browl.git
   cd browl
   git checkout v0.1.0
   ```

2. **Register Google sign-in** as [`AUTHENTICATION.md`](../AUTHENTICATION.md) says. The redirect URI to register is
   your `BASE_URL` without its trailing `/`, then `/_browl/admin/oidc-callback`.

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
   # and remove AUTH_DISABLED=true
   ```

   `.env` holds a secret; keep it readable only by you (`chmod 600 .env`). For a local trial, the example as it is
   (`AUTH_DISABLED=true`, everyone an admin) works without Google.

4. **Start it:**

   ```sh
   docker compose up -d --build
   ```

   The first build downloads Java, ROBOT, Jena and Fuseki, and takes a few minutes. `docker compose ps` shows the
   three services as `healthy` once they're up.

5. **Check it** at `BASE_URL`: with no ontology loaded, the site says it's not available. Sign in from the "Admin
   sign-in" link (or open `/_browl/admin/upload`) and upload an ontology ([Administering browl](admin.md)).

`docker compose stop` and `start` stop and restart the containers; `down` also removes them, but keeps the volumes.
`docker compose down -v` **deletes the volumes**, and with them the ontology and the admin list.

Compose's project name, `browl`, prefixes the volumes (`browl_data`, …). To run a second deployment on one host, use
another directory or `docker compose -p <name>`, and another `BROWL_PORT`.

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

The rest (`FUSEKI_URL`, `SANDBOX_SOCKET`, `DATA_DIR`, `FUSEKI_BASE`, `ROBOT_JAR`, `JENA_HOME`) are set by the image and
`docker-compose.yml`; change them only when deploying another way. [DESIGN.md §2.12](../DESIGN.md#212-configuration)
lists them all.

After changing `.env`, run `docker compose up -d` to recreate the containers with the new settings. A change to
`NAME_PROPERTIES`, `LANGUAGE_RANKING` or `EXTRA_PREFIXES` makes the app rebuild its derived data from the served
ontology at startup, with the site on its "loading" page meanwhile; no upload is needed.

## HTTPS and reverse proxies

browl serves plain HTTP on port 8000; put a reverse proxy in front of it for HTTPS (Caddy, nginx, Traefik, or a cloud
load balancer). Set `BASE_URL` to the public `https` address. browl builds its sign-in callback and every link from
`BASE_URL`, not from the request, so the proxy needn't pass forwarding headers, and session cookies are marked `Secure`
when `BASE_URL` is `https`.

Uploads can be many gigabytes, so the proxy must let them through:

- no limit on the request body below `MAX_UPLOAD_BYTES`;
- stream the body to browl rather than buffering it first, so the upload's progress shows and the proxy needs no disk
  for it;
- long enough timeouts for the upload to arrive.

With nginx, for example:

```nginx
server {
    listen 443 ssl;
    server_name ontology.example.org;
    # ssl_certificate …

    location / {
        proxy_pass http://127.0.0.1:8000;
        client_max_body_size 0;
        proxy_request_buffering off;
        proxy_send_timeout 1h;
        proxy_read_timeout 1h;
    }
}
```

Caddy needs no extra settings: `ontology.example.org { reverse_proxy 127.0.0.1:8000 }`.

## Upgrading

Check out the new release and rebuild:

```sh
git fetch --tags
git checkout v0.2.0   # the release you're upgrading to
docker compose up -d --build
```

The volumes are kept. If the new release computes its derived data differently, the app rebuilds it from the served
ontology at startup, with the site on its "loading" page meanwhile; that takes about as long as a load's last stage.
Read the release notes before upgrading; back up first.

## Backups

The volumes live in Docker's data root (on Docker Desktop, inside its VM), so host backups don't cover them. Back up
`data` and `fuseki`; `jobs` and `sandbox` need none. With the stack stopped, so the files are consistent:

```sh
docker compose stop
for v in data fuseki; do
  docker run --rm -v browl_$v:/volume:ro -v "$PWD":/backup debian:trixie-slim \
    tar -czf /backup/browl_$v.tar.gz -C /volume .
done
docker compose start
```

To restore, into a stack whose containers exist but are stopped (`docker compose up --no-start`, or `stop`):

```sh
for v in data fuseki; do
  docker run --rm -v browl_$v:/volume -v "$PWD":/backup debian:trixie-slim \
    sh -c "find /volume -mindepth 1 -delete && tar -xzpf /backup/browl_$v.tar.gz -C /volume"
done
docker compose start
```

Restore both from the same backup: the job state on `data` names the store on `fuseki`. Instead of backing up
`fuseki`, you can keep the ontology file and upload it again after a restore; the site is unavailable until you do.

## Logs and health

`docker compose logs -f app` follows the app's log, which records each load's stages and any errors; `fuseki` and
`sandbox` log too. Docker keeps the logs until a container is removed.

`/_browl/health` answers `{"status":"ok","fuseki":"up"}` while the app is up, with `"down"` if Fuseki doesn't answer.
It answers even while the site is loading or unavailable, so use it for uptime checks. Each container has its own
health check, which `docker compose ps` shows.

## Deploying on Kubernetes

browl's CI tests only the Compose deployment, so treat this section, and its manifests, as a starting point: check
them against your cluster. Any platform must provide what Compose does:

1. **One replica of each container, never two.** browl keeps its state in SQLite and runs one worker process; a
   second app replica would corrupt its job state. Use `replicas: 1` and the `Recreate` strategy, so an old pod is gone
   before its replacement starts.
2. **Shared volumes.** The app and Fuseki share the `fuseki` volume, and the app and sandbox share `jobs` and
   `sandbox` (a Unix socket, which works only on a local file system). The simplest arrangement that keeps the sandbox
   isolated: the app and Fuseki in one pod, the sandbox in a second pod scheduled on the same node, sharing
   `ReadWriteOnce` volumes. `ReadWriteOnce` admits several pods on one node; `ReadWriteOncePod` doesn't, so don't use
   it for `jobs` or `sandbox`.
3. **No network for the sandbox.** Pods in one pod share its network, so the sandbox can't be a container in the app's
   pod. Give its pod a NetworkPolicy that denies all ingress and egress, which needs a network plugin that enforces
   NetworkPolicy (Calico, Cilium, …). Without one, the sandbox isn't isolated; don't run it so in production.
4. **Fuseki reachable from the app only.** In one pod, the app reaches it at `http://localhost:3030`. Fuseki still
   listens on the pod's address, so the pod's NetworkPolicy admits ingress only to port 8000.
5. **uid 10001 throughout.** The app writes stores that Fuseki opens and deletes, and the sandbox's socket admits only
   its own user. Set `runAsUser`, `runAsGroup` and `fsGroup` to 10001, so new volumes are writable.
6. **The sandbox's other restrictions**: a read-only root file system with a 256 MiB in-memory `/tmp`, no capabilities,
   no privilege escalation, no service account token and no service environment variables. Compose's `init: true`
   reaps the tools' orphaned processes; `shareProcessNamespace: true` does the same here, since the pod's pause
   process is then PID 1. Compose also caps its processes and open files; in Kubernetes, the process limit is the
   kubelet's `podPidsLimit`.
7. **A reverse proxy that admits large uploads**, as [above](#https-and-reverse-proxies).

### Images

Build the images from the release and push them to a registry your cluster can pull from:

```sh
git checkout v0.1.0
docker build -t registry.example.org/browl-app:0.1.0 .
docker build -t registry.example.org/browl-fuseki:0.1.0 docker/fuseki
docker push registry.example.org/browl-app:0.1.0
docker push registry.example.org/browl-fuseki:0.1.0
```

Build on, or for (`docker buildx build --platform …`), your nodes' architecture.

### Manifests

The settings, then the volumes, the two pods, their network policies, and the service and ingress. Sizes and memory
figures are placeholders, from the defaults above; set them for your ontology. Fuseki runs as a native sidecar
(Kubernetes 1.29 or later), so it's answering before the app starts, as Compose's `depends_on` ensures.

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: browl-config
data:
  BASE_URL: https://ontology.example.org/
  # Any other settings, e.g.
  # NAME_PROPERTIES: '["rdfs:label", "skos:prefLabel"]'
---
apiVersion: v1
kind: Secret
metadata:
  name: browl-secrets
stringData:
  OIDC_CLIENT_ID: 1234567890-abc.apps.googleusercontent.com
  OIDC_CLIENT_SECRET: GOCSPX-…
  SEED_ADMINS: '["you@example.org"]'
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: browl-data
spec:
  accessModes: [ReadWriteOnce]
  resources: {requests: {storage: 20Gi}}
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: browl-fuseki
spec:
  accessModes: [ReadWriteOnce]
  resources: {requests: {storage: 50Gi}}
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: browl-jobs
spec:
  accessModes: [ReadWriteOnce]
  resources: {requests: {storage: 50Gi}}
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: browl-sandbox
spec:
  accessModes: [ReadWriteOnce]
  resources: {requests: {storage: 1Gi}}
---
# The app, with Fuseki beside it.
apiVersion: apps/v1
kind: Deployment
metadata:
  name: browl
spec:
  replicas: 1
  strategy: {type: Recreate}
  selector:
    matchLabels: {app.kubernetes.io/name: browl, app.kubernetes.io/component: app}
  template:
    metadata:
      labels: {app.kubernetes.io/name: browl, app.kubernetes.io/component: app}
    spec:
      securityContext:
        runAsUser: 10001
        runAsGroup: 10001
        fsGroup: 10001
        runAsNonRoot: true
        seccompProfile: {type: RuntimeDefault}
      initContainers:
        - name: fuseki
          image: registry.example.org/browl-fuseki:0.1.0
          restartPolicy: Always # a native sidecar: started, and up, before the app
          env:
            - name: JDK_JAVA_OPTIONS
              value: -Xmx1g -XX:MaxDirectMemorySize=128m
          resources:
            requests: {memory: 1536Mi}
            limits: {memory: 1536Mi}
          volumeMounts:
            - {name: fuseki, mountPath: /fuseki}
          startupProbe:
            httpGet: {path: /$/ping, port: 3030}
            periodSeconds: 2
            failureThreshold: 60
          livenessProbe:
            httpGet: {path: /$/ping, port: 3030}
            periodSeconds: 30
      containers:
        - name: app
          image: registry.example.org/browl-app:0.1.0
          ports:
            - {name: http, containerPort: 8000}
          envFrom:
            - configMapRef: {name: browl-config}
            - secretRef: {name: browl-secrets}
          env:
            - {name: FUSEKI_URL, value: "http://localhost:3030"}
            - {name: SANDBOX_SOCKET, value: /sandbox/runner.sock}
          resources:
            requests: {memory: 2Gi}
          volumeMounts:
            - {name: data, mountPath: /data}
            - {name: jobs, mountPath: /data/uploads}
            - {name: sandbox, mountPath: /sandbox}
            - {name: fuseki, mountPath: /fuseki}
          startupProbe:
            httpGet: {path: /_browl/health, port: http}
            periodSeconds: 2
            failureThreshold: 60
          livenessProbe:
            httpGet: {path: /_browl/health, port: http}
            periodSeconds: 30
            timeoutSeconds: 5
      volumes:
        - {name: data, persistentVolumeClaim: {claimName: browl-data}}
        - {name: jobs, persistentVolumeClaim: {claimName: browl-jobs}}
        - {name: sandbox, persistentVolumeClaim: {claimName: browl-sandbox}}
        - {name: fuseki, persistentVolumeClaim: {claimName: browl-fuseki}}
---
# The sandbox: on the app's node, to share its volumes; no network (the NetworkPolicy below).
apiVersion: apps/v1
kind: Deployment
metadata:
  name: browl-sandbox
spec:
  replicas: 1
  strategy: {type: Recreate}
  selector:
    matchLabels: {app.kubernetes.io/name: browl, app.kubernetes.io/component: sandbox}
  template:
    metadata:
      labels: {app.kubernetes.io/name: browl, app.kubernetes.io/component: sandbox}
    spec:
      affinity:
        podAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
            - topologyKey: kubernetes.io/hostname
              labelSelector:
                matchLabels: {app.kubernetes.io/name: browl, app.kubernetes.io/component: app}
      automountServiceAccountToken: false
      enableServiceLinks: false
      shareProcessNamespace: true # the pause process reaps orphaned tool processes
      securityContext:
        runAsUser: 10001
        runAsGroup: 10001
        fsGroup: 10001
        runAsNonRoot: true
        seccompProfile: {type: RuntimeDefault}
      containers:
        - name: sandbox
          image: registry.example.org/browl-app:0.1.0
          command: [python, -m, browl.sandbox, /sandbox/runner.sock]
          workingDir: /tmp
          env:
            - {name: PYTHONDONTWRITEBYTECODE, value: "1"}
          securityContext:
            readOnlyRootFilesystem: true
            allowPrivilegeEscalation: false
            capabilities: {drop: [ALL]}
          resources:
            # REASONER_MAX_HEAP plus about 0.5 GiB.
            requests: {memory: 8704Mi}
            limits: {memory: 8704Mi}
          volumeMounts:
            - {name: jobs, mountPath: /data/uploads}
            - {name: sandbox, mountPath: /sandbox}
            - {name: tmp, mountPath: /tmp}
          readinessProbe: &socket-probe
            exec:
              command: [python, -c, "import socket; socket.socket(socket.AF_UNIX).connect('/sandbox/runner.sock')"]
            periodSeconds: 30
          livenessProbe: *socket-probe
      volumes:
        - {name: jobs, persistentVolumeClaim: {claimName: browl-jobs}}
        - {name: sandbox, persistentVolumeClaim: {claimName: browl-sandbox}}
        - {name: tmp, emptyDir: {medium: Memory, sizeLimit: 256Mi}}
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: browl-sandbox-isolated
spec:
  podSelector:
    matchLabels: {app.kubernetes.io/name: browl, app.kubernetes.io/component: sandbox}
  policyTypes: [Ingress, Egress] # no rules: nothing in, nothing out, not even DNS
---
# Only the website's port is reachable; Fuseki's isn't. Egress stays open, to fetch imports and for Google sign-in.
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: browl-app-ingress
spec:
  podSelector:
    matchLabels: {app.kubernetes.io/name: browl, app.kubernetes.io/component: app}
  policyTypes: [Ingress]
  ingress:
    - ports:
        - {port: 8000}
---
apiVersion: v1
kind: Service
metadata:
  name: browl
spec:
  selector: {app.kubernetes.io/name: browl, app.kubernetes.io/component: app}
  ports:
    - {name: http, port: 80, targetPort: http}
---
# For ingress-nginx; other controllers have their own ways to lift the body limit and buffering.
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: browl
  annotations:
    nginx.ingress.kubernetes.io/proxy-body-size: "0"
    nginx.ingress.kubernetes.io/proxy-request-buffering: "off"
    nginx.ingress.kubernetes.io/proxy-send-timeout: "3600"
    nginx.ingress.kubernetes.io/proxy-read-timeout: "3600"
spec:
  rules:
    - host: ontology.example.org
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service: {name: browl, port: {name: http}}
  tls:
    - hosts: [ontology.example.org]
      secretName: browl-tls
```

Notes:

- The sandbox's pod is scheduled only once the app's is running, onto its node. Until the sandbox is up, a load that
  needs it (a reasoner, or an OWL Functional Syntax file) fails; browsing doesn't need it.
- If the app's pod moves to another node, the sandbox's must follow: delete it, and it's rescheduled beside the app.
  Volumes that can attach only in one zone keep both pods in it.
- Back up the `browl-data` and `browl-fuseki` volumes, as [above](#backups), with your cluster's volume snapshots or
  backup tool, ideally with the app scaled to 0.

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| The app exits at startup with a validation error | A setting is missing or invalid; the error names it. Without sign-in configured, `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` and `SEED_ADMINS` are required |
| The app exits at startup, saying `session.key` is damaged | The session key file was emptied or cut short, perhaps by a bad restore; check the volume's other files too. Delete it (`docker compose run --rm --no-deps app rm /data/session.key`) and start again; everyone must sign in again |
| The log warns that `SANDBOX_SOCKET` is unset | The app runs ROBOT itself, unsandboxed. Expected only outside Compose and the manifests above; don't run like this in production |
| Google says `redirect_uri_mismatch` | The OAuth client doesn't list this deployment's callback exactly: `BASE_URL` without its trailing `/`, then `/_browl/admin/oidc-callback` ([`AUTHENTICATION.md`](../AUTHENTICATION.md)) |
| Fuseki restarts during a load or under load, logging `OutOfMemoryError` | Raise `FUSEKI_HEAP`, and `FUSEKI_MEM_LIMIT` with it if set |
| A load fails at reasoning with "memory limit" | Raise `REASONER_MAX_HEAP`, and `SANDBOX_MEM_LIMIT` (or the sandbox's memory limit) with it |
| A load fails at reasoning with "killed" | The sandbox's container limit is below `REASONER_MAX_HEAP` plus about 0.5 GiB; raise it |
| A load fails with "disk space" | The jobs volume filled up: the reasoner's output can be much larger than the upload. Give it more space |
| The site stays on "loading" after an upgrade or a settings change | The app is rebuilding its derived data; the admin status page shows its progress |
