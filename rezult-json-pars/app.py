import json
import os
from flask import Flask, abort, jsonify, render_template


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_JSON_DIR = os.path.join(BASE_DIR, "json")
JSON_DIR = os.environ.get("FFUF_JSON_DIR", DEFAULT_JSON_DIR)

app = Flask(__name__, static_folder="static", template_folder="templates")


def list_json_files():
    if not os.path.isdir(JSON_DIR):
        return []
    files = []
    for entry in os.listdir(JSON_DIR):
        if entry.endswith(".json") and os.path.isfile(os.path.join(JSON_DIR, entry)):
            files.append(entry)
    return sorted(files)


def load_file_data(file_name):
    file_path = os.path.join(JSON_DIR, file_name)
    if not os.path.isfile(file_path):
        return None
    with open(file_path, "r", encoding="utf-8") as handle:
        data = json.load(handle)
    if isinstance(data, dict):
        results = data.get("results", [])
        meta = {
            "commandline": data.get("commandline"),
            "time": data.get("time"),
        }
        return {"results": results, "meta": meta}
    if isinstance(data, list):
        return {"results": data, "meta": {}}
    return {"results": [], "meta": {}}


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/files")
def api_files():
    return jsonify({"files": list_json_files()})


@app.route("/api/file/<path:file_name>")
def api_file(file_name):
    safe_name = os.path.basename(file_name)
    if safe_name not in list_json_files():
        abort(404)
    data = load_file_data(safe_name)
    if data is None:
        abort(404)
    return jsonify({"file": safe_name, "results": data["results"], "meta": data["meta"]})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    app.run(host="0.0.0.0", port=port, debug=False)
