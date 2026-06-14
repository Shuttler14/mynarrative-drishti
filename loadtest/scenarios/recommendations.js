import http from "k6/http";
import { check, sleep } from "k6";
import { BASE } from "../lib/config.js";

const OCCASIONS = [
  "fest_diwali",
  "wedding_sangeet",
  "corp_formal",
  "cas_daily",
];

export default function (token, userId) {
  const occ = OCCASIONS[Math.floor(Math.random() * OCCASIONS.length)];
  const res = http.post(
    `${BASE}/v1/recommendations`,
    JSON.stringify({ occasion: occ, budget_min: 100000, budget_max: 500000 }),
    {
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
        "X-User-Id": userId,
      },
      tags: { scenario: "reco" },
    }
  );
  check(res, {
    "reco 200": (r) => r.status === 200,
    "has looks": (r) => (r.json("looks") || []).length > 0,
  });
  sleep(Math.random() * 3 + 1); // think time
}
