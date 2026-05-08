from flask import Flask, render_template, request, jsonify, send_file, Response
import yt_dlp
import os
import tempfile
import uuid
import json
import threading
import base64

app = Flask(__name__)

COOKIES_FILE = os.path.join(os.path.dirname(__file__), "cookies.txt")

progress_store = {}

def _get_cookies_file():
    if os.path.exists(COOKIES_FILE):
        return COOKIES_FILE
    # Fallback: cookies encodés en base64 dans la variable d'env YT_COOKIES_B64
    cookies_b64 = os.environ.get("YT_COOKIES_B64", "")
    if cookies_b64:
        tmp_path = os.path.join(tempfile.gettempdir(), "yt_cookies.txt")
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(base64.b64decode(cookies_b64).decode("utf-8"))
            return tmp_path
        except Exception:
            pass
    return None

def get_ydl_base_opts():
    """
    Options yt-dlp avec contournement du blocage bot YouTube sur IP datacenter.

    Stratégie :
    - player_client=ios : YouTube vérifie les clients mobiles différemment des
      navigateurs web. Les IPs datacenter sont moins bloquées par ce chemin.
    - po_token : jeton de preuve d'origine optionnel, généré depuis un vrai
      navigateur. Le plus fiable si l'IP est très agressive. À définir via
      l'env var YT_PO_TOKEN sur Render (voir README pour la procédure).
    """
    extractor_args = {
        "youtube": {
            "player_client": ["ios"],
        }
    }

    po_token = os.environ.get("YT_PO_TOKEN", "")
    if po_token:
        po_client = os.environ.get("YT_PO_TOKEN_CLIENT", "web")
        extractor_args["youtube"]["po_token"] = [f"{po_client}+{po_token}"]

    opts = {
        "quiet": True,
        "no_warnings": True,
        "extractor_args": extractor_args,
    }

    cookies_file = _get_cookies_file()
    if cookies_file:
        opts["cookiefile"] = cookies_file

    return opts

def make_progress_hook(job_id):
    def hook(d):
        if d['status'] == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
            downloaded = d.get('downloaded_bytes', 0)
            speed = d.get('speed', 0)
            eta = d.get('eta', 0)
            percent = (downloaded / total * 100) if total else 0
            progress_store[job_id] = {
                'status': 'downloading',
                'percent': round(percent, 1),
                'speed': speed,
                'eta': eta,
                'total': total,
            }
        elif d['status'] == 'finished':
            progress_store[job_id]['status'] = 'processing'
            progress_store[job_id]['percent'] = 99
    return hook

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/info", methods=["POST"])
def get_info():
    data = request.get_json()
    url = data.get("url", "")
    if not url:
        return jsonify({"error": "URL manquante"}), 400
    try:
        ydl_opts = get_ydl_base_opts()
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            return jsonify({
                "title": info.get("title", "Titre inconnu"),
                "thumbnail": info.get("thumbnail", ""),
                "duration": info.get("duration", 0),
                "uploader": info.get("uploader", ""),
            })
    except Exception as e:
        error_msg = str(e)
        if "Sign in to confirm" in error_msg or "bot" in error_msg.lower():
            return jsonify({"error": "YouTube a bloqué la requête (détection bot). Vérifiez les cookies ou configurez YT_PO_TOKEN."}), 403
        return jsonify({"error": error_msg}), 500

@app.route("/start_download", methods=["POST"])
def start_download():
    data = request.get_json()
    url = data.get("url", "")
    format_type = data.get("format", "mp4")
    quality = data.get("quality", "best")

    if not url:
        return jsonify({"error": "URL manquante"}), 400

    job_id = str(uuid.uuid4())
    temp_dir = tempfile.mkdtemp()
    progress_store[job_id] = {'status': 'starting', 'percent': 0, 'filepath': None, 'filename': None, 'error': None}

    def run():
        try:
            base = get_ydl_base_opts()

            if format_type == "mp3":
                ydl_opts = {
                    **base,
                    "format": "bestaudio/best",
                    "outtmpl": os.path.join(temp_dir, "%(title)s.%(ext)s"),
                    "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
                    "progress_hooks": [make_progress_hook(job_id)],
                }
            else:
                if quality == "720":
                    fmt = "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]"
                elif quality == "480":
                    fmt = "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480][ext=mp4]/best[height<=480]"
                else:
                    fmt = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"

                ydl_opts = {
                    **base,
                    "format": fmt,
                    "outtmpl": os.path.join(temp_dir, "%(title)s.%(ext)s"),
                    "merge_output_format": "mp4",
                    "progress_hooks": [make_progress_hook(job_id)],
                }

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            files = os.listdir(temp_dir)
            if files:
                progress_store[job_id]['status'] = 'done'
                progress_store[job_id]['percent'] = 100
                progress_store[job_id]['filepath'] = os.path.join(temp_dir, files[0])
                progress_store[job_id]['filename'] = files[0]
            else:
                progress_store[job_id]['status'] = 'error'
                progress_store[job_id]['error'] = 'Fichier introuvable après téléchargement'

        except Exception as e:
            error_msg = str(e)
            if "Sign in to confirm" in error_msg or "bot" in error_msg.lower():
                error_msg = "YouTube a bloqué la requête (détection bot). Vérifiez les cookies ou configurez YT_PO_TOKEN."
            progress_store[job_id]['status'] = 'error'
            progress_store[job_id]['error'] = error_msg

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": job_id})

@app.route("/progress/<job_id>")
def progress(job_id):
    def generate():
        import time
        while True:
            info = progress_store.get(job_id, {})
            yield f"data: {json.dumps(info)}\n\n"
            if info.get('status') in ('done', 'error'):
                break
            time.sleep(0.5)
    return Response(generate(), mimetype='text/event-stream')

@app.route("/get_file/<job_id>")
def get_file(job_id):
    info = progress_store.get(job_id, {})
    if info.get('status') != 'done':
        return jsonify({"error": "Fichier pas prêt"}), 400
    filepath = info['filepath']
    filename = info['filename']
    return send_file(filepath, as_attachment=True, download_name=filename)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port, debug=False)
