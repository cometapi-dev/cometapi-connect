# Application icons

`logo.png` is the CometAPI Connect source artwork: a 200 × 200 transparent PNG. `cometapi-connect.ico` and `cometapi-connect.icns` contain resized versions for Windows and macOS; larger sizes are upscaled from the source.

Windows and macOS builds include these icons through `scripts/build_release.py`. Release builds do not require an image library.

To regenerate icons, install Pillow in a development environment and run:

```sh
python scripts/build_icons.py
```

Review the resulting Windows and macOS icons before including them in a release. Product names and branding identify the project; the source-code license does not grant trademark rights.
