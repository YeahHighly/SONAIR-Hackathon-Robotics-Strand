from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
import urllib.error
import urllib.request
from pathlib import Path


MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.5")
PORT = int(os.environ.get("PORT", "8787"))
ROOT = Path(__file__).parent


def send_json(handler, status, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Access-Control-Allow-Origin", "*")
    handler.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
    handler.send_header("Access-Control-Allow-Headers", "Content-Type")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def load_evaluation():
    path = ROOT / "sub-portals" / "csmm" / "evaluation.json"
    if not path.exists():
      path = ROOT / "evaluation.json"
    return json.loads(path.read_text(encoding="utf-8"))


def local_summary(evaluation):
    metrics = evaluation.get("metrics", {})
    cases = evaluation.get("critical_cases", [])
    first = cases[0] if cases else {}
    pose = first.get("pose", {})
    lines = [
        f"System-generated summary: {evaluation.get('assessment', {}).get('label', 'assessment ready')}.",
        (
            "Physical Response Risk Window is "
            f"{metrics.get('physical_response_risk_window_ms')} ms, combining RTT/command tail delay "
            "with physical command-to-motion response."
        ),
        (
            f"RTT P95 is {metrics.get('p95_rtt_ms')} ms, command-delay P95 is "
            f"{metrics.get('p95_command_delay_ms')} ms, and motion-response P95 is "
            f"{metrics.get('p95_motion_response_ms')} ms."
        ),
        (
            "Movement Accuracy@10mm is "
            f"{metrics.get('movement_accuracy_at_10mm_pct')}%, with a Recovery-aware Error Timeline score of "
            f"{metrics.get('recovery_aware_trajectory_score')}. These use the computed ideal-vs-actual "
            "trajectory errors from the local robot telemetry."
        ),
    ]
    if pose:
        lines.append(
            "Representative critical pose: "
            f"{first.get('command_type')} at TCP x={pose.get('x_m')}, "
            f"y={pose.get('y_m')}, z={pose.get('z_m')} m."
        )
    lines.append("No visitor text input or file upload is used; this is derived from backend metrics only.")
    return "\n".join(lines)


def extract_text(response):
    if response.get("output_text"):
        return response["output_text"]
    chunks = []
    for item in response.get("output", []):
        for content in item.get("content", []):
            text = content.get("text")
            if text:
                chunks.append(text)
    return "\n".join(chunks).strip()


def gpt_summary(evaluation):
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return None

    compact = {
        "node_id": evaluation.get("node_id"),
        "metrics": evaluation.get("metrics", {}),
        "critical_cases": evaluation.get("critical_cases", []),
        "assessment": evaluation.get("assessment", {}),
    }
    request_body = {
        "model": MODEL,
        "instructions": (
            "You are a robotics evidence analyst. Use only the supplied computed metrics and critical pose cases. "
            "Write concise plain-English findings for a hackathon demo. Do not claim certification, do not ask for "
            "user input, and do not mention prompts or API calls."
        ),
        "input": (
            "Generate 5 short bullets explaining the RTT/command-delay/motion-lag evidence and why the highlighted "
            "TCP poses and movement-accuracy metrics are representative cases:\n"
            + json.dumps(compact, ensure_ascii=False)
        ),
        "max_output_tokens": 650,
    }
    req = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=json.dumps(request_body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=45) as res:
        return extract_text(json.loads(res.read().decode("utf-8")))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        return

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        if self.path not in ("/api/insights", "/api/insights/"):
            send_json(self, 404, {"error": "not found"})
            return

        try:
            evaluation = load_evaluation()
            summary = gpt_summary(evaluation) or local_summary(evaluation)
            source = "openai" if os.environ.get("OPENAI_API_KEY") else "local-rules"
            send_json(self, 200, {"source": source, "summary": summary})
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            send_json(self, exc.code, {"error": detail, "summary": local_summary(load_evaluation())})
        except Exception as exc:
            send_json(self, 500, {"error": str(exc)})


if __name__ == "__main__":
    print(f"Analysis backend running at http://127.0.0.1:{PORT}/api/insights")
    print("OPENAI_API_KEY is optional. If set, the backend uses fixed metrics to generate a model summary.")
    HTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
