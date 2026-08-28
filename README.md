# Selkies Chromium Bridge

A Chromium integration layer for
[Selkies](https://github.com/selkies-project/selkies).

It makes a streamed remote Chromium session behave more like a local
browser, especially for file uploads and downloads.

## Why

Selkies already provides the difficult parts of remote application
streaming:

- screen capture and video encoding;
- keyboard and pointer input;
- audio;
- clipboard integration;
- browser-based streaming.

Traditional remote-desktop file transfer is less convenient when the
remote application is itself a web browser.

A conventional workflow looks like this:

```text
local PC
   |
   | upload
   v
remote filesystem
   |
   | remote file picker
   v
remote Chromium
   |
   v
website
```

Selkies Chromium Bridge instead connects the semantics of the remote
browser to the local browser:

```text
Remote website
      |
      | <input type="file">
      v
Remote Chromium
      |
      | Chrome DevTools Protocol
      v
FileBridge
      |
      | local native file picker
      v
Local browser / PC
```

After the user selects a local file, FileBridge transfers it to the
container and assigns it directly to the original remote file input.

Downloads work in the opposite direction: downloads started inside
remote Chromium are detected through CDP and forwarded automatically to
the local browser.

No remote file manager is required for normal browser upload/download
workflows.

## Features

- Selkies browser streaming
- Chromium running in Xvfb/Openbox
- PulseAudio support
- persistent Chromium profile
- automatic Chromium recovery
- bidirectional Selkies clipboard
- semantic `<input type="file">` forwarding
- multiple-file uploads
- `accept=` file filters
- file inputs inside iframes
- automatic handling of newly opened Chromium tabs
- remote download forwarding
- Unicode-safe download filenames
- health endpoint
- smoke tests
- pinned Selkies, pixelflux and pcmflux source revisions

## Architecture

```text
                     HTTPS reverse proxy
                            |
                            v
                    +---------------+
                    | nginx gateway |
                    +-------+-------+
                            |
                  +---------+---------+
                  |                   |
                  v                   v
            Selkies :8080       FileBridge :9231
                  |                   |
                  |                   | CDP
                  |                   v
                  +------------> Chromium :9222
                                      |
                                      v
                                   X11/Xvfb
```

The nginx gateway included in Docker Compose:

- proxies the Selkies web application;
- proxies FileBridge;
- injects `filebridge.js` into the Selkies client.

The external reverse proxy only needs to provide HTTPS and, optionally,
authentication.

## Requirements

- Docker Engine
- Docker Compose
- Linux amd64 host
- an HTTPS reverse proxy
- a modern local browser

The container is based on Ubuntu 24.04.

HTTPS is required because the Selkies client and several browser APIs
require a secure browser context.

## Quick start

Clone the repository:

```bash
git clone https://github.com/to-a11y/selkies-chromium-bridge.git
cd selkies-chromium-bridge
```

Create local configuration:

```bash
cp .env.example .env
```

Build and start:

```bash
docker compose up -d --build
```

By default the gateway listens on:

```text
127.0.0.1:8080
```

Put an HTTPS reverse proxy in front of it.

Example nginx configuration:

```nginx
server {
    listen 443 ssl;
    server_name browser.example.com;

    ssl_certificate     /path/to/fullchain.pem;
    ssl_certificate_key /path/to/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8080;

        proxy_http_version 1.1;

        proxy_set_header Host $http_host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;

        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";

        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        proxy_buffering off;
    }
}
```

Authentication may also be implemented at the external reverse-proxy
layer.

## Upstream revisions

The Docker build currently pins the tested media stack:

```text
Selkies
5b17d3ea6a39c759abd99b12e9250978dd3dd8a5

pixelflux
40f01f7752370b3c6962671ca282a4f32456e72d

pcmflux
c5487f28b71e5f82211f21b08a8b84c9e08fa48f
```

These can be changed through `.env`.

Pinning prevents future upstream changes from silently changing the
tested build.

## File upload

FileBridge watches Chromium page targets using the Chrome DevTools
Protocol.

When a remote website opens a file chooser:

1. Chromium emits a file chooser event.
2. FileBridge records the remote input element.
3. `filebridge.js` opens a native file picker in the local browser.
4. The selected file is transferred to the container.
5. FileBridge assigns the file to the original remote input through CDP.

The bridge has been tested with:

- single-file inputs;
- `multiple` inputs;
- `accept=` filters;
- inputs inside iframes;
- newly opened Chromium tabs;
- repeated uploads;
- Unicode filenames.

### Unicode upload limitation

Files whose names contain non-ASCII characters are currently stored
inside the container under unique ASCII aliases before being assigned to
the remote Chromium file input.

The file contents are unchanged.

The remote website may therefore see the generated ASCII filename
instead of the original Unicode filename.

## Download forwarding

FileBridge also monitors Chromium browser-level download events.

When a download completes inside remote Chromium:

1. FileBridge detects completion through CDP.
2. The downloaded file is exposed through a temporary bridge endpoint.
3. `filebridge.js` initiates a download in the local browser.

Unicode download filenames are preserved.

## Persistent browser profile

The Chromium profile is stored outside the container:

```text
./runtime/chrome-profile
```

It is excluded from Git.

This preserves normal browser state between container recreations.

## Chromium recovery

The startup script monitors Chromium.

If the browser exits, it is restarted automatically using the persistent
profile.

Stale Chromium profile locks are removed before startup.

The startup sequence also validates the X11 display before starting the
browser, avoiding stale Xvfb socket problems after container restarts.

## Audio

PulseAudio runs inside the container with a virtual output sink.

Selkies/pcmflux captures:

```text
output.monitor
```

and forwards audio to the local browser.

## Testing

Run:

```bash
./tests/smoke.sh
```

The smoke test checks:

- browser container state;
- gateway HTTP;
- FileBridge JavaScript injection;
- FileBridge health endpoint;
- X11;
- Chromium;
- Chrome DevTools Protocol;
- Selkies;
- PulseAudio;
- audio monitor;
- fatal video pipeline startup errors.

Browser-level upload, download, clipboard and audio behavior should also
be tested manually after significant changes.

## Security

This project is a browser-streaming integration layer, not a hardened
Remote Browser Isolation security appliance.

Chromium currently runs as root inside the Docker container with:

```text
--no-sandbox
```

Running Chromium's Linux sandbox inside a default Docker container
requires additional namespace/capability changes. This project does not
grant broad container privileges merely to enable that sandbox.

Do not rely on this project as the sole security boundary for hostile or
untrusted browser workloads.

Public deployments should normally be:

- exposed through HTTPS;
- protected by authentication when appropriate;
- restricted by firewall/reverse-proxy policy.

## Relationship to Selkies

This project does not replace Selkies.

Selkies and its media components provide the remote display, input and
audio stack.

Selkies Chromium Bridge adds Chromium-specific browser integration and
container/runtime glue around that stack.

The semantic file-forwarding approach is conceptually similar to local
file forwarding used by remote browser-automation systems, but here it
is applied to an interactive streamed Chromium session.

## License

Original code in this repository is licensed under the Mozilla Public
License 2.0.

Selkies, pixelflux, pcmflux and other third-party components retain
their respective licenses.

See `NOTICE` for upstream component information.
