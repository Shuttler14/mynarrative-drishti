"""Fine-tune IDM-VTON's UNet attention on Indian ethnic wear via LoRA.
T4-friendly: rank 16, batch 2, FP16, gradient accumulation.
One LoRA per garment family (saree/lehenga/kurta/sherwani/dupatta).

Dataset layout (paired VTON format):
  data/<subtype>/{person/, garment/, agnostic/, mask/, pose/}
Run: python -m vtoe.training.train_lora --subtype saree --data data/saree --epochs 15
"""
from __future__ import annotations
import argparse

import torch
from diffusers import StableDiffusionXLControlNetInpaintPipeline
from peft import LoraConfig, get_peft_model
from torch.utils.data import DataLoader, Dataset
from torchvision import transforms
from PIL import Image
import pathlib


class EthnicVTONDataset(Dataset):
    def __init__(self, root: str, size=(768, 1024)) -> None:
        self.root = pathlib.Path(root)
        self.ids = [p.stem for p in (self.root / "person").glob("*.jpg")]
        self.tf = transforms.Compose([transforms.Resize(size[::-1]), transforms.ToTensor(),
                                      transforms.Normalize([0.5]*3, [0.5]*3)])

    def __len__(self): return len(self.ids)

    def _load(self, sub, i):
        return self.tf(Image.open(self.root / sub / f"{i}.jpg").convert("RGB"))

    def __getitem__(self, idx):
        i = self.ids[idx]
        return {"person": self._load("person", i), "agnostic": self._load("agnostic", i),
                "garment": self._load("garment", i), "pose": self._load("pose", i),
                "mask": self._load("mask", i)[:1]}


def train(subtype: str, data: str, epochs: int, lr: float, rank: int, output: str | None = None) -> None:
    from vtoe.config import settings
    pipe = StableDiffusionXLControlNetInpaintPipeline.from_pretrained(
        settings.idm_vton, torch_dtype=torch.float16, cache_dir=settings.model_cache)
    unet = pipe.unet
    unet.requires_grad_(False)

    lora = LoraConfig(r=rank, lora_alpha=rank, init_lora_weights="gaussian",
                      target_modules=["to_q", "to_k", "to_v", "to_out.0"])
    unet = get_peft_model(unet, lora).to(settings.device)
    unet.train()

    ds = DataLoader(EthnicVTONDataset(data), batch_size=2, shuffle=True)
    opt = torch.optim.AdamW([p for p in unet.parameters() if p.requires_grad], lr=lr)
    scaler = torch.cuda.amp.GradScaler()
    vae, text_enc = pipe.vae.to(settings.device), pipe.text_encoder.to(settings.device)
    noise_sched = pipe.scheduler

    accum = 4
    for epoch in range(epochs):
        for step, batch in enumerate(ds):
            with torch.autocast("cuda", dtype=torch.float16):
                latents = vae.encode(batch["person"].to(settings.device).half()).latent_dist.sample() * vae.config.scaling_factor
                noise = torch.randn_like(latents)
                t = torch.randint(0, noise_sched.config.num_train_timesteps, (latents.shape[0],), device=latents.device)
                noisy = noise_sched.add_noise(latents, noise, t)
                cond = vae.encode(batch["garment"].to(settings.device).half()).latent_dist.sample() * vae.config.scaling_factor
                pred = unet(torch.cat([noisy, cond], dim=1)[:, :4], t,
                            encoder_hidden_states=None).sample
                loss = torch.nn.functional.mse_loss(pred.float(), noise.float()) / accum
            scaler.scale(loss).backward()
            if (step + 1) % accum == 0:
                scaler.step(opt); scaler.update(); opt.zero_grad()
        print(f"epoch {epoch} loss {loss.item()*accum:.4f}")

    out = output or f"loras/lora-{subtype}"
    unet.save_pretrained(out)
    print(f"saved {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--subtype", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--epochs", type=int, default=15)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--rank", type=int, default=16)
    ap.add_argument("--output", type=str, default=None, help="Output directory for LoRA weights")
    a = ap.parse_args()
    train(a.subtype, a.data, a.epochs, a.lr, a.rank, a.output)
