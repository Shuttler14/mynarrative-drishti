export const BASE = __ENV.BASE_URL || "https://staging-api.drishti.example";
export const VTO_BASE = __ENV.VTO_BASE_URL || BASE;

// SLOs asserted as k6 thresholds — a failing run fails CI/launch gate.
export const THRESHOLDS = {
  "http_req_duration{scenario:reco}": ["p(95)<800", "p(50)<200"],
  "http_req_duration{scenario:tryon_submit}": ["p(95)<2000"],
  "tryon_e2e_seconds": ["p(50)<15", "p(95)<30"],
  "http_req_failed": ["rate<0.02"],
  "tryon_quality_pass": ["rate>0.85"],
};
