#!/usr/bin/env python3
"""
科研費取得合計算出ツール - サーバー v3.0 (デプロイ版)
CiNii Research API を使用

環境変数:
  CINII_APPID    - CiNii Application ID (必須)
  ACCESS_PASSWORD - アクセスパスワード (必須)
  PORT           - ポート番号 (デフォルト: 8080)
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
import hashlib
import secrets
from http.server import HTTPServer
from concurrent.futures import ThreadPoolExecutor, as_completed
from http.cookies import SimpleCookie

PORT = int(os.environ.get("PORT", 8080))
CINII_APPID = os.environ.get("CINII_APPID", "")
ACCESS_PASSWORD = os.environ.get("ACCESS_PASSWORD", "")

# セッション管理（簡易）
valid_sessions = set()


def log(level, msg):
    print(f"  [{level}] {msg}", file=sys.stderr, flush=True)


class KakenHandler(http.server.SimpleHTTPRequestHandler):

    def log_message(self, fmt, *args):
        log("HTTP", fmt % args)

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/login":
            self.handle_login()
        else:
            self.send_error(404)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == "/api/search":
            if not self.check_auth():
                self.send_json({"error": "認証が必要です。ページを再読み込みしてください。"}, 401)
                return
            self.handle_search(parsed)
        elif parsed.path in ("/", "/index.html"):
            self.serve_html()
        else:
            super().do_GET()

    # ─── 認証 ───

    def handle_login(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")
        try:
            data = json.loads(body)
        except:
            data = {}

        password = data.get("password", "")

        if ACCESS_PASSWORD and password == ACCESS_PASSWORD:
            session_id = secrets.token_hex(32)
            valid_sessions.add(session_id)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Set-Cookie", f"session={session_id}; Path=/; HttpOnly; SameSite=Strict; Max-Age=86400")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode("utf-8"))
        elif not ACCESS_PASSWORD:
            # パスワード未設定ならスキップ
            session_id = secrets.token_hex(32)
            valid_sessions.add(session_id)
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Set-Cookie", f"session={session_id}; Path=/; HttpOnly; SameSite=Strict; Max-Age=86400")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode("utf-8"))
        else:
            self.send_json({"error": "パスワードが正しくありません"}, 403)

    def check_auth(self):
        if not ACCESS_PASSWORD:
            return True
        cookie_header = self.headers.get("Cookie", "")
        cookies = SimpleCookie()
        try:
            cookies.load(cookie_header)
        except:
            return False
        session = cookies.get("session")
        if session and session.value in valid_sessions:
            return True
        return False

    # ─── HTML配信 ───

    def serve_html(self):
        try:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html")
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()
            # パスワード要否をHTMLに埋め込む
            content = content.replace("__NEED_PASSWORD__", "true" if ACCESS_PASSWORD else "false")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(content.encode("utf-8"))
        except Exception as e:
            self.send_error(500, str(e))

    # ─── 検索 ───

    def handle_search(self, parsed):
        params = urllib.parse.parse_qs(parsed.query)
        name = params.get("name", [""])[0].replace("\u3000", " ").strip()
        institution = params.get("institution", [""])[0].replace("\u3000", " ").strip()

        if not name:
            self.send_json({"error": "研究者名を入力してください"}, 400)
            return

        if not CINII_APPID:
            self.send_json({"error": "サーバーにCiNii Application IDが設定されていません。管理者に連絡してください。"}, 500)
            return

        log("INFO", f"===== 検索開始: '{name}' (所属: '{institution}') =====")

        try:
            result = self.search_cinii_projects(name, institution)
            if "error" in result:
                self.send_json(result)
                return
            log("INFO", f"===== 検索完了: {result['total_projects']}件, 合計 {result['total_amount']:,}円 =====")
            self.send_json(result)
        except Exception as e:
            log("ERROR", f"検索エラー: {e}")
            traceback.print_exc(file=sys.stderr)
            self.send_json({"error": f"検索エラー: {str(e)}"}, 500)

    def search_cinii_projects(self, name, institution):
        all_projects = []
        start = 1
        per_page = 50
        total = None

        for page_num in range(1, 21):
            params = {
                "q": name,
                "format": "json",
                "count": str(per_page),
                "start": str(start),
                "appid": CINII_APPID,
                "dataSourceType": "KAKEN",
            }
            if institution:
                params["affiliation"] = institution

            url = "https://cir.nii.ac.jp/opensearch/projects?" + urllib.parse.urlencode(params)
            log("INFO", f"API呼び出し (ページ{page_num})")

            data = self.fetch_json(url)
            if data is None:
                if page_num == 1:
                    return {"error": "CiNii Research APIへのアクセスに失敗しました。数分待ってから再試行してください。"}
                break

            if total is None:
                total = data.get("opensearch:totalResults", 0)
                log("INFO", f"検索ヒット総数: {total}件")

            items = data.get("items", [])
            for item in items:
                p = self.parse_cinii_item(item)
                if p:
                    p["_search_name"] = name
                    all_projects.append(p)

            if not items or (total and len(all_projects) >= total):
                break
            start += per_page
            time.sleep(1)

        if all_projects:
            log("INFO", f"各課題の詳細を取得中... ({len(all_projects)}件)")
            self.enrich_with_detail(all_projects)

        return self.aggregate(name, institution, all_projects)

    def parse_cinii_item(self, item):
        p = {
            "title": item.get("title", ""),
            "link": "",
            "project_number": "",
            "category": "",
            "period": "",
            "institution": "",
            "role": "",
            "total_amount": 0,
            "direct_cost": 0,
            "indirect_cost": 0,
        }

        link_obj = item.get("link", {})
        if isinstance(link_obj, dict):
            p["link"] = link_obj.get("@id", "")
        elif isinstance(link_obj, str):
            p["link"] = link_obj

        see_also = item.get("rdfs:seeAlso", {})
        if isinstance(see_also, dict):
            p["_detail_url"] = see_also.get("@id", "")

        creators = item.get("dc:creator", [])
        if isinstance(creators, list) and creators:
            p["_creators"] = creators

        pub_date = item.get("prism:publicationDate", "")
        if pub_date:
            p["period"] = pub_date

        if p["link"]:
            m = re.search(r"/crid/(\d+)", p["link"])
            if m:
                p["_crid"] = m.group(1)

        return p if p["title"] else None

    # ─── 詳細取得（3並列） ───

    def enrich_with_detail(self, projects):
        tasks = []
        for i, p in enumerate(projects):
            detail_url = p.pop("_detail_url", "")
            p.pop("_creators", None)
            crid = p.pop("_crid", "")

            if not detail_url:
                if crid:
                    detail_url = f"https://cir.nii.ac.jp/crid/{crid}.json"
                elif p.get("link") and "/crid/" in p["link"]:
                    detail_url = p["link"].rstrip("/") + ".json"

            if detail_url:
                sep = "&" if "?" in detail_url else "?"
                full_url = f"{detail_url}{sep}appid={CINII_APPID}"
                tasks.append((i, p, full_url))

        done = 0
        def fetch_detail(args):
            idx, proj, url = args
            time.sleep(0.2)
            return idx, proj, self.fetch_json(url)

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {executor.submit(fetch_detail, t): t for t in tasks}
            for future in as_completed(futures):
                idx, proj, detail = future.result()
                if detail:
                    self.extract_kaken_detail(detail, proj)
                done += 1
                if done % 10 == 0:
                    log("INFO", f"  詳細取得: {done}/{len(tasks)}件完了")

    def extract_kaken_detail(self, detail, p):
        # 金額
        alloc = detail.get("allocationAmount", {})
        if alloc:
            total_cost = alloc.get("totalCost", {})
            if isinstance(total_cost, dict):
                try:
                    p["total_amount"] = int(total_cost.get("amount", "0"))
                except:
                    pass
            breakdown = alloc.get("breakdownCost", [])
            if isinstance(breakdown, list):
                for item in breakdown:
                    notation = item.get("notation", [])
                    label = ""
                    for n in notation:
                        if isinstance(n, dict):
                            label = n.get("@value", "")
                            break
                    try:
                        amt = int(item.get("amount", "0"))
                    except:
                        amt = 0
                    if "直接" in label or "Direct" in label:
                        p["direct_cost"] = amt
                    elif "間接" in label or "Indirect" in label:
                        p["indirect_cost"] = amt

        # 課題番号
        proj_ids = detail.get("projectIdentifier", [])
        if isinstance(proj_ids, list):
            for pid in proj_ids:
                if isinstance(pid, dict) and pid.get("@type") == "KAKEN":
                    p["project_number"] = pid.get("@value", "")
                    break

        # 研究種目
        grant = detail.get("grant", {})
        if grant:
            streams = grant.get("jpcoar:fundingStream", [])
            if isinstance(streams, list):
                for s in streams:
                    if isinstance(s, dict) and s.get("@language") == "ja":
                        p["category"] = s.get("@value", "")
                        break

        # 研究期間
        since = detail.get("since", "")
        until = detail.get("until", "")
        if since:
            start_year = since[:4]
            end_year = until[:4] if until else ""
            p["period"] = f"{start_year} - {end_year}" if end_year else start_year

        # 所属機関
        institutions = detail.get("institution", [])
        if isinstance(institutions, list) and institutions:
            for n in institutions[0].get("notation", []):
                if isinstance(n, dict) and n.get("@language") == "ja":
                    p["institution"] = n.get("@value", "")
                    break

        # KAKENリンク
        urls = detail.get("url", [])
        if isinstance(urls, list):
            for u in urls:
                if "kaken.nii.ac.jp" in u.get("@id", ""):
                    p["link"] = u["@id"]
                    break

        # 役割
        researchers = detail.get("researcher", [])
        if isinstance(researchers, list) and "_search_name" in p:
            search_name = p.pop("_search_name")
            for r in researchers:
                names = r.get("foaf:name", [])
                for name_obj in names:
                    if isinstance(name_obj, dict):
                        name_val = name_obj.get("@value", "")
                        if name_val.replace(" ", "").replace("\u3000", "") == search_name.replace(" ", "").replace("\u3000", ""):
                            role_raw = r.get("role", "")
                            if role_raw == "principal_investigator":
                                p["role"] = "代表"
                            elif role_raw == "co_investigator_buntan":
                                p["role"] = "分担"
                            elif role_raw == "co_investigator_renkei":
                                p["role"] = "連携"
                            else:
                                p["role"] = role_raw
                            break
                if p["role"]:
                    break

    # ─── ユーティリティ ───

    def fetch_json(self, url):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Kakenhi-Aggregator/3.0",
                "Accept": "application/json, */*",
            })
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = resp.read()
                enc = "utf-8"
                ct = resp.headers.get("Content-Type", "")
                if "charset=" in ct:
                    enc = ct.split("charset=")[-1].split(";")[0].strip()
                return json.loads(data.decode(enc))
        except urllib.error.HTTPError as e:
            log("ERROR", f"HTTP {e.code}: {e.reason}")
            return None
        except Exception as e:
            log("ERROR", f"通信エラー: {e}")
            return None

    def aggregate(self, name, institution, projects):
        total_amount = pi_amount = 0
        ok = ng = pi_count = co_count = 0
        for p in projects:
            a = p.get("total_amount", 0) or 0
            if a > 0:
                total_amount += a
                ok += 1
                if p.get("role") == "代表":
                    pi_amount += a
            else:
                ng += 1
            if p.get("role") == "代表":
                pi_count += 1
            elif p.get("role") in ("分担", "連携"):
                co_count += 1
        return {
            "researcher_name": name,
            "institution": institution,
            "total_projects": len(projects),
            "amount_available_count": ok,
            "amount_unavailable_count": ng,
            "total_amount": total_amount,
            "pi_amount": pi_amount,
            "pi_count": pi_count,
            "co_count": co_count,
            "projects": projects,
        }

    def send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))


def main():
    if not CINII_APPID:
        print("  [警告] CINII_APPID 環境変数が未設定です", file=sys.stderr)
    if not ACCESS_PASSWORD:
        print("  [警告] ACCESS_PASSWORD 環境変数が未設定です（パスワードなしで動作します）", file=sys.stderr)

    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    print("=" * 60)
    print("  科研費取得合計算出ツール v3.0 (デプロイ版)")
    print(f"  サーバー: http://localhost:{PORT}")
    print(f"  パスワード保護: {'あり' if ACCESS_PASSWORD else 'なし'}")
    print("  終了: Ctrl+C")
    print("=" * 60)

    server = HTTPServer(("", PORT), KakenHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  サーバーを停止しました")
        server.server_close()


if __name__ == "__main__":
    main()
