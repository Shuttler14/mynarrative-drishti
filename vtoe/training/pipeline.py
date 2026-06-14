"""
Full production training pipeline:
1. Scrape real data from Myntra/Flipkart/Amazon
2. Fall back to synthetic data if blocked
3. Process into VTON format
4. Train LoRAs

Run: python -m vtoe.training.pipeline --subtypes saree,lehenga,kurta --target 2000
"""
from __future__ import annotations
import asyncio
import subprocess
import sys
import pathlib
import argparse
import json
from datetime import datetime


def run_cmd(cmd: list[str], timeout: int = 3600) -> tuple[int, str]:
    """Run command and return (returncode, output)."""
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return result.returncode, result.stdout + result.stderr


def main():
    ap = argparse.ArgumentParser(description="Full VTOE training pipeline")
    ap.add_argument("--subtypes", default="saree,lehenga,kurta",
                    help="Comma-separated subtypes to train")
    ap.add_argument("--target", type=int, default=2000,
                    help="Target samples per subtype")
    ap.add_argument("--epochs", type=int, default=15)
    ap.add_argument("--output", default="trained_loras")
    a = ap.parse_args()

    subtypes = [s.strip() for s in a.subtypes.split(",")]
    output = pathlib.Path(a.output)
    output.mkdir(parents=True, exist_ok=True)

    report = {"start": datetime.now().isoformat(), "subtypes": {}}

    for subtype in subtypes:
        print(f"\n{'='*60}")
        print(f"  PIPELINE: {subtype.upper()}")
        print(f"{'='*60}")

        data_dir = pathlib.Path(f"data/{subtype}")

        # Step 1: Try scraping real data
        print(f"\n[1/4] Scraping real data (target: {a.target} images)...")
        try:
            rc, out = run_cmd([
                sys.executable, "-m", "vtoe.training.scrape_indian_fashion",
                "--subtype", subtype, "--max", str(a.target),
            ], timeout=1800)
            print(out[-500:] if len(out) > 500 else out)
        except Exception as e:
            print(f"Scraping failed: {e}")

        # Step 2: Check what we got, fill with synthetic
        raw_dir = data_dir / "raw"
        real_count = len(list(raw_dir.glob("*.jpg"))) if raw_dir.exists() else 0
        print(f"\nReal images found: {real_count}")

        if real_count < a.target:
            synthetic_needed = a.target - real_count
            print(f"Generating {synthetic_needed} synthetic pairs...")
            try:
                rc, out = run_cmd([
                    sys.executable, "-m", "vtoe.training.prepare_data",
                    "--subtype", subtype,
                    "--output", str(data_dir),
                    "--max-samples", str(synthetic_needed),
                ], timeout=600)
                print(out[-300:] if len(out) > 300 else out)
            except Exception as e:
                print(f"Synthetic generation failed: {e}")

        # Step 3: Process raw images into VTON format
        print(f"\n[3/4] Processing into VTON format...")
        # TODO: Run BiRefNet + OpenPose on real images
        # For now, synthetic data is already in VTON format

        # Step 4: Train LoRA
        print(f"\n[4/4] Training {subtype} LoRA ({a.epochs} epochs)...")
        lora_path = output / f"lora-{subtype}"
        try:
            rc, out = run_cmd([
                sys.executable, "-m", "vtoe.training.train_lora",
                "--subtype", subtype,
                "--data", str(data_dir),
                "--epochs", str(a.epochs),
                "--output", str(lora_path),
            ], timeout=7200)
            print(out[-500:] if len(out) > 500 else out)
            success = rc == 0
        except Exception as e:
            print(f"Training failed: {e}")
            success = False

        report["subtypes"][subtype] = {
            "real_images": real_count,
            "target": a.target,
            "lora_path": str(lora_path),
            "success": success,
        }

    # Summary
    report["end"] = datetime.now().isoformat()
    report_path = output / "training_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2)

    print(f"\n{'='*60}")
    print(f"  PIPELINE COMPLETE")
    print(f"{'='*60}")
    for subtype, info in report["subtypes"].items():
        status = "✓" if info["success"] else "✗"
        print(f"  {status} {subtype}: {info['real_images']}/{info['target']} images")
    print(f"\nReport: {report_path}")
    print(f"Next: Upload LoRAs to HuggingFace and update lora_registry.py")


if __name__ == "__main__":
    main()
