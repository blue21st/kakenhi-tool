
#!/usr/bin/env python3
"""
科研費取得合計算出ツール - バックエンドサーバー（Render対応版）
"""
import http.server
import json
import urllib.request
import urllib.parse
import os
import re
import sys
import time
import traceback
from http.server import HTTPServer

# ✅ Render対応：PORTは環境変数から取得
PORT = int(os.environ.get("PORT", 8080))

def log(level, msg):
    print(f"[{level}] {msg}", file=sys.stderr, flush=True)

class KakenHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        log("HTTP", fmt % args)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/search":
            self.handle_search(parsed)
        elif parsed.path in ("/", "/index.html"):
            self.serve_html()
        else:
            super().do_GET()

    def serve_html(self):
        try:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(content.encode("utf-8"))
        except Exception as e:
            self.send_error(500, str(e))

    def handle_search(self, parsed):
        params = urllib.parse.parse_qs(parsed.query)
        name = params.get("name", [""])[0].strip()
        institution = params.get("institution", [""])[0].strip()

        # ✅ appid は環境変数から取得
        appid = os.environ.get("CINII_APPID", "").strip()

        if not name:
            self.send_json({"error": "研究者名を入力してください"}, 400)
            return
        if not appid:
            self.send_json({"error": "CINII_APPID が設定されていません"}, 500)
            return

        log("INFO", f"===== 検索開始: '{name}' =====")

        try:
            result = self.aggregate_dummy(name, institution)
            self.send_json(result)
        except Exception as e:
            log("ERROR", str(e))
            traceback.print_exc()
            self.send_json({"error": str(e)}, 500)

    # （※ ここでは中身省略、あなたの既存処理をそのまま残してOK）
    def aggregate_dummy(self, name, institution):
        # 動作確認用のダミー（Render起動確認に十分）
        return {
            "researcher_name": name,
            "institution": institution,
            "total_projects": 0,
            "amount_available_count": 0,
            "amount_unavailable_count": 0,
            "total_amount": 0,
            "projects": []
        }

    def send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode("utf-8"))

def main():
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    print("=" * 60)
    print(" 科研費取得合計算出ツール（Render対応）")
    print(f" サーバー起動 PORT={PORT}")
    print("=" * 60)

    server = HTTPServer(("", PORT), KakenHandler)
    server.serve_forever()

if __name__ == "__main__":
    main()
