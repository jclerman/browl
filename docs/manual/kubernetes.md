# Deploying on Kubernetes

For operators deploying browl on Kubernetes, or on another container orchestrator. Read
[Operating browl](operator.md) first: its settings, reverse proxy advice and troubleshooting apply here too.

**Contents:**

- [What the platform must provide](#what-the-platform-must-provide)
- [Images](#images)
- [Manifests](#manifests)
- [Checking the isolation](#checking-the-isolation)
- [Upgrading](#upgrading)
- [Logs](#logs)
- [Troubleshooting](#troubleshooting)

## What the platform must provide

browl's CI tests only the Compose deployment, so treat this page, and its manifests, as a starting point: check them
against your cluster. Any platform must provide what Compose does:

1. **One replica of each container, never two.** browl keeps its state in SQLite and runs one worker process; a
   second app replica would corrupt its job state. Use `replicas: 1` and the `Recreate` strategy, so an old pod is gone
   before its replacement starts.
2. **Shared volumes.** The app and Fuseki share the `fuseki` volume, and the app and sandbox share `jobs` and
   `sandbox` (a Unix socket, which works only on a local file system). The simplest arrangement that keeps the sandbox
   isolated: the app and Fuseki in one pod, the sandbox in a second pod, both pinned to one node, sharing
   `ReadWriteOnce` volumes. `ReadWriteOnce` admits several pods on one node; `ReadWriteOncePod` doesn't, so don't use
   it for `jobs` or `sandbox`. Pin both pods to the node with a label: without that, a recreated app pod can land on
   another node and wait forever for volumes the sandbox's pod holds ("Multi-Attach error").
3. **No network for the sandbox.** Containers in one pod share its network, so the sandbox can't be a container in the
   app's pod. Give its pod a NetworkPolicy that denies all ingress and egress, which needs a network plugin that
   enforces NetworkPolicy (Calico, Cilium, …); some clusters' defaults don't, and then ignore the policy without an
   error. Without one, the sandbox isn't isolated; don't run it so in production. [Check it](#checking-the-isolation)
   once deployed.
4. **Fuseki reachable from the app only.** In one pod, the app reaches it at `http://localhost:3030`. Set
   `FUSEKI_HOST=127.0.0.1` on Fuseki's container, so it listens only inside the pod, whatever the network plugin; the
   pod's NetworkPolicy, which admits ingress only to port 8000, is a second line of defense.
5. **uid 10001 throughout.** The app writes stores that Fuseki opens and deletes, and the sandbox's socket admits only
   its own user. Set `runAsUser`, `runAsGroup` and `fsGroup` to 10001, so new volumes are writable.
6. **The sandbox's other restrictions**: a read-only root file system with a 256 MiB in-memory `/tmp`, no capabilities,
   no privilege escalation, no service account token and no service links. Compose's `init: true` reaps the tools'
   orphaned processes; `shareProcessNamespace: true` does the same here, since the pod's pause process is then PID 1.
   Compose also caps its processes and open files; in Kubernetes, the process limit is the kubelet's `podPidsLimit`,
   and the open-files limit the container runtime's, so the manifests can't set either. Kubernetes still sets
   `KUBERNETES_SERVICE_HOST` and `_PORT` in the sandbox, which is harmless: the tools run with a filtered environment,
   and the sandbox has no network.
7. **No Kubernetes API access for the app either.** The app parses uploaded ontologies, so give its pod no service
   account token.
8. **Probes that don't restart a container mid-load.** A restart fails a running load, and an interrupted rebuild
   isn't retried. Give probes Compose's 5 s timeout and room for a busy moment.
9. **A reverse proxy that admits large uploads**, as in [HTTPS and reverse proxies](operator.md#https-and-reverse-proxies).

## Images

Build the images from the release and push them to a registry your cluster can pull from:

```sh
git checkout v0.1.0
docker build -t registry.example.org/browl-app:0.1.0 .
docker build -t registry.example.org/browl-fuseki:0.1.0 docker/fuseki
docker push registry.example.org/browl-app:0.1.0
docker push registry.example.org/browl-fuseki:0.1.0
```

Build on, or for (`docker buildx build --platform …`), your nodes' architecture.

## Manifests

The settings, then the volumes, the two pods, their network policies, and the service and ingress. Sizes and memory
figures are placeholders, from the defaults in [Requirements](operator.md#requirements); set them for your ontology.
Fuseki runs as a native sidecar (Kubernetes 1.29 or later), so it's answering before the app starts, as Compose's
`depends_on` ensures.

First label the node both pods run on (and keep the OIDC secret out of any file you commit):

```sh
kubectl label node <node-name> browl.example.org/node=true
kubectl create secret generic browl-secrets \
  --from-literal=OIDC_CLIENT_ID=1234567890-abc.apps.googleusercontent.com \
  --from-literal=OIDC_CLIENT_SECRET='GOCSPX-…' \
  --from-literal=SEED_ADMINS='["you@example.org"]'
```

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
      nodeSelector: {browl.example.org/node: "true"}
      automountServiceAccountToken: false
      enableServiceLinks: false
      securityContext:
        runAsUser: 10001
        runAsGroup: 10001
        fsGroup: 10001
        fsGroupChangePolicy: OnRootMismatch # else a recursive chown of the store at every start
        runAsNonRoot: true
        seccompProfile: {type: RuntimeDefault}
      initContainers:
        - name: fuseki
          image: registry.example.org/browl-fuseki:0.1.0
          restartPolicy: Always # a native sidecar: started, and up, before the app
          env:
            - name: JDK_JAVA_OPTIONS
              value: -Xmx1g -XX:MaxDirectMemorySize=128m
            - name: FUSEKI_HOST # only the pod's own containers can reach it
              value: 127.0.0.1
          resources:
            requests: {memory: 1536Mi}
            limits: {memory: 1536Mi}
          volumeMounts:
            - {name: fuseki, mountPath: /fuseki}
          # Probes run in the container, as Fuseki listens only on 127.0.0.1.
          startupProbe:
            exec: {command: [curl, -fsS, "http://127.0.0.1:3030/$/ping"]}
            periodSeconds: 2
            timeoutSeconds: 5
            failureThreshold: 60
          livenessProbe:
            exec: {command: [curl, -fsS, "http://127.0.0.1:3030/$/ping"]}
            periodSeconds: 30
            timeoutSeconds: 5
            failureThreshold: 6
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
            timeoutSeconds: 5
            failureThreshold: 60
          livenessProbe:
            httpGet: {path: /_browl/health, port: http}
            periodSeconds: 30
            timeoutSeconds: 5
            failureThreshold: 6
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
      nodeSelector: {browl.example.org/node: "true"}
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
          # Readiness only: a liveness failure would restart the runner, failing a load's reasoning.
          readinessProbe:
            exec:
              command: [python, -c, "import socket; socket.socket(socket.AF_UNIX).connect('/sandbox/runner.sock')"]
            periodSeconds: 30
            timeoutSeconds: 5
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
# For ingress-nginx, which redirects HTTP to HTTPS and sends HSTS by default; other controllers have their own ways
# to lift the body limit and buffering. The site at large keeps the controller's default body limit.
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: browl
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
---
# Uploads only: no body limit (the app enforces MAX_UPLOAD_BYTES), streamed, with long timeouts.
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: browl-upload
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
          - path: /_browl/admin/upload
            pathType: Exact
            backend:
              service: {name: browl, port: {name: http}}
  tls:
    - hosts: [ontology.example.org]
      secretName: browl-tls
```

Notes:

- Until the sandbox is up, a load that needs it (a reasoner, or an OWL Functional Syntax file or import) fails;
  browsing doesn't need it.
- To move browl to another node, scale both Deployments to 0, move the label, and scale them back to 1.
- Back up the `browl-data` and `browl-fuseki` volumes with your cluster's volume snapshots or backup tool, ideally with
  the app scaled to 0. As in [Backups](operator.md#backups), a backup of `browl-data` holds the session key: store it
  as a secret.

## Checking the isolation

A NetworkPolicy that isn't enforced fails silently, so check the sandbox once deployed. This must fail, with a timeout
or an unreachable network:

```sh
kubectl exec deploy/browl-sandbox -- \
  python -c "import socket; socket.create_connection(('1.1.1.1', 443), 3)"
```

If it connects, the sandbox has network access: enable NetworkPolicy enforcement in your cluster before uploading
ontologies.

## Upgrading

Build and push the new release's images, as in [Images](#images), with its version as the tag. Then, when no load is
running, set both Deployments' images to it:

```sh
kubectl set image deploy/browl fuseki=registry.example.org/browl-fuseki:0.2.0 app=registry.example.org/browl-app:0.2.0
kubectl set image deploy/browl-sandbox sandbox=registry.example.org/browl-app:0.2.0
```

The `Recreate` strategy stops each old pod before its replacement starts. As with Compose
([Upgrading](operator.md#upgrading)), the app may rebuild its derived data at startup; don't restart it meanwhile.

## Logs

```sh
kubectl logs deploy/browl -c app        # the app: each load's stages, and errors
kubectl logs deploy/browl -c fuseki
kubectl logs deploy/browl-sandbox
```

The app's `/_browl/health`, which its probes use, also suits uptime checks
([Logs and health](operator.md#logs-and-health)).

## Troubleshooting

[Operating browl](operator.md#troubleshooting) covers most problems. Specific to Kubernetes:

| Symptom | Cause and fix |
| --- | --- |
| The app's pod hangs in `ContainerCreating` with a "Multi-Attach error" | The app's pod and the sandbox's are on different nodes. Pin both to one node, as the manifests do |
| The isolation check connects | The cluster doesn't enforce NetworkPolicy. Enable it (e.g. Calico or Cilium) before uploading ontologies |
| A load fails: "the tool sandbox isn't available" | The sandbox's pod isn't running, or isn't on the app's node. Check with `kubectl get pods -o wide` |
