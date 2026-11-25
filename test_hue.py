#!/usr/bin/env python3
"""Standalone GTK hue test for the countdown digit PNGs."""

from pathlib import Path
import colorsys
import io

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("GdkPixbuf", "2.0")
from gi.repository import GdkPixbuf, Gtk
from PIL import Image


DIGIT_DIR = Path(__file__).resolve().parent / "assets" / "digits"


class HueTest(Gtk.Window):
    def __init__(self):
        super().__init__(title="Shock Timer — Digit Hue Test")
        self.set_border_width(20)
        self.set_resizable(False)
        self.digits = {str(n): Image.open(DIGIT_DIR / f"{n}.png").convert("RGBA") for n in range(10)}
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.add(box)
        title = Gtk.Label(label="Countdown digit hue")
        title.get_style_context().add_class("title")
        box.pack_start(title, False, False, 0)
        self.value = Gtk.Label(label="Hue: 0°")
        box.pack_start(self.value, False, False, 0)
        self.scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 359, 1)
        self.scale.set_size_request(360, -1)
        self.scale.set_draw_value(False)
        self.scale.connect("value-changed", self.update_digits)
        box.pack_start(self.scale, False, False, 0)
        preview = Gtk.Box(spacing=4)
        preview.set_halign(Gtk.Align.CENTER)
        box.pack_start(preview, False, False, 0)
        self.images = [Gtk.Image(), Gtk.Image()]
        for image in self.images:
            preview.pack_start(image, False, False, 0)
        self.update_digits(self.scale)
        self.connect("destroy", Gtk.main_quit)
        self.show_all()

    def update_digits(self, scale):
        hue = int(scale.get_value())
        self.value.set_text(f"Hue: {hue}°")
        for digit, widget in zip("30", self.images):
            image = self.digits[digit].copy()
            pixels = image.load()
            for y in range(image.height):
                for x in range(image.width):
                    r, g, b, alpha = pixels[x, y]
                    brightness = max(r, g, b) / 255
                    rr, gg, bb = colorsys.hsv_to_rgb(hue / 360, 1, brightness)
                    pixels[x, y] = (round(rr * 255), round(gg * 255), round(bb * 255), alpha)
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            loader = GdkPixbuf.PixbufLoader.new_with_type("png")
            loader.write(buffer.getvalue())
            loader.close()
            pixbuf = loader.get_pixbuf().scale_simple(96, 128, GdkPixbuf.InterpType.NEAREST)
            widget.set_from_pixbuf(pixbuf)


if __name__ == "__main__":
    HueTest()
    Gtk.main()
