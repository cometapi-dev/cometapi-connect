"""CometAPI Connect nodes using ComfyUI's public node and preview APIs."""

from pathlib import Path

import numpy as np
import torch
from comfy_api.latest import ComfyExtension, InputImpl, io, ui
from PIL import Image

from . import cometapi_cloud as cloud


def generate(kind, model, prompt):
    result = None
    for result in cloud.generate(kind, model, prompt, ""):
        pass
    if not result or not (result[0] if kind == "image" else result[1]):
        raise RuntimeError(result[2] if result else "CometAPI returned no result")
    return Path(result[0] if kind == "image" else result[1])


class CometAPIImage(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="CometAPIConnectImage",
            display_name="CometAPI Image",
            category="CometAPI",
            is_output_node=True,
            inputs=[
                io.Combo.Input("model", options=cloud.IMAGE_MODELS),
                io.String.Input("prompt", multiline=True),
            ],
            outputs=[io.Image.Output()],
        )

    @classmethod
    def execute(cls, model, prompt):
        path = generate("image", model, prompt)
        with Image.open(path) as image:
            tensor = torch.from_numpy(
                np.array(image.convert("RGB")).astype(np.float32) / 255.0
            ).unsqueeze(0)
        return io.NodeOutput(tensor, ui=ui.PreviewImage(tensor, cls=cls))


class CometAPIVideo(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        return io.Schema(
            node_id="CometAPIConnectVideo",
            display_name="CometAPI Video (4 seconds)",
            category="CometAPI",
            is_output_node=True,
            inputs=[
                io.Combo.Input("model", options=cloud.VIDEO_MODELS),
                io.String.Input("prompt", multiline=True),
            ],
            outputs=[io.Video.Output()],
        )

    @classmethod
    def execute(cls, model, prompt):
        path = generate("video", model, prompt)
        return io.NodeOutput(
            InputImpl.VideoFromFile(str(path)),
            ui=ui.PreviewVideo([ui.SavedResult(path.name, "cometapi", io.FolderType.output)]),
        )


class CometAPIExtension(ComfyExtension):
    async def get_node_list(self):
        return [CometAPIImage, CometAPIVideo]


async def comfy_entrypoint():
    return CometAPIExtension()
