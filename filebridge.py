#!/usr/bin/env python3

import asyncio
import json
import os
import time
import uuid
import secrets
import mimetypes
from urllib.parse import quote
from pathlib import Path

from aiohttp import web, ClientSession, WSMsgType


CDP_LIST = "http://127.0.0.1:9222/json/list"
UPLOAD_ROOT = Path("/tmp/filebridge")
UPLOAD_TTL = 30 * 60

#
# -------------------------------------------------------
# REMOTE DOWNLOAD -> LOCAL BROWSER
# -------------------------------------------------------
#

DOWNLOAD_ROOT = Path(
    "/tmp/filebridge-downloads"
)

DOWNLOAD_TTL = 30 * 60

UPLOAD_ROOT.mkdir(
    parents=True,
    exist_ok=True
)

DOWNLOAD_ROOT.mkdir(
    parents=True,
    exist_ok=True
)

clients = set()
connections = {}
target_tasks = {}

download_active = {}
download_tokens = {}

pending = None
pending_lock = asyncio.Lock()


def log(text):
    print(f"[filebridge] {text}", flush=True)


class CDPConnection:
    def __init__(self, ws, target_id):
        self.ws = ws
        self.target_id = target_id
        self.seq = 0
        self.lock = asyncio.Lock()

    async def send(self, method, params=None):
        async with self.lock:
            self.seq += 1
            msg = {
                "id": self.seq,
                "method": method
            }

            if params is not None:
                msg["params"] = params

            await self.ws.send_str(
                json.dumps(msg)
            )

            return self.seq


async def broadcast(obj):
    payload = json.dumps(obj)

    dead = []

    for ws in list(clients):
        try:
            await ws.send_str(payload)
        except Exception:
            dead.append(ws)

    for ws in dead:
        clients.discard(ws)


async def target_worker(target, session):
    global pending

    tid = target["id"]
    ws_url = target.get("webSocketDebuggerUrl")

    if not ws_url:
        return

    try:
        async with session.ws_connect(
            ws_url,
            max_msg_size=0
        ) as ws:

            conn = CDPConnection(ws, tid)
            connections[tid] = conn

            await conn.send("Page.enable")
            await conn.send("DOM.enable")

            await conn.send(
                "Page.setInterceptFileChooserDialog",
                {
                    "enabled": True
                }
            )

            log(
                f"attached target={tid[:8]} "
                f"url={target.get('url','')}"
            )

            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue

                try:
                    data = json.loads(msg.data)
                except Exception:
                    continue

                if "error" in data:
                    log(
                        "CDP error: " +
                        json.dumps(
                            data["error"],
                            ensure_ascii=False
                        )
                    )
                    continue

                if (
                    data.get("method") ==
                    "Page.fileChooserOpened"
                ):
                    params = data.get(
                        "params",
                        {}
                    )

                    backend_node_id = params.get(
                        "backendNodeId"
                    )

                    if not backend_node_id:
                        log(
                            "fileChooserOpened without "
                            "backendNodeId"
                        )
                        continue

                    item = {
                        "target_id": tid,
                        "backend_node_id":
                            backend_node_id,
                        "mode":
                            params.get(
                                "mode",
                                "selectSingle"
                            ),
                        "time": time.time()
                    }

                    async with pending_lock:
                        pending = item

                    log(
                        "file chooser intercepted "
                        f"mode={item['mode']} "
                        f"node={backend_node_id}"
                    )

                    await broadcast({
                        "type": "choose",
                        "mode": item["mode"]
                    })

    except asyncio.CancelledError:
        raise

    except Exception as exc:
        log(
            f"target {tid[:8]} disconnected: "
            f"{exc}"
        )

    finally:
        connections.pop(tid, None)



#
# -------------------------------------------------------
# DOWNLOAD CAPTURE
# -------------------------------------------------------
#

async def cleanup_download(
    token,
    path
):
    await asyncio.sleep(
        DOWNLOAD_TTL
    )

    item = download_tokens.get(
        token
    )

    if (
        item is None or
        item.get("path") != path
    ):
        return

    download_tokens.pop(
        token,
        None
    )

    try:
        Path(path).unlink(
            missing_ok=True
        )
    except Exception:
        pass

    log(
        f"download cleanup "
        f"token={token[:8]}"
    )


async def browser_download_worker():
    """
    Browser-domain CDP connection.

    Chrome downloads files under their GUID and
    Browser.downloadProgress tells us when the file
    has finished.
    """

    version_url = (
        "http://127.0.0.1:9222/json/version"
    )

    while True:
        try:
            async with ClientSession() as session:

                async with session.get(
                    version_url,
                    timeout=3
                ) as response:
                    version = (
                        await response.json()
                    )

                ws_url = version.get(
                    "webSocketDebuggerUrl"
                )

                if not ws_url:
                    raise RuntimeError(
                        "browser CDP websocket missing"
                    )

                async with session.ws_connect(
                    ws_url,
                    max_msg_size=0
                ) as ws:

                    command_id = 1

                    await ws.send_str(
                        json.dumps({
                            "id":
                                command_id,

                            "method":
                                "Browser.setDownloadBehavior",

                            "params": {
                                "behavior":
                                    "allowAndName",

                                "downloadPath":
                                    str(
                                        DOWNLOAD_ROOT
                                    ),

                                "eventsEnabled":
                                    True
                            }
                        })
                    )

                    #
                    # Wait for command response.
                    #
                    while True:
                        message = await ws.receive()

                        if (
                            message.type !=
                            WSMsgType.TEXT
                        ):
                            raise RuntimeError(
                                "browser CDP closed"
                            )

                        data = json.loads(
                            message.data
                        )

                        if (
                            data.get("id") !=
                            command_id
                        ):
                            continue

                        if "error" in data:
                            raise RuntimeError(
                                "setDownloadBehavior: "
                                + json.dumps(
                                    data["error"],
                                    ensure_ascii=False
                                )
                            )

                        break

                    log(
                        "browser download capture "
                        f"enabled path={DOWNLOAD_ROOT}"
                    )

                    async for message in ws:

                        if (
                            message.type !=
                            WSMsgType.TEXT
                        ):
                            continue

                        try:
                            data = json.loads(
                                message.data
                            )
                        except Exception:
                            continue

                        method = data.get(
                            "method"
                        )

                        params = data.get(
                            "params",
                            {}
                        )

                        #
                        # Download started.
                        #
                        if (
                            method ==
                            "Browser.downloadWillBegin"
                        ):
                            guid = str(
                                params.get(
                                    "guid",
                                    ""
                                )
                            )

                            if not guid:
                                continue

                            filename = (
                                safe_filename(
                                    params.get(
                                        "suggestedFilename"
                                    )
                                )
                            )

                            download_active[
                                guid
                            ] = {
                                "filename":
                                    filename,

                                "url":
                                    params.get(
                                        "url",
                                        ""
                                    ),

                                "started":
                                    time.time()
                            }

                            log(
                                "download BEGIN "
                                f"guid={guid} "
                                f"name={filename!r}"
                            )

                            await broadcast({
                                "type":
                                    "downloadStarted",

                                "filename":
                                    filename
                            })

                            continue

                        #
                        # Download progress/completion.
                        #
                        if (
                            method ==
                            "Browser.downloadProgress"
                        ):
                            guid = str(
                                params.get(
                                    "guid",
                                    ""
                                )
                            )

                            state = params.get(
                                "state"
                            )

                            item = (
                                download_active.get(
                                    guid
                                )
                            )

                            if item is None:
                                continue

                            if state == "canceled":

                                download_active.pop(
                                    guid,
                                    None
                                )

                                try:
                                    (
                                        DOWNLOAD_ROOT /
                                        guid
                                    ).unlink(
                                        missing_ok=True
                                    )
                                except Exception:
                                    pass

                                log(
                                    "download CANCELED "
                                    f"guid={guid}"
                                )

                                await broadcast({
                                    "type":
                                        "downloadCanceled",

                                    "filename":
                                        item[
                                            "filename"
                                        ]
                                })

                                continue

                            if state != "completed":
                                continue

                            #
                            # Chrome normally returns filePath
                            # here. allowAndName also guarantees
                            # GUID as the disk filename.
                            #
                            raw_path = (
                                params.get(
                                    "filePath"
                                )
                                or str(
                                    DOWNLOAD_ROOT /
                                    guid
                                )
                            )

                            file_path = Path(
                                raw_path
                            )

                            #
                            # Small grace period: completed event
                            # can race filesystem visibility.
                            #
                            for _ in range(50):

                                if (
                                    file_path.is_file()
                                ):
                                    break

                                fallback = (
                                    DOWNLOAD_ROOT /
                                    guid
                                )

                                if fallback.is_file():
                                    file_path = (
                                        fallback
                                    )
                                    break

                                await asyncio.sleep(
                                    0.05
                                )

                            if not file_path.is_file():

                                log(
                                    "download ERROR "
                                    "completed file missing "
                                    f"guid={guid} "
                                    f"path={file_path}"
                                )

                                continue

                            filename = item[
                                "filename"
                            ]

                            token = (
                                secrets.token_urlsafe(
                                    24
                                )
                            )

                            download_tokens[
                                token
                            ] = {
                                "path":
                                    str(
                                        file_path
                                    ),

                                "filename":
                                    filename,

                                "created":
                                    time.time()
                            }

                            download_active.pop(
                                guid,
                                None
                            )

                            try:
                                size = (
                                    file_path.stat().st_size
                                )
                            except Exception:
                                size = 0

                            log(
                                "download READY "
                                f"guid={guid} "
                                f"bytes={size} "
                                f"name={filename!r}"
                            )

                            await broadcast({
                                "type":
                                    "downloadReady",

                                "token":
                                    token,

                                "filename":
                                    filename,

                                "size":
                                    size
                            })

                            asyncio.create_task(
                                cleanup_download(
                                    token,
                                    str(
                                        file_path
                                    )
                                )
                            )

        except asyncio.CancelledError:
            raise

        except Exception as exc:
            log(
                "browser download worker: "
                f"{exc}"
            )

        await asyncio.sleep(1)


async def download_handler(
    request
):
    token = request.match_info.get(
        "token",
        ""
    )

    item = download_tokens.get(
        token
    )

    if item is None:
        raise web.HTTPNotFound(
            text="Download expired or not found"
        )

    path = Path(
        item["path"]
    )

    if not path.is_file():
        download_tokens.pop(
            token,
            None
        )

        raise web.HTTPNotFound(
            text="Download file not found"
        )

    filename = safe_filename(
        item["filename"]
    )

    #
    # ASCII fallback + RFC 5987 UTF-8 filename.
    #
    ascii_name = "".join(
        ch
        if (
            ch.isascii()
            and 32 <= ord(ch) < 127
            and ch not in '"\\/'
        )
        else "_"
        for ch in filename
    ).strip()

    if not ascii_name:
        ascii_name = "download"

    disposition = (
        f'attachment; filename="{ascii_name}"; '
        f"filename*=UTF-8''"
        f"{quote(filename, safe='')}"
    )

    content_type = (
        mimetypes.guess_type(
            filename
        )[0]
        or "application/octet-stream"
    )

    log(
        "download SERVE "
        f"name={filename!r} "
        f"path={path}"
    )

    return web.FileResponse(
        path,
        headers={
            "Content-Disposition":
                disposition,

            "Content-Type":
                content_type,

            "Cache-Control":
                "private, no-store"
        }
    )


async def test_download_source(
    request
):
    """
    Test endpoint used INSIDE the remote Chrome.
    """

    filename = (
        "Тест скачивания.txt"
    )

    disposition = (
        'attachment; '
        'filename="test-download.txt"; '
        "filename*=UTF-8''"
        + quote(
            filename,
            safe=""
        )
    )

    return web.Response(
        body=(
            "Selkies semantic download "
            "forwarding works.\n"
        ).encode("utf-8"),

        headers={
            "Content-Type":
                "text/plain; charset=utf-8",

            "Content-Disposition":
                disposition
        }
    )


async def discover_targets(app):
    async with ClientSession() as session:
        while True:
            try:
                async with session.get(
                    CDP_LIST,
                    timeout=3
                ) as response:
                    targets = await response.json()

                for target in targets:
                    if target.get("type") != "page":
                        continue

                    tid = target.get("id")

                    if not tid:
                        continue

                    task = target_tasks.get(tid)

                    if (
                        task is None or
                        task.done()
                    ):
                        target_tasks[tid] = (
                            asyncio.create_task(
                                target_worker(
                                    target,
                                    session
                                )
                            )
                        )

                for tid, task in list(
                    target_tasks.items()
                ):
                    if task.done():
                        target_tasks.pop(
                            tid,
                            None
                        )

            except Exception as exc:
                log(
                    f"target discovery: {exc}"
                )

            await asyncio.sleep(1)


async def cleanup_batch(path):
    await asyncio.sleep(UPLOAD_TTL)

    try:
        import shutil
        shutil.rmtree(
            path,
            ignore_errors=True
        )
        log(
            f"cleanup {path}"
        )
    except Exception:
        pass


def safe_filename(name):
    name = str(name or "upload")

    name = name.replace("\\", "/")
    name = name.split("/")[-1]
    name = name.replace("\x00", "")

    if not name:
        name = "upload"

    return name[:240]


async def upload_handler(request):
    global pending

    async with pending_lock:
        current = (
            dict(pending)
            if pending
            else None
        )

    if not current:
        return web.json_response(
            {
                "ok": False,
                "error":
                    "No pending remote file chooser"
            },
            status=409
        )

    reader = await request.multipart()

    batch = (
        UPLOAD_ROOT /
        uuid.uuid4().hex
    )

    batch.mkdir(
        parents=True,
        exist_ok=True
    )

    paths = []
    names = []
    index = 0

    while True:
        part = await reader.next()

        if part is None:
            break

        if (
            part.name != "files" or
            not part.filename
        ):
            continue

        original_name = safe_filename(
            part.filename
        )

        item_dir = (
            batch /
            str(index)
        )

        item_dir.mkdir(
            parents=True,
            exist_ok=True
        )

        if original_name.isascii():
            disk_name = original_name
        else:
            suffix = Path(
                original_name
            ).suffix

            if (
                not suffix.isascii() or
                len(suffix) > 16
            ):
                suffix = ""

            disk_name = (
                "rbi-upload-"
                + uuid.uuid4().hex[:10]
                + suffix
            )

        dest = item_dir / disk_name

        size = 0

        with dest.open("wb") as fp:
            while True:
                chunk = await part.read_chunk(
                    size=1024 * 1024
                )

                if not chunk:
                    break

                fp.write(chunk)
                size += len(chunk)

        paths.append(
            str(dest)
        )

        names.append(original_name)

        log(
            f"uploaded local file "
            f"original={original_name!r} "
            f"disk={disk_name!r} "
            f"bytes={size}"
        )

        index += 1

    if not paths:
        return web.json_response(
            {
                "ok": False,
                "error": "No files received"
            },
            status=400
        )

    if (
        current["mode"] ==
        "selectSingle"
    ):
        paths = paths[:1]
        names = names[:1]

    conn = connections.get(
        current["target_id"]
    )

    if conn is None:
        return web.json_response(
            {
                "ok": False,
                "error":
                    "Remote page target disappeared"
            },
            status=409
        )

    await conn.send(
        "DOM.setFileInputFiles",
        {
            "files": paths,
            "backendNodeId":
                current[
                    "backend_node_id"
                ]
        }
    )

    async with pending_lock:
        if (
            pending and
            pending.get("target_id") ==
                current["target_id"] and
            pending.get(
                "backend_node_id"
            ) ==
                current[
                    "backend_node_id"
                ]
        ):
            pending = None

    asyncio.create_task(
        cleanup_batch(batch)
    )

    log(
        "setFileInputFiles: " +
        ", ".join(names)
    )

    return web.json_response(
        {
            "ok": True,
            "files": names
        }
    )


FILEBRIDGE_JS = r'''
(() => {
  if (window.__selkiesFileBridge) return;
  window.__selkiesFileBridge = true;

  const picker =
    document.createElement('input');

  picker.type = 'file';
  picker.style.display = 'none';

  document.documentElement.appendChild(
    picker
  );

  const panel =
    document.createElement('div');

  panel.style.cssText = `
    position:fixed;
    right:16px;
    bottom:16px;
    z-index:2147483647;
    display:none;
    align-items:center;
    gap:10px;
    padding:10px 12px;
    border-radius:8px;
    background:rgba(25,25,25,.94);
    color:white;
    font:14px sans-serif;
    box-shadow:0 4px 18px rgba(0,0,0,.35)
  `;

  const label =
    document.createElement('span');

  const button =
    document.createElement('button');

  button.textContent =
    'Выбрать файл';

  button.style.cssText = `
    padding:6px 12px;
    cursor:pointer;
  `;

  panel.append(
    label,
    button
  );

  document.documentElement.appendChild(
    panel
  );

  let chooser = null;

  function show(
    text,
    showButton = true
  ) {
    label.textContent = text;

    button.style.display =
      showButton
        ? ''
        : 'none';

    panel.style.display =
      'flex';
  }

  function hideLater() {
    setTimeout(() => {
      panel.style.display =
        'none';
    }, 1200);
  }

  function openPicker() {
    picker.value = '';

    try {
      picker.click();
    } catch (_) {}
  }

  button.addEventListener(
    'click',
    openPicker
  );

  picker.addEventListener(
    'change',
    async () => {
      if (!picker.files.length) {
        return;
      }

      show(
        `Передача: ${picker.files.length}`,
        false
      );

      const form =
        new FormData();

      for (
        const file of picker.files
      ) {
        form.append(
          'files',
          file,
          file.name
        );
      }

      try {
        const response =
          await fetch(
            '/filebridge/upload',
            {
              method: 'POST',
              body: form
            }
          );

        const data =
          await response.json();

        if (!response.ok) {
          throw new Error(
            data.error ||
            `HTTP ${response.status}`
          );
        }

        show(
          'Файл передан',
          false
        );

        hideLater();

      } catch (error) {
        show(
          `Ошибка: ${error.message}`,
          true
        );
      }
    }
  );

  function connect() {
    const proto =
      location.protocol === 'https:'
        ? 'wss:'
        : 'ws:';

    const ws =
      new WebSocket(
        `${proto}//${location.host}/filebridge/ws`
      );

    ws.addEventListener(
      'message',
      event => {
        let msg;

        try {
          msg =
            JSON.parse(
              event.data
            );
        } catch (_) {
          return;
        }

        /*
         * -----------------------------------------------
         * REMOTE DOWNLOAD -> LOCAL BROWSER
         * -----------------------------------------------
         */

        if (
          msg.type ===
          'downloadStarted'
        ) {
          show(
            `Скачивается: ${
              msg.filename ||
              'файл'
            }`,
            false
          );

          return;
        }

        if (
          msg.type ===
          'downloadReady'
        ) {
          const link =
            document.createElement(
              'a'
            );

          link.href =
            '/filebridge/download/' +
            encodeURIComponent(
              msg.token
            );

          /*
           * Content-Disposition on the HTTP
           * response also contains the original
           * filename. This attribute is an
           * additional hint to the browser.
           */
          link.download =
            msg.filename || '';

          link.style.display =
            'none';

          document.body.appendChild(
            link
          );

          link.click();

          setTimeout(
            () => {
              link.remove();
            },
            1000
          );

          show(
            `Скачивание на ПК: ${
              msg.filename ||
              'файл'
            }`,
            false
          );

          hideLater();

          console.log(
            '[filebridge] LOCAL DOWNLOAD',
            msg.filename
          );

          return;
        }

        if (
          msg.type ===
          'downloadCanceled'
        ) {
          show(
            `Скачивание отменено: ${
              msg.filename ||
              'файл'
            }`,
            false
          );

          hideLater();

          return;
        }

        if (
          msg.type !== 'choose'
        ) {
          return;
        }

        chooser = msg;

        picker.multiple =
          msg.mode ===
          'selectMultiple';

        show(
          picker.multiple
            ? 'Выберите файлы'
            : 'Выберите файл',
          true
        );

        /*
         * Обычно локальный click,
         * отправленный Selkies в remote Chrome,
         * ещё сохраняет transient user activation.
         * Поэтому сначала пробуем открыть chooser
         * автоматически.
         *
         * Если браузер это заблокирует,
         * остаётся маленькая кнопка справа снизу.
         */
        setTimeout(
          openPicker,
          0
        );
      }
    );

    ws.addEventListener(
      'close',
      () => {
        setTimeout(
          connect,
          1000
        );
      }
    );
  }

  connect();

  console.log(
    '[filebridge] client installed'
  );
})();
'''


async def js_handler(request):
    return web.Response(
        text=FILEBRIDGE_JS,
        content_type="application/javascript",
        headers={
            "Cache-Control": "no-store"
        }
    )


async def ws_handler(request):
    ws = web.WebSocketResponse(
        heartbeat=30
    )

    await ws.prepare(request)

    clients.add(ws)

    log(
        f"local client connected "
        f"clients={len(clients)}"
    )

    try:
        async for _ in ws:
            pass
    finally:
        clients.discard(ws)

        log(
            f"local client disconnected "
            f"clients={len(clients)}"
        )

    return ws


async def health_handler(request):
    return web.json_response({
        "ok": True,
        "targets":
            len(connections),
        "clients":
            len(clients),
        "pending":
            pending is not None
    })


async def startup(app):
    app["discover_task"] = (
        asyncio.create_task(
            discover_targets(app)
        )
    )

    app["download_task"] = (
        asyncio.create_task(
            browser_download_worker()
        )
    )

    log(
        "started on 0.0.0.0:9231"
    )


async def cleanup(app):
    for key in (
        "discover_task",
        "download_task"
    ):
        task = app.get(
            key
        )

        if task:
            task.cancel()


app = web.Application(
    client_max_size=
        512 * 1024 * 1024
)

app.router.add_get(
    "/test-download",
    test_download_source
)

app.router.add_get(
    "/filebridge/download/{token}",
    download_handler
)

app.router.add_get(
    "/health",
    health_handler
)

app.router.add_get(
    "/filebridge.js",
    js_handler
)

app.router.add_get(
    "/filebridge/ws",
    ws_handler
)

app.router.add_post(
    "/filebridge/upload",
    upload_handler
)

app.on_startup.append(startup)
app.on_cleanup.append(cleanup)

web.run_app(
    app,
    host="0.0.0.0",
    port=9231,
    print=None
)
