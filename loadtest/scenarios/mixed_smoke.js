import { login } from "../lib/auth.js";
import reco from "./recommendations.js";
import tryon from "./tryon.js";
import search from "./product_search.js";
import { SharedArray } from "k6/data";
import { THRESHOLDS } from "../lib/config.js";

const users = new SharedArray("users", () =>
  JSON.parse(open("../data/users.json"))
);
const garments = new SharedArray("garments", () =>
  JSON.parse(open("../data/garments.json"))
);

export const options = {
  thresholds: THRESHOLDS,
  scenarios: {
    // browse-heavy: most traffic is reco + search (cheap), few try-ons (GPU)
    browse: {
      executor: "ramping-vus",
      exec: "browse",
      stages: [
        { duration: "2m", target: 50 },
        { duration: "5m", target: 100 },
        { duration: "2m", target: 0 },
      ],
    },
    tryon: {
      executor: "constant-vus",
      exec: "doTryon",
      vus: 10,
      duration: "9m",
    },
  },
};

function pick(arr) {
  return arr[Math.floor(Math.random() * arr.length)];
}

export function browse() {
  const u = pick(users);
  const token = login(u.phone);
  if (Math.random() < 0.6) reco(token, u.user_id);
  else search(token, u.user_id);
}

export function doTryon() {
  const u = pick(users);
  const token = login(u.phone);
  tryon(token, u.user_id, pick(garments));
}
