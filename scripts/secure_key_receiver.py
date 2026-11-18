from __future__ import annotations

import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs


HOST = "127.0.0.1"
PORT = 8765
PROJECT_DIR = Path(__file__).resolve().parent.parent
SECRET_DIR = PROJECT_DIR / ".secrets"
SECRET_FILE = SECRET_DIR / "tunnel-runtime-key"


PAGE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MemorySafe Private Setup</title>
  <style>
    body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #f6f2ff; color: #241a35; font-family: -apple-system, BlinkMacSystemFont, sans-serif; }
    main { width: min(520px, calc(100vw - 40px)); background: white; border: 1px solid #e7ddf5; border-radius: 22px; padding: 32px; box-shadow: 0 18px 50px rgba(72,46,110,.12); }
    h1 { margin: 0 0 10px; font-size: 28px; }
    p { line-height: 1.5; color: #665a75; }
    label { display: block; margin: 24px 0 8px; font-weight: 650; }
    input { box-sizing: border-box; width: 100%; padding: 14px; border: 1px solid #cfc2df; border-radius: 12px; font-size: 18px; }
    button { width: 100%; margin-top: 16px; padding: 14px; border: 0; border-radius: 12px; background: #7a4fc7; color: white; font-size: 17px; font-weight: 700; cursor: pointer; }
    button:disabled { opacity: .65; cursor: wait; }
    small { display: block; margin-top: 16px; color: #746780; }
    .error { color: #a32638; font-weight: 650; }
  </style>
</head>
<body>
  <main>
    <h1>MemorySafe private setup</h1>
    <p>This page runs only on this Mac. The key is saved locally and is not sent to chat.</p>
    __MESSAGE__
    <form id="key-form" autocomplete="off">
      <label for="key">Copied OpenAI key</label>
      <input id="key" name="key" type="password" placeholder="Paste the key beginning with sk-" autofocus required>
      <button id="save-button" type="submit">Save privately</button>
    </form>
    <small>Use ⌘V to paste. Never send this key in a chat.</small>
  </main>
  <script>
    document.getElementById('key-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      const button = document.getElementById('save-button');
      const body = new URLSearchParams();
      body.set('key', document.getElementById('key').value);
      button.disabled = true;
      button.textContent = 'Saving…';
      try {
        const response = await fetch('/save', {
          method: 'POST',
          headers: {'Content-Type': 'application/x-www-form-urlencoded'},
          body: body.toString()
        });
        document.documentElement.innerHTML = await response.text();
      } catch (_error) {
        button.disabled = false;
        button.textContent = 'Try saving again';
      }
    });
  </script>
</body>
</html>"""


SUCCESS_PAGE = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>MemorySafe key saved</title><style>body{font-family:-apple-system,BlinkMacSystemFont,sans-serif;background:#f6f2ff;min-height:100vh;display:grid;place-items:center;margin:0;color:#241a35}main{background:white;padding:36px;border-radius:22px;text-align:center;box-shadow:0 18px 50px rgba(72,46,110,.12)}h1{color:#5f37a4}</style></head><body><main><h1>Saved securely ✓</h1><p>Return to Codex. MemorySafe will finish connecting now.</p></main></body></html>"""


class Handler(BaseHTTPRequestHandler):
    saved = False

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def _send_html(self, body: str, status: int = 200) -> None:
        payload = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/":
            self.send_error(404)
            return
        self._send_html(PAGE.replace("__MESSAGE__", ""))

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/save":
            self.send_error(404)
            return

        length = int(self.headers.get("Content-Length", "0"))
        fields = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
        secret = fields.get("key", [""])[0].strip()
        if not secret.startswith("sk-") or len(secret) < 30:
            message = '<p class="error">That key is incomplete. Copy the full key beginning with sk- and try again.</p>'
            self._send_html(PAGE.replace("__MESSAGE__", message), 400)
            return

        SECRET_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(SECRET_DIR, 0o700)
        temporary_file = SECRET_FILE.with_suffix(".tmp")
        temporary_file.write_text(secret, encoding="utf-8")
        os.chmod(temporary_file, 0o600)
        temporary_file.replace(SECRET_FILE)
        os.chmod(SECRET_FILE, 0o600)
        Handler.saved = True
        self._send_html(SUCCESS_PAGE)


def main() -> None:
    with HTTPServer((HOST, PORT), Handler) as server:
        print(f"READY http://{HOST}:{PORT}/", flush=True)
        while not Handler.saved:
            server.handle_request()
        print("SAVED", flush=True)


if __name__ == "__main__":
    main()
