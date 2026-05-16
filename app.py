import os
from flask import Flask, request, jsonify, send_file

from usps_direct_tracker import track_bulk

app = Flask(__name__)


@app.route("/")
def index():
    return send_file(os.path.join(os.path.dirname(__file__), "tracker-ui.html"))


@app.route("/api/track", methods=["POST"])
def api_track():
    data = request.get_json(force=True)
    numbers = data.get("tracking_numbers", [])
    if not numbers:
        return jsonify({"error": "No tracking numbers provided"}), 400

    seen = set()
    unique = [n for n in numbers if n not in seen and not seen.add(n)]

    results = track_bulk(unique)
    return jsonify(results)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
