# CometAPI for AUTOMATIC1111

An experimental extension for AUTOMATIC1111 Stable Diffusion WebUI. It adds a **CometAPI** tab that submits cloud generation jobs and saves returned images and videos under `outputs/cometapi`.

The tab provides SDXL and Stable Diffusion 3.5 Medium through CometAPI's Replicate-compatible routes, and Sora 2 / Sora 2 Pro through its video routes. Model availability and charges depend on CometAPI account access and the selected route.

The extension's media transport is also the canonical source for generated CometAPI Connect assets. It does not redirect the stock txt2img/img2img pipeline or provide local video inference. Standard WebUI tabs still require their own checkpoints.

## Install and use

1. Copy this directory into the WebUI's `extensions/cometapi-connect` directory and restart WebUI.
2. Open the **CometAPI** tab and enter your API key, or set `COMETAPI_API_KEY` in the WebUI server environment. Manually entered keys are not saved by the tab. When CometAPI Connect installs the integration, it stores the key in a private `cometapi.json` configuration file that the extension reads for each job.
3. Select a model, enter a prompt, and choose **Generate**.

Use WebUI on loopback and keep its configuration accessible only to trusted users. Each generation submits a paid job. The tab displays the job ID and polls for up to ten minutes. An ambiguous submission or timeout is not automatically resubmitted; retain the job ID and check its status before submitting another job.

Only `https://api.cometapi.com` receives the key. Image delivery downloads use allowed HTTPS Replicate hosts without authentication. Video downloads use the authenticated CometAPI video route. Authenticated requests and downloads do not follow redirects. Outputs have size limits and are decoded or container-checked before completion is reported. Local JSON job records contain the prompt, model, job ID, output path, and file hash; treat prompts and generated content as private data.

## Compatibility and testing

The integration targets AUTOMATIC1111 WebUI v1.10.1. Test your environment before relying on it; Windows and Linux execution are not established by the repository's offline checks. If that WebUI release encounters its removed Stable Diffusion dependency, consult the [upstream issue and workaround](https://github.com/AUTOMATIC1111/stable-diffusion-webui/issues/17213).

Run the offline extension tests using an installed WebUI environment:

```sh
A1111_ROOT=/path/to/stable-diffusion-webui /path/to/webui-venv/bin/python test_extension.py
```

These checks cover response handling, delivery-host restrictions, credential handling, redirect refusal, ambiguous submissions, and output errors. They do not make paid requests. After changing `scripts/cometapi_media.py`, run `python scripts/sync_media_assets.py` from the CometAPI Connect repository root to update generated assets.

API references: [Replicate predictions](https://apidoc.cometapi.com/api/image/replicate/create-predictions-general), [prediction polling](https://apidoc.cometapi.com/api/image/replicate/replicate-query), and [Sora video jobs](https://apidoc.cometapi.com/quickstarts/video/sora-2-api).
