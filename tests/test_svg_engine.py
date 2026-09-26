"""SVG Importer's pure parsing (pages/svg_importer/engine.py): path data,
length attributes, the root <svg> size and colour values - each of these
used to raise on common real-world input and abort the whole import."""

import itertools
import math
import os
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET

import _paths  # noqa: F401  (puts app/ on sys.path)

from pages.svg_importer import engine

SVG_NS = 'xmlns="http://www.w3.org/2000/svg"'


def anchors(subpath):
    return [(round(a['x'], 6), round(a['y'], 6)) for a in subpath.anchors]


def parse_with_timeout(d, seconds=5):
    """parse_path_d(d), failing the test instead of hanging the suite."""
    result = {}
    t = threading.Thread(target=lambda: result.setdefault('v', engine.parse_path_d(d)), daemon=True)
    t.start()
    t.join(seconds)
    if t.is_alive():
        raise AssertionError(f"parse_path_d({d!r}) did not return (infinite loop)")
    return result['v']


def walk_one(xml, viewport=(200.0, 100.0)):
    el = ET.fromstring(xml)
    return engine.walk(el, engine.IDENTITY, "#000000", 1.0, itertools.count(1), viewport=viewport)


class PathDataTests(unittest.TestCase):
    def test_packed_arc_flags(self):
        # SVGO output: "1010 0" is large-arc 1, sweep 0, then x=10 y=0.
        sps = engine.parse_path_d('M10 10a5 5 0 1010 0')
        self.assertEqual(len(sps), 1)
        self.assertEqual(anchors(sps[0])[-1], (20.0, 10.0))

    def test_arc_flags_match_unpacked_form(self):
        packed = engine.parse_path_d('M10 10a5 5 0 1010 0')
        spaced = engine.parse_path_d('M10 10 a5 5 0 1 0 10 0')
        self.assertEqual(anchors(packed[0]), anchors(spaced[0]))

    def test_comma_separated_arc(self):
        sps = engine.parse_path_d('M0 0a5,5,0,1,0,10,0')
        self.assertEqual(anchors(sps[0])[-1], (10.0, 0.0))

    def test_both_flags_packed_before_coordinate(self):
        # "11 10 0": flags 1 and 1, then x=10 (not flags 1, 1 and x=... "1 10").
        sps = engine.parse_path_d('M0 0A5 5 0 11 10 0')
        self.assertEqual(anchors(sps[0])[-1], (10.0, 0.0))
        self.assertEqual(anchors(sps[0]), anchors(engine.parse_path_d('M0 0A5 5 0 1 1 10 0')[0]))

    def test_flags_packed_with_decimal(self):
        # rx=.5 ry=.5, flags 0 and 1, then x=.5 y=.5.
        sps = engine.parse_path_d('M0 0a.5.5 0 01.5.5')
        self.assertEqual(anchors(sps[0])[-1], (0.5, 0.5))

    def test_numbers_after_close_do_not_hang(self):
        sps = parse_with_timeout('M0 0L1 1Z2 2')
        self.assertEqual(len(sps), 1)
        self.assertTrue(sps[0].closed)
        self.assertEqual(anchors(sps[0]), [(0.0, 0.0), (1.0, 1.0)])

    def test_malformed_data_keeps_what_parsed(self):
        self.assertEqual(anchors(parse_with_timeout('M0 0L5 5L1')[0]), [(0.0, 0.0), (5.0, 5.0)])
        self.assertEqual(parse_with_timeout('L1 1'), [])  # no opening moveto
        self.assertEqual(parse_with_timeout('5 5'), [])
        self.assertEqual(anchors(parse_with_timeout('M0 0L5 5 x 9 9')[0]), [(0.0, 0.0), (5.0, 5.0)])
        self.assertEqual(parse_with_timeout(''), [])

    def test_bad_arc_flag_stops_path(self):
        sps = parse_with_timeout('M0 0L1 0A5 5 0 2 0 10 0')
        self.assertEqual(anchors(sps[0]), [(0.0, 0.0), (1.0, 0.0)])

    def test_ordinary_paths_unchanged(self):
        sps = engine.parse_path_d('M1-2l.5.5e1 3 3H10V0z m1 1 h2')
        self.assertEqual(anchors(sps[0]), [(1.0, -2.0), (1.5, 3.0), (4.5, 6.0), (10.0, 6.0), (10.0, 0.0)])
        self.assertTrue(sps[0].closed)
        self.assertEqual(anchors(sps[1]), [(2.0, -1.0), (4.0, -1.0)])


class LengthTests(unittest.TestCase):
    def test_units(self):
        L = engine.svg_length_px
        self.assertEqual(L('10'), 10.0)
        self.assertEqual(L('10px'), 10.0)
        self.assertEqual(L(' 2.5 '), 2.5)
        self.assertAlmostEqual(L('1in'), 96.0)
        self.assertAlmostEqual(L('2.54cm'), 96.0)
        self.assertAlmostEqual(L('25.4mm'), 96.0)
        self.assertAlmostEqual(L('72pt'), 96.0)
        self.assertAlmostEqual(L('1pc'), 16.0)
        self.assertAlmostEqual(L('1em'), 16.0)
        self.assertAlmostEqual(L('1ex'), 8.0)
        self.assertAlmostEqual(L('1E1'), 10.0)

    def test_percent_and_fallbacks(self):
        L = engine.svg_length_px
        self.assertEqual(L('50%', 200.0), 100.0)
        self.assertIsNone(L('50%'))
        self.assertEqual(L('50%', None, 0.0), 0.0)
        self.assertIsNone(L(None))
        self.assertIsNone(L(''))
        self.assertIsNone(L('auto'))
        self.assertIsNone(L('10furlongs'))

    def test_stroke_width_length(self):
        self.assertEqual(engine.parse_svg_length('2'), 2.0)
        self.assertEqual(engine.parse_svg_length('2px'), 2.0)
        self.assertAlmostEqual(engine.parse_svg_length('1mm'), 96.0 / 25.4)
        self.assertIsNone(engine.parse_svg_length('5%'))
        self.assertIsNone(engine.parse_svg_length(''))


class GeometryAttributeTests(unittest.TestCase):
    def test_full_canvas_rect(self):
        node = walk_one(f'<rect {SVG_NS} width="100%" height="100%" fill="#fff"/>')
        self.assertEqual(sorted(anchors(node.subpaths[0])),
                         sorted([(0.0, 0.0), (200.0, 0.0), (200.0, 100.0), (0.0, 100.0)]))

    def test_rect_with_units(self):
        node = walk_one(f'<rect {SVG_NS} x="1in" y="10px" width="10" height="5pt"/>')
        xs = [a[0] for a in anchors(node.subpaths[0])]
        ys = [a[1] for a in anchors(node.subpaths[0])]
        self.assertAlmostEqual(min(xs), 96.0, places=5)
        self.assertAlmostEqual(max(xs), 106.0, places=5)
        self.assertAlmostEqual(min(ys), 10.0, places=5)
        self.assertAlmostEqual(max(ys), 10.0 + 5 * 96.0 / 72.0, places=5)

    def test_circle_percentages(self):
        # r="10%" is of the normalized diagonal sqrt((w^2 + h^2) / 2).
        node = walk_one(f'<circle {SVG_NS} cx="50%" cy="50%" r="10%"/>')
        r = 0.1 * math.sqrt((200.0 ** 2 + 100.0 ** 2) / 2.0)
        xs = [a[0] for a in anchors(node.subpaths[0])]
        ys = [a[1] for a in anchors(node.subpaths[0])]
        self.assertAlmostEqual(max(xs), 100.0 + r, places=5)
        self.assertAlmostEqual(min(ys), 50.0 - r, places=5)

    def test_circle_px_radius(self):
        node = walk_one(f'<circle {SVG_NS} cx="10" cy="10" r="10px"/>')
        self.assertAlmostEqual(max(a[0] for a in anchors(node.subpaths[0])), 20.0)

    def test_ellipse_and_line(self):
        node = walk_one(f'<ellipse {SVG_NS} cx="50%" cy="10" rx="10%" ry="1em"/>')
        xs = [a[0] for a in anchors(node.subpaths[0])]
        ys = [a[1] for a in anchors(node.subpaths[0])]
        self.assertAlmostEqual(max(xs), 120.0)
        self.assertAlmostEqual(max(ys), 26.0)
        node = walk_one(f'<line {SVG_NS} x1="0" y1="0" x2="100%" y2="50%" stroke="#000"/>')
        self.assertEqual(anchors(node.subpaths[0]), [(0.0, 0.0), (200.0, 50.0)])

    def test_unparseable_geometry_is_skipped_not_raised(self):
        self.assertIsNone(walk_one(f'<rect {SVG_NS} width="auto" height="10"/>'))
        self.assertIsNone(walk_one(f'<circle {SVG_NS} r="big"/>'))

    def test_use_offset_with_units(self):
        root = ET.fromstring(f'<svg {SVG_NS}><rect id="r" width="1" height="1"/>'
                             '<use href="#r" x="10px" y="50%"/></svg>')
        id_map = {el.get('id'): el for el in root.iter() if el.get('id')}
        node = engine.walk(root[1], engine.IDENTITY, "#000000", 1.0, itertools.count(1), id_map,
                           viewport=(200.0, 100.0))
        self.assertEqual(min(anchors(node.subpaths[0])), (10.0, 50.0))


class ViewBoxTests(unittest.TestCase):
    def vb(self, attrs):
        return engine.get_svg_viewbox(ET.fromstring(f'<svg {SVG_NS} {attrs}/>'))

    def test_unitless_and_px_unchanged(self):
        self.assertEqual(self.vb('width="800" height="600" viewBox="0 0 1024 768"'),
                         (0.0, 0.0, 1024.0, 768.0, 800.0, 600.0))
        self.assertEqual(self.vb('width="800px" height="600px"'), (0.0, 0.0, 800.0, 600.0, 800.0, 600.0))
        self.assertEqual(self.vb('viewBox="10 20 100 50"'), (10.0, 20.0, 100.0, 50.0, 100.0, 50.0))
        self.assertEqual(self.vb(''), (0.0, 0.0, 100.0, 100.0, 100.0, 100.0))

    def test_absolute_units_convert(self):
        _, _, _, _, w, h = self.vb('width="210mm" height="297mm" viewBox="0 0 210 297"')
        self.assertAlmostEqual(w, 793.7007874, places=5)
        self.assertAlmostEqual(h, 297 * 96 / 25.4, places=5)

    def test_missing_or_percent_height_uses_aspect_ratio(self):
        for attrs in ('width="400" height="100%" viewBox="0 0 200 100"',
                      'width="400" viewBox="0 0 200 100"'):
            _, _, _, _, w, h = self.vb(attrs)
            self.assertEqual((w, h), (400.0, 200.0), attrs)
        _, _, _, _, w, h = self.vb('width="100%" height="50" viewBox="0 0 200 100"')
        self.assertEqual((w, h), (100.0, 50.0))
        _, _, _, _, w, h = self.vb('width="100%" height="100%" viewBox="0 0 200 100"')
        self.assertEqual((w, h), (200.0, 100.0))
        _, _, _, _, w, h = self.vb('width="300"')
        self.assertEqual((w, h), (300.0, 100.0))

    def test_degenerate_viewbox_ignored(self):
        self.assertEqual(self.vb('width="50" height="40" viewBox="0 0 0 0"'),
                         (0.0, 0.0, 50.0, 40.0, 50.0, 40.0))


class ColorTests(unittest.TestCase):
    def assertColor(self, value, expected):
        got = engine.parse_color(value)
        self.assertIsNotNone(got, value)
        for g, e in zip(got, expected):
            self.assertAlmostEqual(g, e, places=3, msg=value)

    def test_legacy_and_css4_rgb(self):
        red = (1.0, 0.0, 0.0, 1.0)
        for v in ('rgb(255,0,0)', 'rgb(255, 0, 0)', 'rgb(255 0 0)', 'rgb(100%,0%,0%)',
                  'rgb(100% 0% 0%)', 'RGB(255,0,0)', 'rgba(255,0,0,1)'):
            self.assertColor(v, red)
        self.assertColor('rgb(255 0 0 / 50%)', (1.0, 0.0, 0.0, 0.5))
        self.assertColor('rgb(255 0 0 / .25)', (1.0, 0.0, 0.0, 0.25))
        self.assertColor('rgba(0, 0, 255, 0.5)', (0.0, 0.0, 1.0, 0.5))
        self.assertColor('rgb(300, -5, 0)', (1.0, 0.0, 0.0, 1.0))  # clamped

    def test_hsl(self):
        self.assertColor('hsl(120, 100%, 50%)', (0.0, 1.0, 0.0, 1.0))
        self.assertColor('hsla(240, 100%, 50%, 0.5)', (0.0, 0.0, 1.0, 0.5))
        self.assertColor('hsl(0deg 100% 50% / 25%)', (1.0, 0.0, 0.0, 0.25))
        self.assertColor('hsl(0.5turn 100% 50%)', (0.0, 1.0, 1.0, 1.0))

    def test_named_colors_case_insensitive(self):
        self.assertColor('Red', (1.0, 0.0, 0.0, 1.0))
        self.assertColor('WHITE', (1.0, 1.0, 1.0, 1.0))
        self.assertColor('crimson', (0xdc / 255.0, 0x14 / 255.0, 0x3c / 255.0, 1.0))
        self.assertColor('RebeccaPurple', (0x66 / 255.0, 0x33 / 255.0, 0x99 / 255.0, 1.0))
        self.assertEqual(len(engine._NAMED_COLORS), 148)
        self.assertEqual(engine.parse_color('transparent'), (0.0, 0.0, 0.0, 0.0))
        self.assertIsNone(engine.parse_color('None'))

    def test_hex(self):
        self.assertColor('#F00', (1.0, 0.0, 0.0, 1.0))
        self.assertColor('#00ff00', (0.0, 1.0, 0.0, 1.0))
        self.assertColor('#0000ff80', (0.0, 0.0, 1.0, 128 / 255.0))
        self.assertColor('#f008', (1.0, 0.0, 0.0, 0x88 / 255.0))

    def test_malformed_never_raises(self):
        for v in ('#ggg', '#12', '#1234567', 'rgb(1,2)', 'rgb(a,b,c)', 'rgb(nan,0,0)',
                  'rgb(1,2,3,4,5)', 'rgb(', 'hsl(10%, 50%, 50%)', 'currentColor',
                  'notacolor', 'url(#grad)', 'none', '', None):
            self.assertIsNone(engine.parse_color(v), v)

    def test_unparsed_color_is_noted_not_fatal(self):
        report = engine.ImportReport()
        el = ET.fromstring(f'<rect {SVG_NS} width="10" height="10" fill="rgb(1,2)"/>')
        node = engine.walk(el, engine.IDENTITY, "#000000", 1.0, itertools.count(1), report=report)
        self.assertIsNotNone(node)
        self.assertIsNone(node.color)
        self.assertEqual(report.unparsed_colors, 1)


class BuildSvgToolsTests(unittest.TestCase):
    def test_problem_svg_builds(self):
        svg = (f'<svg {SVG_NS} width="210mm" height="100%" viewBox="0 0 200 100">'
               '<rect width="100%" height="100%" fill="Red"/>'
               '<circle cx="50%" cy="50%" r="10px" fill="rgb(0 255 0 / 50%)"/>'
               '<path d="M10 10a5 5 0 1010 0Z2 2" fill="hsl(240 100% 50%)"/>'
               '</svg>')
        fd, path = tempfile.mkstemp(suffix='.svg')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                f.write(svg)
            logs = []
            tools, label, count = engine.build_svg_tools(path, 1920, 1080, log=logs.append)
        finally:
            os.remove(path)
        self.assertEqual(len(tools), 1)
        self.assertTrue(any('793.701 x 396.85' in line for line in logs), logs)
        self.assertTrue(any('Found 3 shape(s)' in line for line in logs), logs)


def walk_doc(xml):
    """Walks every child of a whole <svg> document the way build_svg_tools
    does (CSS and gradients included); returns (shapes, report)."""
    root = ET.fromstring(xml)
    report = engine.ImportReport()
    css = engine.parse_css_classes(root, report)
    gradients = engine.parse_gradient_defs(root)
    shapes = []

    def collect(n):
        if isinstance(n, engine.ShapeNode):
            shapes.append(n)
        elif n is not None:
            for c in n.children:
                collect(c)

    for child in root:
        collect(engine.walk(child, engine.IDENTITY, "#000000", 1.0, itertools.count(1),
                            gradient_defs=gradients or None, report=report,
                            viewport=(200.0, 100.0), css_classes=css))
    return shapes, report


class StyleTests(unittest.TestCase):
    def test_css_classes_including_grouped_selectors(self):
        # Illustrator's own shape of <style>.
        shapes, report = walk_doc(
            f'<svg {SVG_NS}><defs><style>/* made by Illustrator */'
            '.cls-1,.cls-2{fill:none;}.cls-2{stroke:#00f;stroke-width:2px;}'
            '.cls-3{fill:#e30613;}</style></defs>'
            '<rect class="cls-3" width="10" height="10"/>'
            '<rect class="cls-2" width="10" height="10"/>'
            '<rect class="cls-1" width="10" height="10"/></svg>')
        self.assertEqual(len(shapes), 2)  # cls-1 is unpainted
        self.assertEqual(shapes[0].color, engine.parse_color('#e30613'))
        self.assertFalse(shapes[1].has_fill)
        self.assertTrue(shapes[1].has_stroke)
        self.assertEqual(report.unsupported_css_rules, 0)

    def test_inline_style_beats_class_and_class_beats_attribute(self):
        shapes, _ = walk_doc(
            f'<svg {SVG_NS}><style>.a{{fill:#f00}}</style>'
            '<rect class="a" fill="#0f0" width="1" height="1"/>'
            '<rect class="a" style="fill:#00f" width="1" height="1"/></svg>')
        self.assertEqual(shapes[0].color, engine.parse_color('#f00'))
        self.assertEqual(shapes[1].color, engine.parse_color('#00f'))

    def test_unsupported_css_is_reported(self):
        _, report = walk_doc(
            f'<svg {SVG_NS}><style>path{{fill:red}} #x{{fill:red}} .a{{fill:red}}</style></svg>')
        self.assertEqual(report.unsupported_css_rules, 2)
        self.assertTrue(report.has_findings())

    def test_hidden_layers_are_skipped(self):
        shapes, _ = walk_doc(
            f'<svg {SVG_NS}><style>.off{{display:none}}</style>'
            '<g display="none"><rect width="1" height="1"/></g>'
            '<g class="off"><rect width="1" height="1"/></g>'
            '<rect style="visibility:hidden" width="1" height="1"/>'
            '<g visibility="hidden"><rect width="1" height="1"/>'
            '<rect id="shown" visibility="visible" width="1" height="1"/></g></svg>')
        self.assertEqual([s.name for s in shapes], ['shown'])

    def test_fill_opacity_does_not_dim_the_stroke_and_is_inherited(self):
        shapes, _ = walk_doc(
            f'<svg {SVG_NS}><g fill-opacity="0.5" opacity="0.8">'
            '<rect fill="#f00" stroke="#00f" stroke-opacity="0.25" width="1" height="1"/></g></svg>')
        self.assertAlmostEqual(shapes[0].opacity, 0.4)
        self.assertAlmostEqual(shapes[0].stroke_opacity, 0.2)

    def test_bounding_box_gradient_turns_with_the_shape(self):
        shapes, _ = walk_doc(
            f'<svg {SVG_NS}><linearGradient id="g"><stop offset="0" stop-color="#000"/>'
            '<stop offset="1" stop-color="#fff"/></linearGradient>'
            '<rect width="10" height="4" fill="url(#g)" transform="rotate(90)"/></svg>')
        g = shapes[0].fill_gradient
        # Left-to-right across the rect, rotated 90 degrees: now top-to-bottom.
        self.assertAlmostEqual(g["p0"][0], 0.0)
        self.assertAlmostEqual(g["p0"][1], 0.0)
        self.assertAlmostEqual(g["p1"][0], 0.0)
        self.assertAlmostEqual(g["p1"][1], 10.0)


if __name__ == '__main__':
    unittest.main()
