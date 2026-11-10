# Groundskeeper Helm chart

Run [Groundskeeper](https://github.com/Groundskeeper-robot/groundskeeper) on Kubernetes: one Deployment, a Service,
and a PersistentVolumeClaim for its `/data`. Everything site-specific (the robot's address, Home Assistant, Frigate,
imagery) is configured in the panel's **Settings** UI after it starts — not in `values.yaml`.

> Groundskeeper controls a machine with blades and stores the location of a home. Control, map writing, guard
> auto-resume and assistant commands are **off** on a new install; turn them on deliberately in **Settings → Safety**.

## Install

```bash
helm install groundskeeper ./deploy/helm/groundskeeper \
  --namespace groundskeeper --create-namespace
```

Reach the panel (default `ClusterIP`):

```bash
kubectl -n groundskeeper port-forward svc/groundskeeper 8425:8425
# open http://127.0.0.1:8425
```

## Values

| Key | Default | Description |
|---|---|---|
| `image.repository` | `ghcr.io/groundskeeper-robot/groundskeeper` | Image repository |
| `image.tag` | `""` (chart `appVersion`) | Image tag |
| `port` | `8425` | Panel HTTP port (container + service) |
| `puid` / `pgid` | `1000` | uid/gid the panel runs as; owns `/data` |
| `env` | `{}` | Extra plain env vars (secrets go via `envFrom`) |
| `envFrom` | `[]` | Env from Secrets/ConfigMaps (e.g. a Home Assistant token) |
| `persistence.enabled` | `true` | Create a PVC for `/data` |
| `persistence.storageClass` | `""` | `""` = cluster default |
| `persistence.size` | `2Gi` | PVC size |
| `persistence.existingClaim` | `""` | Use an existing PVC instead |
| `service.type` | `ClusterIP` | Service type |
| `service.port` | `8425` | Service port |
| `hostNetwork` | `false` | Only needed for a WeatherFlow Tempest UDP broadcast (:50222) |
| `ingress.enabled` | `false` | Create an Ingress |
| `resources` | `{}` | Container resources |
| `podSecurityContext` / `securityContext` | `{}` | See below |
| `nodeSelector` / `tolerations` / `affinity` | — | Scheduling |

## Running non-root

By default the image starts as root and drops to `puid:pgid` via its entrypoint (the same as the upstream
docker-compose), handing `/data` to that user once. To run fully non-root instead:

```yaml
podSecurityContext:
  runAsUser: 1000
  runAsGroup: 1000
  fsGroup: 1000
```

The entrypoint then skips the chown and runs directly as that user; `fsGroup` makes the PVC group-writable.

## Notes

- The panel needs network access to the robot's local broker (and/or the cloud); that is reached from the pod and
  configured in the UI.
- `hostNetwork` is only for receiving a Tempest weather station's UDP broadcast. Leave it off otherwise; the panel
  is reachable through the Service regardless.
