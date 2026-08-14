"""Build compact transparent toolbar PNGs from the user-supplied icon files."""

from pathlib import Path

from PIL import Image, ImageChops, ImageOps


SOURCE = Path(r"C:\Users\xander\OneDrive - iicx\Desktop")
DESTINATION = Path(__file__).with_name("assets") / "toolbar"


def transparent_icon(source_name: str, target_name: str) -> None:
    with Image.open(SOURCE / source_name) as source:
        image = source.convert("RGBA")
    alpha = image.getchannel("A")
    rgb = image.convert("RGB")
    darkness = ImageChops.invert(ImageOps.grayscale(rgb))
    # Preserve source alpha (WEBP) and turn the white JPG background fully
    # transparent.  Soft edges remain antialiased.
    alpha = ImageChops.multiply(alpha, darkness)
    foreground = Image.new("RGBA", image.size, (0, 0, 0, 0))
    foreground.putalpha(alpha)
    bounds = alpha.getbbox()
    if bounds is None:
        raise ValueError(f"No visible icon in {source_name}")
    cropped = foreground.crop(bounds)
    cropped.thumbnail((18, 18), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (18, 18), (0, 0, 0, 0))
    canvas.alpha_composite(cropped, ((18 - cropped.width) // 2, (18 - cropped.height) // 2))
    canvas.save(DESTINATION / target_name, "PNG")


if __name__ == "__main__":
    DESTINATION.mkdir(parents=True, exist_ok=True)
    transparent_icon("add.webp", "add.png")
    transparent_icon("remove.jpg", "remove.png")
    transparent_icon("bin.jpg", "clear.png")
