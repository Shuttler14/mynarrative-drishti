import http from "k6/http";
import { check } from "k6";
import { BASE } from "./config.js";

// Pre-provision test users; here we mint tokens via OTP test bypass in staging.
export function login(phone) {
  http.post(
    `${BASE}/v1/auth/otp/request`,
    JSON.stringify({ phone }),
    { headers: { "Content-Type": "application/json" } }
  );
  // staging exposes a deterministic test OTP via a header-gated endpoint
  const res = http.post(
    `${BASE}/v1/auth/otp/verify`,
    JSON.stringify({ phone, code: __ENV.TEST_OTP || "000000" }),
    {
      headers: {
        "Content-Type": "application/json",
        "X-Test-Mode": __ENV.TEST_KEY,
      },
    }
  );
  check(res, { "login 200": (r) => r.status === 200 });
  return res.json("access_token");
}
