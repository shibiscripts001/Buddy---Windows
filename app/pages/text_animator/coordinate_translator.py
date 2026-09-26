"""Coordinate translation math between PySide6 pixel space and Fusion normalized space."""
from typing import Tuple


class CoordinateTranslator:
    """Translates coordinates between PySide6 QGraphicsView pixel space and Fusion normalized space.

    The two spaces differ:
    - PySide6: Pixel-based, (0,0) is Top-Left. Width W, Height H.
    - Fusion: Normalized 0.0 to 1.0, (0.5, 0.5) is Dead-Center.
              Y-axis is inverted (bottom is 0.0, top is 1.0).
    """

    @staticmethod
    def pyside_to_fusion(pixel_x: float, pixel_y: float, canvas_width: float, canvas_height: float) -> Tuple[float, float]:
        """Converts PySide6 pixel coordinates (top-left 0,0) to Fusion normalized coordinates.

        Returns (fusion_x, fusion_y) where center is (0.5, 0.5), top is 1.0, bottom is 0.0.
        """
        if canvas_width <= 0 or canvas_height <= 0:
            return 0.5, 0.5

        fusion_x = pixel_x / canvas_width
        # Invert Y axis: top (pixel_y = 0) becomes 1.0, bottom (pixel_y = H) becomes 0.0
        fusion_y = 1.0 - (pixel_y / canvas_height)

        return round(fusion_x, 4), round(fusion_y, 4)

    @staticmethod
    def fusion_to_pyside(fusion_x: float, fusion_y: float, canvas_width: float, canvas_height: float) -> Tuple[float, float]:
        """Converts Fusion normalized coordinates back to PySide6 pixel coordinates."""
        if canvas_width <= 0 or canvas_height <= 0:
            return 0.0, 0.0

        pixel_x = fusion_x * canvas_width
        pixel_y = (1.0 - fusion_y) * canvas_height

        return round(pixel_x, 2), round(pixel_y, 2)
