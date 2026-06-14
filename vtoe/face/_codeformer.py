from __future__ import annotations
import os

import cv2
import numpy as np
import torch
from PIL import Image

from vtoe.utils.imaging import to_cv, to_pil


class CodeFormerRestorer:
    def __init__(self, settings) -> None:
        self.s = settings
        self.device = settings.device
        self.w = 0.7
        self._net = None
        self._face_helper = None

    def _lazy(self):
        if self._net is not None:
            return
        from basicsr.utils.registry import ARCH_REGISTRY
        from facelib.utils.face_restoration_helper import FaceRestoreHelper

        ckpt = os.path.join(self.s.model_cache, "codeformer", "codeformer.pth")
        net = ARCH_REGISTRY.get("CodeFormer")(
            dim_embd=512, codebook_size=1024, n_head=8, n_layers=9,
            connect_list=["32", "64", "128", "256"]).to(self.device)
        state = torch.load(ckpt, map_location=self.device)["params_ema"]
        net.load_state_dict(state)
        net.eval()
        self._net = net
        self._face_helper = FaceRestoreHelper(
            upscale_factor=1, face_size=512, crop_ratio=(1, 1),
            det_model="retinaface_resnet50", use_parse=True, device=self.device)

    @torch.inference_mode()
    def enhance(self, img: Image.Image) -> Image.Image:
        self._lazy()
        bgr = to_cv(img)
        self._face_helper.clean_all()
        self._face_helper.read_image(bgr)
        n = self._face_helper.get_face_landmarks_5(only_center_face=True, resize=640)
        if n == 0:
            return img
        self._face_helper.align_warp_face()

        for cropped in self._face_helper.cropped_faces:
            t = self._to_tensor(cropped)
            try:
                out = self._net(t, w=self.w, adain=True)[0]
            except Exception:
                continue
            restored = self._from_tensor(out)
            self._face_helper.add_restored_face(restored, cropped)

        self._face_helper.get_inverse_affine(None)
        result = self._face_helper.paste_faces_to_input_image()
        return to_pil(result)

    def _to_tensor(self, face_bgr: np.ndarray) -> torch.Tensor:
        rgb = cv2.cvtColor(face_bgr, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        t = torch.from_numpy(rgb).permute(2, 0, 1).unsqueeze(0)
        return ((t - 0.5) / 0.5).to(self.device)

    def _from_tensor(self, t: torch.Tensor) -> np.ndarray:
        t = (t.clamp(-1, 1) + 1) / 2
        arr = (t.permute(1, 2, 0).cpu().numpy() * 255).round().astype(np.uint8)
        return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)
