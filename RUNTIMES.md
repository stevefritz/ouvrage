# Ouvrage Worker Runtimes

Reference for what's available inside `foreman:full` / `foreman:eyes` (images built from `Dockerfile.runtimes`). CC workers run as the `switchboard` user inside `/work/<project>/<worktree>`.

## Quick reference

| Runtime | Versions | Default | How to select a non-default version |
|---|---|---|---|
| Python | 3.11, 3.12, 3.13 | system `python3` → Ubuntu 3.12 | Call `python3.11` / `python3.12` / `python3.13` explicitly. Do **not** repoint `python3` — apt tooling depends on it. |
| Node.js | 18, 20, 22 | 22 | `fnm use 20` inside the project dir, or `fnm exec --using=18 node ...`. |
| PHP | 8.2, 8.3, 8.4 | 8.4 | Call `php8.2` / `php8.3` / `php8.4` explicitly, or `update-alternatives --set php /usr/bin/php8.3` for the session. |
| Go | 1.22.12 | — | Only one version. `go`, `gofmt` on PATH. |
| Ruby | 3.3.6 | — | Only one version. Managed by rbenv — `rbenv versions` to list. |
| Rust | stable | — | `rustup toolchain install nightly` if a project needs it. |

## Package managers pre-installed

- **Python:** `pip`, `poetry`, `uv` (installed into 3.13)
- **Node:** `npm`, `npx`, `yarn`, `pnpm`
- **PHP:** `composer` at `/usr/local/bin/composer`
- **Ruby:** `bundler` (via `gem install bundler` on 3.3.6)
- **Rust:** `cargo`

## Paths

| Thing | Path |
|---|---|
| fnm (Node manager) | `/usr/local/fnm` |
| Go install | `/usr/local/go` |
| rbenv | `/usr/local/rbenv` (root + shims on PATH) |
| Rust / cargo | `/home/switchboard/.cargo` (symlinked to `/usr/local/bin`) |
| Claude CLI | `/usr/local/bin/claude` |
| App code (locked) | `/app` — `750 root:svc-only`, worker user has **no access** |
| Worktrees | `/work/<project>/<branch>` |
| DB + OAuth key | `/data/` (switchboard-svc only) |
| Temp files | `/work/.tmp` (TMPDIR redirected here to keep writable layer small) |

## Tools on PATH

`git`, `curl`, `wget`, `jq`, `unzip`, `rg` (ripgrep), `fd`, `tmpreaper`, build tools (`gcc`, `g++`, `make`, `cmake`, `pkg-config`, `autoconf`, `automake`, `libtool`).

## System libraries pre-installed

Grouped by purpose — enough for most native-extension builds without extra `apt install`:

- **SSL / crypto:** `libssl-dev`, `libffi-dev`
- **XML / text:** `libxml2-dev`, `libxslt-dev`, `libonig-dev`
- **Databases:** `libpq-dev`, `libmysqlclient-dev`, `libsqlite3-dev`
- **Images:** `libjpeg-dev`, `libpng-dev`, `libwebp-dev`, `libfreetype-dev`, `libgd-dev`, `libmagickwand-dev`
- **Compression:** `libzip-dev`, `libbz2-dev`, `zlib1g-dev`
- **i18n:** `libicu-dev`
- **Network:** `libcurl4-openssl-dev`
- **Ruby:** `libyaml-dev`
- **Memcached:** `libmemcached-dev`

## PHP extensions pre-installed (all three versions)

`mbstring`, `xml`, `curl`, `zip`, `gd`, `intl`, `bcmath`, `mysql`, `pgsql`, `sqlite3`, `redis`, `apcu`, `memcached`, `imagick`, `soap`, `xdebug`, plus the `php-common` bundle (`pdo`, `tokenizer`, `ctype`, `fileinfo`, `dom`, `simplexml`, `xmlwriter`, `iconv`, `calendar`, `gettext`, `exif`, `pcntl`, `posix`, `sockets`, `opcache`).

## Playwright (eyes/full images with `WITH_PLAYWRIGHT=true`)

- `playwright` Python package + npm bindings
- Chromium browser installed with system deps via `npx playwright install --with-deps chromium`
- Use for dashboard/UI verification tasks. See `scripts/visual-check.py`.

## Gotchas

- **Don't repoint `python3`.** System tools (`add-apt-repository`, `apt_pkg`) rely on the Ubuntu-packaged Python. Always call `python3.13` (or whichever) explicitly.
- **Node default is 22.** If a project's `package.json` has `"engines": { "node": "20.x" }`, run `fnm use 20` before `npm install` — otherwise native modules will get built against 22's headers.
- **PHP default is 8.4.** Legacy apps may need `update-alternatives --set php /usr/bin/php8.2`.
- **Rust lives in `/home/switchboard/.cargo`.** If a task needs a new toolchain component, `rustup` works as normal — state persists inside the container for the session.
- **`/app` is not readable by the worker user.** If a task needs to reference switchboard's own code, it's running on the wrong machine — CC workers should only touch files inside `/work/...`.
- **`/work/.tmp` is bind-mounted** (not writable layer) — tmpreaper sweeps files older than 2h hourly. Don't rely on `/tmp` for anything that must survive.

## Adding a new runtime version

Edit `Dockerfile.runtimes`, rebuild, retag. Keep the layer ordering (system libs → runtimes → app code) so day-to-day app deploys only rebuild from the `COPY` layer onward.

```bash
cd /root/mcp-switchboard
docker build -f Dockerfile.runtimes --build-arg WITH_PLAYWRIGHT=true -t foreman:full .
```
