import http from "k6/http";
import { check, sleep } from "k6";
import { Trend, Rate } from "k6/metrics";
import { VTO_BASE } from "../lib/config.js";

const e2e = new Trend("tryon_e2e_seconds");
const qpass = new Rate("tryon_quality_pass");

export default function (token, userId, garment) {
  const start = Date.now();
  // 1. submit
  const submit = http.post(
    `${VTO_BASE}/v1/tryon`,
    JSON.stringify({ look_id: garment.look_id }),
    {
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
        "X-User-Id": userId,
      },
      tags: { scenario: "tryon_submit" },
    }
  );
  if (!check(submit, { "tryon accepted (202)": (r) => r.status === 202 })) return;
  const sessionId = submit.json("session_id");

  // 2. poll status until completed/failed (caps the GPU path)
  let done = false,
    attempts = 0,
    body = {};
  while (!done && attempts < 60) {
    // up to ~60s
    sleep(1);
    const st = http.get(`${VTO_BASE}/v1/tryon/${sessionId}/status`, {
      headers: { Authorization: `Bearer ${token}`, "X-User-Id": userId },
      tags: { scenario: "tryon_poll" },
    });
    body = st.json();
    done =
      body.status === "completed" ||
      body.status === "failed" ||
      body.status === "completed_low_quality";
    attempts++;
  }
  e2e.add((Date.now() - start) / 1000);
  qpass.add(body.status === "completed");
  check(body, { "tryon completed": (b) => b.status !== "failed" });
}
