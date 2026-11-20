# Devcontainer

A Frappe bench with ERPNext, Payments and kefiya, for developing and testing
kefiya locally. Based on the frappe_docker devcontainer: MariaDB 11.8, two
Redis containers and the `frappe/bench` image (Python 3.14, Node 24).

## Layout

| Path in the container                     | What                                             |
| ----------------------------------------- | ------------------------------------------------ |
| `/workspace/kefiya`                       | This repository (bind mount); VS Code opens it   |
| `/workspace/development/frappe-bench`     | The bench, in the Docker volume `bench-data`     |
| `.../frappe-bench/apps/kefiya`            | Symlink to `/workspace/kefiya`, so edits are live |

The bench is in a volume and not in the repository: on macOS a bind mount is
slow for `env/` and `node_modules/`, and it survives rebuilding the container.

## First start

Give Docker at least 8 GB of memory (Docker Desktop: Settings, Resources).
With 4 GB, `bench build --app frappe` is killed while it builds Frappe Studio.

1. Open the repository in VS Code and run **Dev Containers: Reopen in Container**.
2. In the container terminal, set up the bench (about 10 to 20 minutes):

   ```bash
   bash .devcontainer/setup-bench.sh
   ```

3. Start it:

   ```bash
   cd /workspace/development/frappe-bench && bench start
   ```

4. Open http://localhost:8000 and log in as `Administrator` / `admin`.

Two sites, both with ERPNext, Payments and kefiya:

- `kefiya.localhost`: for development. The setup wizard is done (company
  "Kefiya Dev GmbH", Germany, EUR), developer mode and the scheduler are on.
- `test.localhost`: for the tests. The setup wizard must not run on it.

MariaDB root password: `123`.

## Tests

```bash
cd /workspace/development/frappe-bench
bench --site test.localhost run-tests --app kefiya
```

## Without VS Code

```bash
docker compose -f .devcontainer/docker-compose.yml -p kefiya_devcontainer up -d
docker compose -f .devcontainer/docker-compose.yml -p kefiya_devcontainer exec -u frappe frappe bash
sudo chown frappe:frappe /workspace/development   # once, as postCreateCommand does
```
