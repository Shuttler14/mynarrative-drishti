#!/usr/bin/env python3
"""RECO-v4.5 — human-label workbench for VTON golden cases.

Seven dimensions per case (all NULL until labelled → gates report NO-DATA):
  garment_fidelity  0-100  garment appearance preserved vs source
  leakage_pct       0-100  % of the output wrongly covered by garment
  identity_ok       bool   person recognisably the same
  body_preservation 0-100  pose/body shape preserved
  boundary_quality  0-100  edge/blend quality around the garment
  artifacts         int    visible artifact count (0 = clean)
  overall           0-100  overall visual correctness

Usage:
  python bench/label_vton.py --list
  python bench/label_vton.py --show v01
  python bench/label_vton.py --case v01 --fidelity 95 --leakage 0 \
      --identity ok --body 96 --boundary 92 --artifacts 0 --overall 94 --by alice
  python bench/label_vton.py --interactive --by alice
"""
from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import sys

CASES_PATH = pathlib.Path(__file__).parent / "golden" / "vton_cases.json"
SCORE_DIMS = ("fidelity:garment_fidelity", "leakage:leakage_pct",
              "body:body_preservation", "boundary:boundary_quality",
              "overall:overall")


def _load() -> dict:
    return json.loads(CASES_PATH.read_text())


def _save(data: dict) -> None:
    CASES_PATH.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")


def _fmt(v) -> str:
    return "—" if v is None else str(v)


def _find(cases: list, case_id: str) -> dict:
    for c in cases:
        if c.get("id") == case_id:
            return c
    sys.exit(f"unknown case id: {case_id} (try --list)")


def _score(label: str) -> float:
    v = float(label)
    if not 0 <= v <= 100:
        raise argparse.ArgumentTypeError(f"{label}: must be 0-100")
    return v


def _bool(label: str):
    t = label.strip().lower()
    if t in ("ok", "true", "1", "yes", "y"):
        return True
    if t in ("bad", "false", "0", "no", "n"):
        return False
    raise argparse.ArgumentTypeError(f"{label}: use ok/bad")


def cmd_list(data: dict) -> None:
    print(f"{'id':<5} {'by':<10} {'fid':>5} {'leak':>6} {'idn':>5} "
          f"{'body':>5} {'bnd':>5} {'art':>4} {'ovr':>5}  case")
    for c in data["cases"]:
        lb = c.get("labels") or {}
        marked = lb.get("labelled_by")
        print(f"{c['id']:<5} {_fmt(marked):<10} {_fmt(lb.get('garment_fidelity')):>5} "
              f"{_fmt(lb.get('leakage_pct')):>6} {_fmt(lb.get('identity_ok')):>5} "
              f"{_fmt(lb.get('body_preservation')):>5} {_fmt(lb.get('boundary_quality')):>5} "
              f"{_fmt(lb.get('artifacts')):>4} {_fmt(lb.get('overall')):>5}  "
              f"{(c.get('notes') or '')[:52]}")


def cmd_show(data: dict, case_id: str) -> None:
    print(json.dumps(_find(data["cases"], case_id), indent=2))


def _apply(case: dict, args, by: str) -> None:
    lb = case.setdefault("labels", {})
    mapping = {
        "garment_fidelity": args.fidelity,
        "leakage_pct": args.leakage,
        "identity_ok": args.identity,
        "body_preservation": args.body,
        "boundary_quality": args.boundary,
        "artifacts": args.artifacts,
        "overall": args.overall,
    }
    for key, val in mapping.items():
        if val is not None:
            lb[key] = val
    lb["labelled_by"] = by
    lb["labelled_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def cmd_set(data: dict, args) -> None:
    case = _find(data["cases"], args.case)
    _apply(case, args, args.by)
    _save(data)
    print(f"labelled {args.case} by {args.by} "
          f"(fidelity={_fmt(case['labels'].get('garment_fidelity'))}, "
          f"leakage={_fmt(case['labels'].get('leakage_pct'))}, "
          f"identity={_fmt(case['labels'].get('identity_ok'))}, "
          f"body={_fmt(case['labels'].get('body_preservation'))}, "
          f"boundary={_fmt(case['labels'].get('boundary_quality'))}, "
          f"artifacts={_fmt(case['labels'].get('artifacts'))}, "
          f"overall={_fmt(case['labels'].get('overall'))})")


def cmd_interactive(data: dict, by: str) -> None:
    for case in data["cases"]:
        lb = case.get("labels") or {}
        if lb.get("labelled_by") and input(
                f"{case['id']} already labelled by {lb['labelled_by']} — redo? [y/N] "
        ).strip().lower() not in ("y", "yes"):
            continue
        print(f"\n=== {case['id']} — {case.get('notes', '')}")
        print(f"    person:   {(case.get('person') or {}).get('url', '')[:100]}")
        print(f"    garment:  {(case.get('garment') or {}).get('url', '')[:100]}")
        print("    (press Enter to leave a dimension unchanged / null)")
        args = argparse.Namespace(fidelity=None, leakage=None, identity=None,
                                  body=None, boundary=None, artifacts=None, overall=None)
        for attr, key, kind in [
            ("fidelity", "garment_fidelity", "score"),
            ("leakage", "leakage_pct", "score"),
            ("identity", "identity_ok", "bool"),
            ("body", "body_preservation", "score"),
            ("boundary", "boundary_quality", "score"),
            ("artifacts", "artifacts", "int"),
            ("overall", "overall", "score"),
        ]:
            raw = input(f"    {key} [{_fmt(lb.get(key))}]: ").strip()
            if not raw:
                continue
            try:
                if kind == "bool":
                    setattr(args, attr, _bool(raw))
                elif kind == "int":
                    setattr(args, attr, int(raw))
                else:
                    setattr(args, attr, _score(raw))
            except (ValueError, argparse.ArgumentTypeError) as e:
                print(f"      skipped ({e})")
        _apply(case, args, by)
    _save(data)
    print("\nsaved.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--list", action="store_true", help="table of cases + labels")
    ap.add_argument("--show", metavar="ID", help="print one case as JSON")
    ap.add_argument("--case", metavar="ID", help="label this case")
    ap.add_argument("--interactive", action="store_true", help="walk every case")
    ap.add_argument("--by", default="", help="labeller name (required to write)")
    for attr, _key, _kind in [(a, k, t) for a, k, t in [
        ("fidelity", "garment_fidelity", "score"),
        ("leakage", "leakage_pct", "score"),
        ("identity", "identity_ok", "bool"),
        ("body", "body_preservation", "score"),
        ("boundary", "boundary_quality", "score"),
        ("artifacts", "artifacts", "int"),
        ("overall", "overall", "score"),
    ]]:
        ap.add_argument(f"--{attr}", help=f"{_key} ({_kind})")
    args = ap.parse_args()

    data = _load()
    if args.list:
        cmd_list(data)
    elif args.show:
        cmd_show(data, args.show)
    elif args.case:
        if not args.by:
            ap.error("--case requires --by <labeller>")
        cmd_set(data, args)
    elif args.interactive:
        if not args.by:
            ap.error("--interactive requires --by <labeller>")
        cmd_interactive(data, args.by)
    else:
        ap.print_help()
        print("\nunlabelled cases:", sum(
            1 for c in data["cases"]
            if not (c.get("labels") or {}).get("labelled_by")))


if __name__ == "__main__":
    main()
