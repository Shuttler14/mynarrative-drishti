import http from "k6/http";
import { check } from "k6";
import { BASE } from "../lib/config.js";

const Q = ["red saree", "kurta", "lehenga", "blue shirt", "anarkali"];

export default function (token, userId) {
  const q = Q[Math.floor(Math.random() * Q.length)];
  const res = http.get(
    `${BASE}/v1/products/search?q=${encodeURIComponent(q)}`,
    {
      headers: {
        Authorization: `Bearer ${token}`,
        "X-User-Id": userId,
      },
      tags: { scenario: "search" },
    }
  );
  check(res, { "search 200": (r) => r.status === 200 });
}
